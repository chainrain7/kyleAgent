from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kyle_agent.core.bus.events import ContextCompactedEvent
from kyle_agent.core.compact.tokens import count_messages, count_text
from kyle_agent.core.events.bus import EventBus

if TYPE_CHECKING:
    from kyle_agent.core.context import ExecutionContext
    from kyle_agent.core.llm.base import LLMProvider
    from kyle_agent.core.llm.router import ModelRouter

logger = logging.getLogger(__name__)

_COMPACT_PROMPT = """\
You are compressing an agent conversation into a handoff state snapshot.
Another LLM instance will continue this task from your snapshot alone — make it complete.

Respond with ONLY a JSON object (no prose, no markdown fences) with exactly these keys:

{
  "goal": "one sentence describing what the user asked",
  "completed": ["specific steps done, with file paths / commands / decisions"],
  "files": {"path": "one-line current state of each created or modified file"},
  "todos": ["ordered remaining steps"],
  "facts": ["critical data the next LLM needs verbatim: ids, tokens, exact errors, config values"]
}

Rules:
- completed/files/todos/facts may be empty arrays/objects, but must exist.
- Omit reasoning and intermediate attempts; keep conclusions and concrete values.
"""


# 返回当前 UTC 时间的简短时间戳字符串（用于文件名）
def _ts_compact() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CompactionResult:
    summary_text: str
    original_token_estimate: int
    summary_tokens: int
    # 结构化状态快照(goal/completed/files/todos/facts);解析失败为 None
    structured: dict[str, Any] | None = None
    # 增量压缩时原样保留的最近轮次消息(压缩不触及)
    preserved_tail: list[dict[str, Any]] | None = None


