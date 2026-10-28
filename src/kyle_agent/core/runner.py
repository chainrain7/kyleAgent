from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kyle_agent.core.bus.events import RunFinishedEvent, RunStartedEvent
from kyle_agent.core.compact.compactor import Compactor
from kyle_agent.core.config import KyleConfig
from kyle_agent.core.context import ExecutionContext
from kyle_agent.core.durable import SNAPSHOT_FILENAME, FileRunPersister, RunSnapshot
from kyle_agent.core.events.bus import EventBus, EventHandler
from kyle_agent.core.events.writer import EventWriter
from kyle_agent.core.llm.base import LLMProvider
from kyle_agent.core.llm.factory import build_provider
from kyle_agent.core.llm.router import build_router
from kyle_agent.core.loop import AgentLoop
from kyle_agent.core.mcp.server import McpServerManager
from kyle_agent.core.memory.index import MemoryIndex
from kyle_agent.core.memory.loader import load_context_file
from kyle_agent.core.permissions.manager import PermissionManager
from kyle_agent.core.runs import RUNS_DIR, new_run_id
from kyle_agent.core.session.model import Session
from kyle_agent.core.session.store import SessionStore
from kyle_agent.core.subagent.registry import BackgroundTaskRegistry
from kyle_agent.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kyle_agent.core.task.manager import TaskManager
from kyle_agent.core.tools.builtin import (
    BashTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WebSearchTool,
    WriteFileTool,
)
from kyle_agent.core.tools.registry import ToolRegistry
from kyle_agent.core.trace.provider import TracingProvider
from kyle_agent.core.trace.writer import TraceWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: KyleConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        memory_index: MemoryIndex | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        # 跨会话记忆库;不注入时按 config.memory 决定是否自建(生产默认开)
        self._memory_index = memory_index
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = BackgroundTaskRegistry()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        memory_index: MemoryIndex | None = None,
    ) -> ToolRegistry:
        # 空列表表示"不允许任何工具"，None 表示不限制；必须用 is not None 区分
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist is not None else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool(), WebSearchTool()]:
            if _ok(t.name):
                registry.register(t)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id, memory_index=memory_index)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        # OnCallCopilot 业务层:[oncall] enabled 时注册领域工具
        if self._config.oncall.enabled:
            from kyle_agent.oncall.tools import build_oncall_tools

            env_dir = Path(self._config.oncall.env_dir).expanduser()
            for t in build_oncall_tools(env_dir):
                if _ok(t.name):
                    registry.register(t)

        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        resume: RunSnapshot | None = None,
    ) -> RunOutcome:
        run_id = run_id or (resume.run_id if resume is not None else None) or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        # durable run 恢复:以快照消息为 prefill、从断点步续跑,
        # goal/max_steps/override 均以快照为准(调用方无需重复传对)
        if resume is not None:
            history = resume.messages
            goal = resume.goal
            system_prompt_override = resume.system_prompt_override

        global_ctx = load_context_file(Path("~/.kyle/context.md").expanduser())
        project_ctx = load_context_file(Path(".kyle/context.md"))

        task_manager = TaskManager(run_path / ".tasks")

        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        # 分层记忆:跨会话检索与目标相关的持久记忆注入 system prompt
        # 注入的实例优先(测试/定制用);否则按 config.memory 自建
        memory_index: MemoryIndex | None = self._memory_index
        recalled = ""
        if memory_index is None and self._config.memory.enabled:
            memory_index = MemoryIndex(Path(self._config.memory.db).expanduser())
        if memory_index is not None:
            hits = memory_index.recall(goal, k=self._config.memory.top_k)
            if hits:
                recalled = "\n".join(
                    f"- ({h.ts[:10]} {h.source}) {h.content}" for h in hits
                )

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=resume.max_steps if resume is not None else self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            recalled_memories=recalled,
            system_prompt_override=system_prompt_override,
        )
        if resume is not None:
            # 从断点步续跑,步数预算按快照进度累计
            context.step = resume.step
        prefill_len = len(history)

        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            # durable run:每步快照(run_state.json),run 结束写终态
            persister: FileRunPersister | None = None
            if self._config.agent.durable_runs:
                persister = FileRunPersister(
                    run_path / SNAPSHOT_FILENAME,
                    session.id if session is not None else "",
                )
            try:
                provider: LLMProvider = self._provider or build_provider(
                    self._config.llm
                )
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    memory_index=memory_index,
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                # 模型路由(static 保持旧行为;rule_based 见 llm/router.py)
                router = build_router(self._config.llm)
                compactor = Compactor(
                    bus, session_dir, session_id_str, router=router,
                    keep_recent_turns=self._config.compaction.keep_recent_turns,
                )
                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                    router=router,
                    max_parallel_tools=self._config.agent.max_parallel_tools,
                    persister=persister,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

            # 终态快照:status 非 running,daemon 重启后不再恢复该 run
            if persister is not None:
                try:
                    await persister.persist(context)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "terminal snapshot persist failed run_id=%s", run_id
                    )

            # OnCallCopilot:run 含 submit_diagnosis 时生成处置报告(失败不影响 run)
            if self._config.oncall.enabled:
                try:
                    from kyle_agent.oncall.report import generate_report
                    from kyle_agent.oncall.sim import SimEnv

                    env_dir = Path(self._config.oncall.env_dir).expanduser()
                    generate_report(run_path, SimEnv(env_dir), run_id)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "oncall report generation failed run_id=%s", run_id
                    )

        # 成功 run 的结论摘要回写记忆库(截断 300 字,来源标注 run)
        if (
            memory_index is not None
            and context.status == "success"
            and context.result.strip()
        ):
            memory_index.remember(
                context.result.strip()[:300],
                source="run",
                session_id=session.id if session is not None else "",
            )

        if session is not None and store is not None:
            # 持久化前裁掉尾部未配对的 tool_use/tool_result，保证 thread.jsonl 始终平衡；
            # 否则取消 run 留下的孤儿会让后续 read_messages 永久裁掉其后追加的消息
            if context.compacted or resume is not None:
                # 压缩/恢复场景 prefill 切片不再有效:整表重写 thread.jsonl
                # (原文件自动备份)——恢复场景下快照段落必须一并写入
                store.write_compacted(
                    session.id, store.trim_orphan_tool_use(context.messages)
                )
            else:
                tail = store.trim_orphan_tool_use(context.messages[prefill_len:])
                store.append_messages(session.id, tail, run_id=run_id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
