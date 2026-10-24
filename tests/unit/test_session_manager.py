from __future__ import annotations

from pathlib import Path

import pytest

from kyle_agent.core.bus.envelope import HandlerError
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.runner import RunOutcome
from kyle_agent.core.session.manager import SESSION_CLOSED, SESSION_NOT_FOUND, SessionManager
from kyle_agent.core.session.model import Session
from kyle_agent.core.session.store import SessionStore


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 功能：验证 create 会创建 active session、写入 meta 并发布 session.created 事件
# 设计：用真实 SessionStore + EventBus 收集事件，覆盖 manager 与 store/bus 的协作边界
async def test_create_session_writes_meta_and_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert store.read_meta(session.id).title == "title"
    assert [e.type for e in events] == ["session.created"]  # type: ignore[attr-defined]


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 kyle run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert store.read_meta(session.id).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


# 功能：resume("") 在"进程重启"(新 SessionManager 实例)后能从磁盘找回最近活跃 session
# 设计：创建两个 session 并关闭更新的那个,模拟 daemon 重启后新实例 resume,
#       断言找回的是仍活跃的旧 session 且 send_message 可继续(改造 E 的核心场景)
async def test_resume_latest_active_after_restart(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    older = await manager.create("chat", "older")
    newer = await manager.create("chat", "newer")
    await manager.close(newer.id)

    restarted = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    resumed = await restarted.resume("")

    assert resumed.id == older.id
    # 恢复后可以直接续聊
    await restarted.send_message(resumed.id, "hello again")
    messages = store.read_messages(resumed.id)
    assert messages[0]["content"] == "hello again"


# 功能：显式 session_id 的 resume 与不存在 id 的报错
# 设计：显式 id 恢复指定 session;不存在时抛 FileNotFoundError(磁盘无此 meta)
async def test_resume_explicit_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    restarted = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    assert (await restarted.resume(session.id)).id == session.id
    with pytest.raises(FileNotFoundError):
        await restarted.resume("sess-nosuch")


# 功能：磁盘上没有可恢复的活跃 session 时 resume 返回结构化错误
# 设计：空存储目录 + 新实例 → HandlerError(SESSION_NOT_FOUND),客户端据此退回 create
async def test_resume_no_active_returns_handler_error(tmp_path: Path) -> None:
    restarted = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await restarted.resume("")
    assert exc.value.code == SESSION_NOT_FOUND
