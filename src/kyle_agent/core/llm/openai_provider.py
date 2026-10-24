"""OpenAI 兼容端点的 LLM Provider(改造 C)。

项目的消息/事件格式全线采用 Anthropic 形态(text / tool_use / tool_result /
thinking blocks)。本 provider 的职责是把这些格式在边界上翻译成 OpenAI
Chat Completions 形态(system / user / assistant / tool 消息、function
tool_calls),核心循环(AgentLoop/Context/Compactor)完全不感知差异。

支持任意 OpenAI 兼容端点:OPENAI_BASE_URL 指向
https://api.deepseek.com、本地 vLLM/Ollama 等均可。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from kyle_agent.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
# OpenAI 系模型的上下文窗口注册表(与 anthropic provider 的注册表同理)
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "deepseek-flash": 128_000,
    "deepseek-v4-pro": 128_000,
}

# Anthropic stop_reason ↔ OpenAI finish_reason 的映射表
_STOP_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 返回模型上下文窗口;未注册模型回退 200k
def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, 200_000)


# 把 Anthropic 形态的 assistant content blocks 翻译成一条 OpenAI assistant 消息
def _assistant_to_openai(content: object) -> dict[str, Any] | None:
    if isinstance(content, str):
        return {"role": "assistant", "content": content} if content else None
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            tool_calls.append({
                "id": str(block.get("id", "")),
                "type": "function",
                "function": {
                    "name": str(block.get("name", "")),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })
        # thinking 块无 OpenAI 等价物,丢弃(回传时 OpenAI 也不产生该块)
    if not text_parts and not tool_calls:
        return None
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# 把一条 Anthropic user 消息(文本或 tool_result 块列表)翻译成 OpenAI 消息列表
def _user_to_openai(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    out: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if block.get("type") == "tool_result":
            out.append({
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id", "")),
                "content": str(block.get("content", "")),
            })
        elif block.get("type") == "text":
            out.append({"role": "user", "content": str(block.get("text", ""))})
    return out


# 翻译整条 Anthropic 消息历史为 OpenAI 消息列表
def to_openai_messages(
    messages: list[dict[str, object]], system: str | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant":
            translated = _assistant_to_openai(content)
            if translated is not None:
                out.append(translated)
        elif role == "user":
            out.extend(_user_to_openai(content))
    return out


# 把 Anthropic 工具 schema 列表翻译成 OpenAI function 工具列表
def to_openai_tools(
    tool_schemas: list[dict[str, object]],
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for schema in tool_schemas:
        tools.append({
            "type": "function",
            "function": {
                "name": str(schema.get("name", "")),
                "description": str(schema.get("description", "")),
                "parameters": schema.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return tools


class OpenAIProvider:
    # 初始化 OpenAI 兼容客户端;http_client 可注入用于测试
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key and http_client is None:
            raise SystemExit(
                "OPENAI_API_KEY not set (required when llm.provider = \"openai\")"
            )
        self._model = model
        self._base_url = (base_url or os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL))
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=300.0,
        )

    # 流式调用 OpenAI Chat Completions,翻译格式并返回 Anthropic 形态的 LlmResponse
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        model: str | None = None,
        strategy: str = "static",
    ) -> LlmResponse:
        effective_model = model or self._model
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id, model=effective_model, strategy=strategy, ts=_now()
            )
        )

        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": to_openai_messages(messages, system),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        tools = to_openai_tools(tool_schemas)
        if tools:
            payload["tools"] = tools

        text_parts: list[str] = []
        finish_reason: str | None = None
        usage_raw: dict[str, Any] = {}
        # tool_call 增量按 index 聚合:首块给 id/name,后续块追加 arguments 片段
        tool_acc: dict[int, dict[str, Any]] = {}

        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"openai api error {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    log.debug("openai: skip non-json sse line: %r", data[:100])
                    continue
                if chunk.get("usage"):
                    usage_raw = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                token = delta.get("content")
                if token:
                    text_parts.append(token)
                    await bus.publish(
                        LlmTokenEvent(run_id=run_id, token=token, ts=_now())
                    )
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index", 0))
                    acc = tool_acc.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["name"] = fn["name"]
                    if fn.get("arguments"):
                        acc["arguments"] += fn["arguments"]

        usage = UsageStats(
            input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            context_pct=(
                int(usage_raw.get("prompt_tokens", 0) or 0) / _context_window(effective_model)
            ),
        )
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=usage.context_pct,
                ts=_now(),
            )
        )

        tool_calls: list[ToolCallBlock] = []
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            try:
                parsed_input: dict[str, object] = json.loads(acc["arguments"] or "{}")
            except json.JSONDecodeError:
                # 参数流拼装失败时兜底为空对象并记日志,保持消息配对可继续
                log.warning("openai: tool arguments parse failed for %s", acc["name"])
                parsed_input = {}
            tool_calls.append(
                ToolCallBlock(id=acc["id"] or f"call_{idx}", name=acc["name"], input=parsed_input)
            )

        stop_reason = _STOP_REASON_MAP.get(finish_reason or "stop", "end_turn")
        return LlmResponse(
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            text="".join(text_parts),
            usage=usage,
        )

