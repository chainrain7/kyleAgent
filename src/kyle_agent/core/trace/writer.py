from __future__ import annotations

import asyncio
from pathlib import Path

from kyle_agent.core.trace.record import TraceRecord

# stop() 等待 drain 退出的兜底超时:超时后强杀任务,保证 stop 必然返回。
# 旧实现(cancel + await task)在 Windows Proactor 下存在竞态——join waiter
# 永不唤醒且事件循环无定时器无 IO,表现为整个测试/进程无限挂死。
_STOP_TIMEOUT_S = 5.0


class TraceWriter:
    # 初始化 TraceWriter；写入目标文件路径在 start() 前不会创建
    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: asyncio.Queue[TraceRecord | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # 创建目录、启动后台 drain task
    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())

    # 入队哨兵让 drain 处理完剩余记录后自然退出,再等待任务收尾;
    # wait_for 兜底保证 stop 必然返回(超时强杀),不会无限挂起
    async def stop(self) -> None:
        if self._task is None:
            return
        self._queue.put_nowait(None)
        task = self._task
        self._task = None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_TIMEOUT_S)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass

    # 非阻塞地将 record 放入写入队列
    def emit(self, record: TraceRecord) -> None:
        self._queue.put_nowait(record)

    # 持续从队列读取 record 并追加写入文件;收到哨兵 None 时退出
    async def _drain(self) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            while True:
                record = await self._queue.get()
                try:
                    if record is None:
                        return
                    f.write(record.model_dump_json() + "\n")
                    f.flush()
                finally:
                    self._queue.task_done()
