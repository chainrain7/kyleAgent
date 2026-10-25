from __future__ import annotations

import asyncio
from pathlib import Path

from kyle_agent.core.config import KyleConfig
from kyle_agent.core.context import ExecutionContext
from kyle_agent.core.durable import (
    FileRunPersister,
    RunSnapshot,
    load_snapshot,
    save_snapshot,
    scan_running,
)
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.types import LlmResponse
from kyle_agent.core.runner import AgentRunner
from kyle_agent.core.session.model import Session
from kyle_agent.core.session.store import SessionStore


# 构造带消息的快照
def _snap(**kw: object) -> RunSnapshot:
    base = RunSnapshot(
        run_id="run-x", session_id="sess-1", goal="do it", step=2,
        messages=[
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
        ],
    )
    for k, v in kw.items():
        setattr(base, k, v)  # type: ignore[attr-defined]
    return base


# 功能:快照保存/加载原子往返
# 设计:写入后读回字段一致;损坏文件 load 返回 None 而非抛异常
def test_snapshot_roundtrip_and_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "run_state.json"
    save_snapshot(p, _snap())
    loaded = load_snapshot(p)
    assert loaded is not None and loaded.run_id == "run-x" and loaded.step == 2
    p.write_text("{broken", encoding="utf-8")
    assert load_snapshot(p) is None


# 功能:scan_running 只收集 status=="running" 的快照
# 设计:运行中/已成功/损坏三种文件并存,断言只返回运行中的
def test_scan_running_filters(tmp_path: Path) -> None:
    d1 = tmp_path / "a" / "runs" / "r1"
    d2 = tmp_path / "a" / "runs" / "r2"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    save_snapshot(d1 / "run_state.json", _snap())
    save_snapshot(d2 / "run_state.json", _snap(status="success"))
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "run_state.json").write_text("junk", encoding="utf-8")
    pending = scan_running(tmp_path)
    assert len(pending) == 1 and pending[0][1].run_id == "run-x"


# 功能:AgentLoop 每步结束调用 persister,快照内容与 context 一致
# 设计:FileRunPersister 写入 run 目录;两步 run 后快照 step==2 且含消息
async def test_loop_persists_each_step(tmp_path: Path) -> None:
    from kyle_agent.core.llm.types import ToolCallBlock
    from kyle_agent.core.loop import AgentLoop
    from kyle_agent.core.tools.base import BaseTool, ToolResult
    from kyle_agent.core.tools.registry import ToolRegistry

    class _Echo(BaseTool):
        name = "read_file"
        description = "echo"
        input_schema: dict[str, object] = {"type": "object", "properties": {}}

        async def invoke(self, params: dict[str, object]) -> ToolResult:
            return ToolResult(content="ok")

    class _P:
        def __init__(self) -> None:
            self.n = 0

        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.n += 1
            if self.n == 1:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(id="t1", name="read_file", input={})],
                )
            return LlmResponse(stop_reason="end_turn", text="done")

    snap_path = tmp_path / "run_state.json"
    persister = FileRunPersister(snap_path, "sess-1")
    registry = ToolRegistry()
    registry.register(_Echo())
    ctx = ExecutionContext(run_id="r9", goal="g", max_steps=4)
    loop = AgentLoop(_P(), registry, EventBus(), persister=persister)  # type: ignore[arg-type]
    await loop.run(ctx)

    snap = load_snapshot(snap_path)
    assert snap is not None
    assert snap.step == 2 and snap.status == "success"
    assert snap.session_id == "sess-1"


# 功能:runner 从快照恢复续跑——从断点步继续,thread 整表重写含快照段落
# 设计:快照带 1 条 tool_result 历史;恢复 provider 断言收到的消息包含
#       该历史且直接 end_turn;结束后 read_messages 含快照段落与收尾
async def test_runner_resume_from_snapshot(tmp_path: Path) -> None:
    class _ResumeProvider:
        def __init__(self) -> None:
            self.seen: list[dict[str, object]] = []

        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.seen = [dict(m) for m in messages]
            return LlmResponse(stop_reason="end_turn", text="RESUMED-DONE")

    store = SessionStore(tmp_path / "sessions")
    session = Session(id="sess-1", mode="chat", status="active", title="",
                      created_at="t", updated_at="t")
    store.write_meta(session)

    snap = _snap(step=1, max_steps=6)
    provider = _ResumeProvider()
    cfg = KyleConfig()
    cfg.memory.enabled = False
    runner = AgentRunner(cfg, provider=provider, runs_dir=tmp_path / "runs")  # type: ignore[arg-type]
    outcome = await runner.run_and_capture(
        "ignored-goal", run_id="run-x", session=session, store=store, resume=snap
    )

    assert outcome.status == "success" and outcome.result == "RESUMED-DONE"
    # 续跑 provider 收到的是快照消息(含 working),不是 thread 旧历史
    assert any(m.get("content") == "do it" for m in provider.seen)
    # thread 整表重写:包含快照段落 + 恢复后的收尾
    messages = store.read_messages("sess-1")
    assert messages[0]["content"] == "do it"
    assert "RESUMED-DONE" in str(messages[-1]["content"])


# 功能:SessionManager 启动恢复——扫描到运行中快照则派发续跑任务
# 设计:手工放置 running 快照与 session meta;fake runner_factory 记录
#       收到的 resume 快照;调用 recover_pending_runs 后断言派发数
async def test_recover_pending_runs(tmp_path: Path) -> None:
    from kyle_agent.core.session.manager import SessionManager

    store = SessionStore(tmp_path)
    session = Session(id="sess-1", mode="chat", status="active", title="",
                      created_at="t", updated_at="t")
    store.write_meta(session)
    run_dir = store.runs_dir("sess-1") / "run-x"
    run_dir.mkdir(parents=True)
    save_snapshot(run_dir / "run_state.json", _snap())

    resumed: list[RunSnapshot] = []

    class _FakeRunner:
        async def run_and_capture(self, goal, *, run_id=None, session=None,
                                  store=None, system_prompt_override=None,
                                  tool_whitelist=None, resume=None):
            assert resume is not None
            resumed.append(resume)
            from kyle_agent.core.runner import RunOutcome
            return RunOutcome(status="success", result="ok", reason=None)

    manager = SessionManager(store, lambda: _FakeRunner(), EventBus())  # type: ignore[arg-type]
    count = await manager.recover_pending_runs()
    assert count == 1
    await asyncio.sleep(0.05)  # 等后台任务执行
    assert len(resumed) == 1 and resumed[0].run_id == "run-x"
