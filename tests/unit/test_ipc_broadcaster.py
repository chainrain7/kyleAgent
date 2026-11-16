from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from kyle_agent.core.bus.events import RunStartedEvent, StepStartedEvent
from kyle_agent.core.transport.ipc_broadcaster import IpcEventBroadcaster


def _make_writer(*, drain_raises: Exception | None = None) -> asyncio.StreamWriter:
    writer = MagicMock(spec=asyncio.StreamWriter)
    if drain_raises is not None:
        writer.drain = AsyncMock(side_effect=drain_raises)
    else:
        writer.drain = AsyncMock()
    return cast(asyncio.StreamWriter, writer)


def _run_started(run_id: str = "r1") -> RunStartedEvent:
    return RunStartedEvent(run_id=run_id, goal="test", ts="2026-01-01T00:00:00Z")


# 功能：验证 subscribe 后 handle 将匹配 topic 的事件写入 writer，且内容是合法的 EventPushEnvelope
# 设计：用 MagicMock writer 捕获写入的字节，反序列化后断言 kind 和 event.type；
#       广播走每订阅 pump 异步投递，handle 后用 wait_pending 等队列排空再断言
async def test_subscriber_receives_matching_event() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])

    await broadcaster.handle(_run_started())
    await broadcaster.wait_pending()

    writer.write.assert_called_once()  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["kind"] == "event"
    assert data["event"]["type"] == "run.started"
    await broadcaster.close()


# 功能：验证无订阅时 handle 不向任何 writer 写入数据
# 设计：创建 broadcaster 但不 subscribe，调用 handle 后断言 write 从未被调用，验证空 fan-out 的边界情况
async def test_no_subscription_no_write() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()

    await broadcaster.handle(_run_started())
    await broadcaster.wait_pending()

    writer.write.assert_not_called()  # type: ignore[attr-defined]
    await broadcaster.close()


# 功能：验证 topic glob "step.*" 匹配 step.started 但不匹配 run.started
# 设计：向同一 broadcaster 发布两种事件，断言 write 只被调用一次，验证 fnmatch 语义的 glob 边界行为
async def test_topic_glob_matches_step_not_run() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["step.*"])

    step_event = StepStartedEvent(run_id="r1", step=1, ts="2026-01-01T00:00:00Z")
    run_event = _run_started()

    await broadcaster.handle(step_event)
    await broadcaster.handle(run_event)
    await broadcaster.wait_pending()

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["event"]["type"] == "step.started"
    await broadcaster.close()


# 功能：验证 scope="global" 的订阅能收到任意 run_id 的事件
# 设计：发布两个不同 run_id 的事件，断言两次都写入，确认 global scope 不过滤 run_id 字段
async def test_scope_global_receives_all_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="global")

    await broadcaster.handle(_run_started("r1"))
    await broadcaster.handle(_run_started("r2"))
    await broadcaster.wait_pending()

    assert writer.write.call_count == 2  # type: ignore[attr-defined]
    await broadcaster.close()


# 功能：验证 scope="run:<id>" 只接收匹配 run_id 的事件，过滤其他 run_id
# 设计：订阅 scope="run:abc"，发布 run_id="abc" 和 run_id="xyz"，断言只写入一次，验证 run-specific scope 的过滤语义
async def test_scope_run_specific_filters_other_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="run:abc")

    await broadcaster.handle(_run_started("abc"))
    await broadcaster.handle(_run_started("xyz"))
    await broadcaster.wait_pending()

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    await broadcaster.close()


# 功能：验证 unsubscribe 后 handle 不再向该 writer 发送事件
# 设计：先 subscribe 再 unsubscribe，再调用 handle，断言 write 从未被调用，验证订阅生命周期的正确性
async def test_unsubscribe_stops_delivery() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])
    broadcaster.unsubscribe(writer)

    await broadcaster.handle(_run_started())
    await broadcaster.wait_pending()

    writer.write.assert_not_called()  # type: ignore[attr-defined]
    await broadcaster.close()


