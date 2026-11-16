from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from kyle_agent.core.bus.envelope import EventPushEnvelope
from kyle_agent.core.trace.record import TraceRecord
from kyle_agent.core.trace.writer import TraceWriter

logger = logging.getLogger(__name__)

# 每订阅者事件队列上限:慢消费者只堵塞自己的队列,超出后丢弃最旧事件(drop-oldest)
DEFAULT_QUEUE_SIZE = 1000

# 队列项:编码后的信封字节 + trace 所需元数据(run_id / 事件类型)
type _QueueItem = tuple[bytes, str | None, str]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _swallow(task: asyncio.Task[None]) -> None:
    # 取回已结束任务的异常,避免 asyncio 的 "exception was never retrieved" 告警
    if not task.cancelled():
        task.exception()


@dataclass
class _Subscription:
    sub_id: str
    writer: asyncio.StreamWriter
    topics: list[str]
    scope: str
    queue: asyncio.Queue[_QueueItem | None]  # None 为关停哨兵
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    dropped: int = 0  # 队列满后累计丢弃的事件数(诊断慢消费者)


class IpcEventBroadcaster:
    """事件广播器(带背压隔离)。

    每个订阅者一条 bounded 队列 + 独立 pump 协程:bus.publish 的热路径只做
    topic/scope 过滤与入队(put_nowait),网络写与 drain 的流控等待全部移入 pump。
    单个慢客户端不再阻塞其他订阅者,也不会经 EventBus 反压卡停 agent loop;
    队列满时丢弃最旧事件(保最新状态,UI 最终一致)并限频告警。
    """

    def __init__(
        self,
        trace: TraceWriter | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._subscriptions: list[_Subscription] = []
        self._trace = trace
        self._queue_size = queue_size

    # 注册一个客户端订阅:建队列并启动该订阅的 pump 协程,返回 subscription_id
    def subscribe(
        self,
        writer: asyncio.StreamWriter,
        topics: list[str],
        scope: str = "global",
    ) -> str:
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = _Subscription(
            sub_id=sub_id,
            writer=writer,
            topics=topics,
            scope=scope,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        sub.task = asyncio.create_task(self._pump(sub))
        self._subscriptions.append(sub)
        return sub_id

    # 移除指定 writer 的所有订阅并取消对应 pump(不 join,见 _stop_sub 注释)
    def unsubscribe(self, writer: asyncio.StreamWriter) -> None:
        keep: list[_Subscription] = []
        for sub in self._subscriptions:
            if sub.writer is writer:
                self._stop_sub(sub)
            else:
                keep.append(sub)
        self._subscriptions = keep

    # 将事件分发到所有匹配订阅的队列:热路径,不 await 网络 IO
    async def handle(self, event: BaseModel) -> None:
        event_dict = event.model_dump()
        event_type: str = event_dict.get("type", "")
        run_id: str | None = event_dict.get("run_id")

        # 信封只编码一次,所有匹配订阅共享同一份字节
        item: _QueueItem | None = None
        for sub in list(self._subscriptions):
            if not self._matches_topic(event_type, sub.topics):
                continue
            if not self._matches_scope(run_id, sub.scope):
                continue
            if item is None:
                encoded = (
                    EventPushEnvelope(event=event_dict).model_dump_json() + "\n"
                ).encode()
                item = (encoded, run_id, event_type)
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # 慢消费者:丢最旧保最新;被丢事件的 task_done 在此补偿,join() 不会被卡
                try:
                    sub.queue.get_nowait()
                    sub.queue.task_done()
                    sub.queue.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                sub.dropped += 1
                # 限频告警:首次丢弃与每满 1000 次各记一条,避免持续丢时刷屏
                if sub.dropped == 1 or sub.dropped % 1000 == 0:
                    logger.warning(
                        "ipc broadcast: sub %s queue full (slow consumer), "
                        "dropped events total=%d",
                        sub.sub_id,
                        sub.dropped,
                    )

    # 等待所有在册订阅的队列排空(测试与优雅关停用;死亡订阅退出前也会补 task_done)
    async def wait_pending(self) -> None:
        await asyncio.gather(
            *(sub.queue.join() for sub in list(self._subscriptions))
        )

    # 关停:向每个订阅发哨兵让 pump 排空残余事件后退出,超时兜底强取消
    async def close(self) -> None:
        subs = list(self._subscriptions)
        for sub in subs:
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                self._stop_sub(sub)
        tasks = [sub.task for sub in subs if sub.task is not None]
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
        except TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()

    # 单个订阅的泵:从队列取事件写网络并等待流控,写失败即清理该订阅
    async def _pump(self, sub: _Subscription) -> None:
        while True:
            item = await sub.queue.get()
            try:
                if item is None:
                    return
                encoded, run_id, event_type = item
                try:
                    sub.writer.write(encoded)
                    await sub.writer.drain()
                    if self._trace is not None:
                        client_id = str(
                            sub.writer.get_extra_info("peername", "<unknown>")
                        )
                        self._trace.emit(
                            TraceRecord(
                                ts=_now(),
                                direction="CORE→CLIENT",
                                layer="ipc",
                                kind="push",
                                run_id=run_id,
                                client_id=client_id,
                                data={"sub_id": sub.sub_id, "event_type": event_type},
                            )
                        )
                except (ConnectionResetError, BrokenPipeError, OSError):
                    logger.debug("dead connection for sub %s, cleaning up", sub.sub_id)
                    self._remove_sub(sub)
                    return
            finally:
                sub.queue.task_done()

    # 取消订阅的 pump 任务(不 join:Windows Proactor 上 cancel+await 的收尾可能
    # 长时间不返回——TraceWriter 前车之鉴;异常由 done callback 吸收)
    @staticmethod
    def _stop_sub(sub: _Subscription) -> None:
        if sub.task is not None and not sub.task.done():
            sub.task.cancel()
            sub.task.add_done_callback(_swallow)

    # 按对象身份移除单个订阅(pump 发现死连接时调用,与按 writer 批量移除互补)
    def _remove_sub(self, sub: _Subscription) -> None:
        self._subscriptions = [s for s in self._subscriptions if s is not sub]

    # 检查事件类型是否匹配订阅的 topic 列表（支持 fnmatch glob 模式）
    @staticmethod
    def _matches_topic(event_type: str, topics: list[str]) -> bool:
        return any(fnmatch.fnmatch(event_type, pattern) for pattern in topics)

    # 检查事件 run_id 是否匹配订阅的 scope（global 全通，run:<id> 精确匹配）
    @staticmethod
    def _matches_scope(run_id: str | None, scope: str) -> bool:
        if scope == "global":
            return True
        if scope.startswith("run:"):
            return run_id == scope[4:]
        return False
