from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kyle_agent.core.bus.events import StepFinishedEvent, StepStartedEvent
from kyle_agent.core.context import ExecutionContext
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.base import LLMProvider
from kyle_agent.core.llm.types import ToolCallBlock
from kyle_agent.core.tools.base import ToolResult
from kyle_agent.core.tools.invocation import invoke_tool
from kyle_agent.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kyle_agent.core.compact.compactor import Compactor
    from kyle_agent.core.llm.router import ModelRouter
    from kyle_agent.core.permissions.manager import PermissionManager


log = logging.getLogger(__name__)

# 可安全并行的只读工具集合:无本地状态变更,乱序执行不影响结果
_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file", "list_dir", "note_save",
    "task_get", "task_list", "agent_result", "web_search",
})

def _now() -> str:
    return datetime.now(UTC).isoformat()


# 默认系统提示:职责说明 + 运行平台/shell 提示(模型据此写对命令语法)
def _default_system_prompt() -> str:
    platform_hint = (
        "The bash tool runs commands via cmd.exe on Windows."
        if sys.platform == "win32"
        else "The bash tool runs POSIX shell commands."
    )
    return (
        "You are a helpful AI assistant. "
        "Use the available tools to complete the user's goal. "
        f"{platform_hint} "
        "When the goal is fully achieved, respond with a final answer "
        "and do not call any more tools."
    )


class AgentLoop:
    # 初始化循环依赖：LLM provider、工具注册表、事件总线，可选的权限管理器和压缩器
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        router: ModelRouter | None = None,
        max_parallel_tools: int = 4,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._router = router
        # 只读工具批次的并发上限;1 等效关闭并行
        self._max_parallel_tools = max(1, max_parallel_tools)

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            # 模型路由:按步次/历史特征选档,未配置 router 时保持原行为
            model_override: str | None = None
            strategy = "static"
            if self._router is not None:
                decision = self._router.select(
                    step=context.step,
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    purpose="chat",
                )
                model_override = decision.model
                strategy = self._router.strategy
                log.debug(
                    "router selected model=%s reason=%s run_id=%s step=%d",
                    decision.model, decision.reason, context.run_id, context.step,
                )
            try:
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(_default_system_prompt()),
                    model=model_override,
                    strategy=strategy,
                )
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)

            # [act] execute requested tools; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                results = await self._execute_tool_calls(response.tool_calls, context)
                for tc, result in zip(response.tool_calls, results, strict=True):
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could be "
                        "completed. Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # Termination check — end_turn wins over max_steps if both hit on same step
            if response.stop_reason == "end_turn":
                context.result = response.text or ""
                context.mark_success()
            elif context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

    # 执行一批工具调用:纯只读批次并发(信号量限流),含变更工具时整批串行;
    # 无论执行顺序如何,返回结果与 tool_calls 顺序一一对应
    async def _execute_tool_calls(
        self, tool_calls: list[ToolCallBlock], context: ExecutionContext
    ) -> list[ToolResult]:
        async def _one(tc: ToolCallBlock) -> ToolResult:
            return await invoke_tool(
                self._registry, tc, self._bus, context.run_id,
                permission_manager=self._permission_manager,
                session_id=self._session_id,
            )

        can_parallel = (
            len(tool_calls) > 1
            and self._max_parallel_tools > 1
            and all(tc.name in _READ_ONLY_TOOLS for tc in tool_calls)
        )
        if not can_parallel:
            return [await _one(tc) for tc in tool_calls]

        # 并发上限:同一批次内的只读工具同时执行(如一轮读 5 个文件)
        sem = asyncio.Semaphore(self._max_parallel_tools)

        async def _limited(tc: ToolCallBlock) -> ToolResult:
            async with sem:
                return await _one(tc)

        return list(await asyncio.gather(*(_limited(tc) for tc in tool_calls)))