# 功能：验证写入失败（ConnectionResetError）后订阅自动移除，下次 handle 不再尝试写入
# 设计：drain() 抛出 ConnectionResetError 由 pump 捕获触发死连接清理；wait_pending 等 pump
#       走完错误路径后断言第二次 handle 时 write 未被调用；第一次 write 在 drain 前已执行，
#       call_count==1 是预期行为而非被测点
async def test_dead_connection_removed_after_failure() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer(drain_raises=ConnectionResetError())
    broadcaster.subscribe(writer, topics=["run.*"])

    event = _run_started()
    await broadcaster.handle(event)  # pump 里 drain 失败 → 订阅被移除
    await broadcaster.wait_pending()

    assert writer.write.call_count == 1  # type: ignore[attr-defined]

    writer.write.reset_mock()  # type: ignore[attr-defined]
    await broadcaster.handle(event)  # no subscribers remain
    await broadcaster.wait_pending()
    writer.write.assert_not_called()  # type: ignore[attr-defined]
    await broadcaster.close()


# 功能：验证慢消费者的 drain 阻塞不会反压 handle（背压隔离的核心行为）
# 设计：drain 侧用 Event 卡住直到放行；断言 handle 入队即返回（elapsed 远小于 drain 等待时间），
#       放行后事件仍被完整投递，证明阻塞被隔离在每订阅 pump 内而非发布热路径上
async def test_slow_consumer_does_not_block_publish() -> None:
    broadcaster = IpcEventBroadcaster(queue_size=100)
    released = asyncio.Event()
    writer = _make_writer()

    async def blocked_drain() -> None:
        await released.wait()

    writer.drain = AsyncMock(side_effect=blocked_drain)  # type: ignore[method-assign]
    broadcaster.subscribe(writer, topics=["run.*"])

    t0 = time.perf_counter()
    await broadcaster.handle(_run_started())
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1  # 入队即返回,不等 drain

    writer.write.assert_not_called()  # type: ignore[attr-defined]  # pump 仍在等 drain
    released.set()
    await broadcaster.wait_pending()
    writer.write.assert_called_once()  # type: ignore[attr-defined]
    await broadcaster.close()


# 功能：验证队列溢出时丢弃最旧事件（drop-oldest），最新事件得以保留
# 设计：queue_size=2 且 drain 全程阻塞，连发 4 个不同 run_id 的事件——前两次入队、
#       后两次挤掉最旧；断言 dropped 计数与最终投递的是最新的 r2/r3 两个事件
async def test_queue_overflow_drops_oldest() -> None:
    broadcaster = IpcEventBroadcaster(queue_size=2)
    released = asyncio.Event()
    writer = _make_writer()

    async def blocked_drain() -> None:
        await released.wait()

    writer.drain = AsyncMock(side_effect=blocked_drain)  # type: ignore[method-assign]
    broadcaster.subscribe(writer, topics=["run.*"])

    # handle 内部无 await,4 次发布期间 pump 不会得到调度,队列状态完全确定
    for i in range(4):
        await broadcaster.handle(_run_started(f"r{i}"))

    sub = broadcaster._subscriptions[0]  # noqa: SLF001  # 测试探针:读内部计数
    assert sub.dropped == 2
    assert sub.queue.qsize() == 2

    released.set()
    await broadcaster.wait_pending()
    payloads = [
        json.loads(call.args[0].rstrip(b"\n")) for call in writer.write.call_args_list
    ]
    assert [p["event"]["run_id"] for p in payloads] == ["r2", "r3"]
    await broadcaster.close()


# 功能：验证 unsubscribe 会取消对应 pump 协程，任务不残留
# 设计：订阅拿到内部 task 引用后 unsubscribe，直接 await 被取消的任务（抑制 CancelledError）
#       断言任务终态为 cancelled，且后续 handle 不再投递
async def test_unsubscribe_cancels_pump() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])
    task = broadcaster._subscriptions[0].task  # noqa: SLF001  # 测试探针:取 pump 任务
    assert task is not None

    broadcaster.unsubscribe(writer)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()

    await broadcaster.handle(_run_started())
    await broadcaster.wait_pending()
    writer.write.assert_not_called()  # type: ignore[attr-defined]
    await broadcaster.close()