class Compactor:
    # 初始化压缩器，绑定事件总线、session 目录、session ID、可选模型路由器与保留轮数
    def __init__(
        self,
        bus: EventBus,
        session_dir: Path,
        session_id: str,
        router: ModelRouter | None = None,
        keep_recent_turns: int = 0,
    ) -> None:
        self._bus = bus
        self._session_dir = session_dir
        self._session_id = session_id
        self._router = router
        # >0 时启用增量压缩:只摘要较早历史,最近 N 轮原样保留(0 = 全量替换)
        self._keep_recent_turns = keep_recent_turns

    # 压缩 ExecutionContext.messages，就地替换消息列表并写 summary 文件
    async def compact(
        self,
        context: ExecutionContext,
        provider: LLMProvider,
        focus: str = "",
    ) -> CompactionResult | None:
        # 增量压缩:最近 N 轮原样保留,只压缩更早的历史
        to_compress = context.messages
        tail: list[dict[str, Any]] = []
        if self._keep_recent_turns > 0:
            to_compress, tail = _split_recent_turns(
                context.messages, self._keep_recent_turns
            )
            if not to_compress:
                # 没有可压缩的更早历史(会话太短),本次跳过
                return None

        result = await self.compact_messages(to_compress, provider, focus=focus)
        if result is None:
            return None
        result.preserved_tail = tail

        context.messages = [
            {"role": "user", "content": result.summary_text},
            {"role": "assistant", "content": "Understood, I'll continue from this summary."},
            *tail,
        ]
        # 标记已压缩，runner 持久化时改用整表重写而非按 prefill 切片追加
        context.compacted = True
        self._write_summary(result.summary_text, result.structured)
        await self._bus.publish(
            ContextCompactedEvent(
                session_id=self._session_id,
                run_id=context.run_id,
                original_tokens=result.original_token_estimate,
                summary_tokens=result.summary_tokens,
                ts=_now(),
            )
        )
        logger.info(
            "context compacted session=%s run=%s original≈%d summary=%d tokens",
            self._session_id, context.run_id,
            result.original_token_estimate, result.summary_tokens,
        )
        return result

    # 纯函数式压缩：接收消息列表，返回 CompactionResult；失败时返回 None
    async def compact_messages(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        focus: str = "",
    ) -> CompactionResult | None:
        from kyle_agent.core.events.bus import EventBus as _Bus

        # 真实 BPE token 计数(cl100k_base),替代早期 char/4 粗估
        original_estimate = count_messages(messages)

        history_text = _messages_to_text(messages)
        prompt = _COMPACT_PROMPT
        if focus.strip():
            prompt += f"\n\nIMPORTANT: Pay special attention to: {focus.strip()}"

        compress_request: list[dict[str, object]] = [
            {"role": "user", "content": f"{prompt}\n\n---\n\n{history_text}"}
        ]

        # 压缩摘要属于执行类工作,路由启用时用便宜档模型
        model_override: str | None = None
        strategy = "static"
        if self._router is not None:
            decision = self._router.select(
                step=0, messages=messages, tool_schemas=[], purpose="compact"
            )
            model_override = decision.model
            strategy = self._router.strategy

        try:
            silent_bus = _Bus()
            response = await provider.chat(
                messages=compress_request,
                tool_schemas=[],
                bus=silent_bus,
                run_id="compact",
                step=0,
                system="You are a helpful assistant that summarizes conversations.",
                model=model_override,
                strategy=strategy,
            )
        except Exception:
            logger.exception("compactor: LLM call failed, skipping compaction")
            return None

        summary_text = response.text.strip()
        if not summary_text:
            logger.warning("compactor: LLM returned empty summary, skipping compaction")
            return None

        # 优先按结构化 JSON 解析;失败回退纯文本摘要(兼容任意模型输出)
        structured = _parse_structured(summary_text)
        if structured is not None:
            summary_text = _render_structured(structured)

        summary_tokens = (
            response.usage.output_tokens if response.usage else count_text(summary_text)
        )

        return CompactionResult(
            summary_text=summary_text,
            original_token_estimate=original_estimate,
            summary_tokens=summary_tokens,
            structured=structured,
        )

    # 将摘要写入 session 目录:summary_<ts>.md(可读) + 结构化时同写 summary_<ts>.json
    def _write_summary(self, text: str, structured: dict[str, Any] | None) -> None:
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            ts = _ts_compact()
            (self._session_dir / f"summary_{ts}.md").write_text(text, encoding="utf-8")
            if structured is not None:
                (self._session_dir / f"summary_{ts}.json").write_text(
                    json.dumps(structured, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
        except Exception:
            logger.exception("compactor: failed to write summary file")


# 从压缩响应中解析结构化 JSON;容忍 markdown 围栏,失败返回 None
def _parse_structured(text: str) -> dict[str, Any] | None:
    import re as _re

    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    candidate = m.group(1) if m else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "goal" not in data:
        return None
    return data


# 把结构化状态渲染为紧凑的 Markdown(作为注入上下文的摘要文本)
def _render_structured(data: dict[str, Any]) -> str:
    lines = ["# State Snapshot", f"Goal: {data.get('goal', '')}"]
    completed = data.get("completed") or []
    if completed:
        lines.append("Completed:")
        lines += [f"- {c}" for c in completed]
    files = data.get("files") or {}
    if files:
        lines.append("Files:")
        lines += [f"- {path}: {state}" for path, state in files.items()]
    todos = data.get("todos") or []
    if todos:
        lines.append("TODOs:")
        lines += [f"- {t}" for t in todos]
    facts = data.get("facts") or []
    if facts:
        lines.append("Facts:")
        lines += [f"- {f}" for f in facts]
    return "\n".join(lines)


# 把消息历史切为(待压缩前缀, 最近 keep_rounds 轮原样尾部);轮 = assistant 起 + 其后 user
def _split_recent_turns(
    messages: list[dict[str, Any]], keep_rounds: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # 从后向前找 keep_rounds 个 assistant 消息的下标
    assistant_idx = [
        i for i, m in enumerate(messages) if m.get("role") == "assistant"
    ]
    if len(assistant_idx) < keep_rounds + 1:
        # 整段都属于"最近",没有更早历史可压缩
        return [], messages
    split_at = assistant_idx[-keep_rounds]
    return messages[:split_at], messages[split_at:]


# 将消息列表序列化为可供 LLM 阅读的纯文本
def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            blocks: list[str] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(block.get("text", ""))
                elif btype == "tool_use":
                    blocks.append(
                        f"<tool_call name={block.get('name')} id={block.get('id')}>\n"
                        f"{block.get('input', {})}\n</tool_call>"
                    )
                elif btype == "tool_result":
                    blocks.append(
                        f"<tool_result id={block.get('tool_use_id')}>\n"
                        f"{block.get('content', '')}\n</tool_result>"
                    )
            parts.append(f"[{role}]\n" + "\n".join(blocks))
    return "\n\n".join(parts)
