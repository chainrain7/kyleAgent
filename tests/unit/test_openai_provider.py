from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.openai_provider import (
    OpenAIProvider,
    to_openai_messages,
    to_openai_tools,
)
from kyle_agent.core.llm.types import LlmResponse

# --- 消息格式翻译 ---------------------------------------------------------------


# 功能：Anthropic 文本消息 + system 正确翻译为 OpenAI system/user 消息
# 设计：最简形态——字符串 content 直接透传,system 置于首位
def test_translate_simple_messages() -> None:
    out = to_openai_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "You are helpful.",
    )
    assert out == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


# 功能：assistant 的 tool_use 块翻译为 OpenAI tool_calls,thinking 块被丢弃
# 设计：混合 thinking/text/tool_use 的块列表是真实回传形态;断言 json 参数序列化与丢弃行为
def test_translate_assistant_tool_use_and_thinking() -> None:
    out = to_openai_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "plan", "signature": "s"},
                    {"type": "text", "text": "calling"},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "bash",
                        "input": {"command": "echo hi"},
                    },
                ],
            }
        ],
        None,
    )
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "calling"
    assert msg["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "echo hi"}'},
        }
    ]


# 功能：user 的 tool_result 块列表翻译为 OpenAI tool 角色消息
# 设计：Anthropic 把同批 tool_result 合并在一条 user 消息,OpenAI 要求逐条 tool 消息
def test_translate_tool_results() -> None:
    out = to_openai_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "out1"},
                    {"type": "tool_result", "tool_use_id": "call_2", "content": "out2"},
                ],
            }
        ],
        None,
    )
    assert out == [
        {"role": "tool", "tool_call_id": "call_1", "content": "out1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "out2"},
    ]


# 功能：工具 schema 从 Anthropic input_schema 翻译为 OpenAI function.parameters
# 设计：字段名映射 name/description/input_schema → function 三元组
def test_translate_tools() -> None:
    out = to_openai_tools(
        [{
            "name": "read_file",
            "description": "Read a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }]
    )
    assert out == [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]


# --- 流式 chat -----------------------------------------------------------------


# 构造一段 OpenAI SSE 响应字节流
def _sse(chunks: list[dict[str, Any]]) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    return ("\n".join(lines) + "\ndata: [DONE]\n\n").encode()


# 构造带 MockTransport 的 provider,handler 捕获请求体
def _provider(sse_body: bytes) -> tuple[OpenAIProvider, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=sse_body)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://mock.test/v1"
    )
    return OpenAIProvider("test-model", http_client=client), captured


async def _chat(
    provider: OpenAIProvider,
    messages: list[dict[str, object]] | None = None,
    tool_schemas: list[dict[str, object]] | None = None,
) -> tuple[LlmResponse, list[BaseModel]]:
    collected: list[BaseModel] = []
    bus = EventBus()

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    bus.subscribe(_collect)
    result = await provider.chat(
        messages=messages or [{"role": "user", "content": "hi"}],
        tool_schemas=tool_schemas or [],
        bus=bus,
        run_id="r1",
    )
    return result, collected


# 功能：文本流式响应拼接为 text,逐 token 发布事件,usage 正确解析
# 设计：两个 content 增量 + 末块 usage;断言事件顺序(model_selected → token×2 → usage)
async def test_streaming_text_and_usage() -> None:
    body = _sse([
        {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    ])
    provider, captured = _provider(body)
    result, events = await _chat(provider)

    assert result.stop_reason == "end_turn"
    assert result.text == "Hello"
    assert result.tool_calls == []
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 2
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types == ["llm.model_selected", "llm.token", "llm.token", "llm.usage"]
    assert captured["json"]["stream"] is True
    assert captured["json"]["stream_options"] == {"include_usage": True}


# 功能：tool_calls 增量流按 index 聚合,参数片段拼接为完整 JSON
# 设计：id/name 首块出现、arguments 分两片到达——覆盖最典型的增量模式;断言
#       stop_reason=tool_use 且 ToolCallBlock.input 已解析为 dict
async def test_streaming_tool_calls_assembly() -> None:
    body = _sse([
        {
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0, "id": "call_9", "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path": '},
                }]},
                "finish_reason": None,
            }]
        },
        {
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": '"a.txt", "content": "x"}'},
                }]},
                "finish_reason": None,
            }]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    provider, _ = _provider(body)
    result, _ = await _chat(provider)

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_9"
    assert tc.name == "write_file"
    assert tc.input == {"path": "a.txt", "content": "x"}


# 功能：finish_reason 映射 stop/tool_calls/length → end_turn/tool_use/max_tokens
# 设计：参数化三种结束原因,锁定映射表行为
@pytest.mark.parametrize(
    ("finish", "expected"),
    [("stop", "end_turn"), ("tool_calls", "tool_use"), ("length", "max_tokens")],
)
async def test_finish_reason_mapping(finish: str, expected: str) -> None:
    body = _sse([{"choices": [{"delta": {}, "finish_reason": finish}]}])
    provider, _ = _provider(body)
    result, _ = await _chat(provider)
    assert result.stop_reason == expected


# 功能：非 200 响应抛 RuntimeError 并带状态码与响应片段
# 设计：MockTransport 返回 401;断言异常消息可定位问题(便于排障)
async def test_api_error_raises_runtime_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error": "bad key"}')

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://mock.test/v1"
    )
    provider = OpenAIProvider("m", http_client=client)
    with pytest.raises(RuntimeError, match="401"):
        await _chat(provider)


# 功能：model/strategy 覆盖参数透传到请求体与事件(与 AnthropicProvider 行为一致)
# 设计：路由器兼容性——OpenAI 后端同样支持按调用换模型
async def test_model_strategy_override() -> None:
    body = _sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])
    provider, captured = _provider(body)
    bus = EventBus()
    events: list[BaseModel] = []

    async def _c(e: BaseModel) -> None:
        events.append(e)

    bus.subscribe(_c)
    await provider.chat(
        messages=[{"role": "user", "content": "x"}],
        tool_schemas=[],
        bus=bus,
        run_id="r1",
        model="routed-model",
        strategy="rule_based",
    )
    assert captured["json"]["model"] == "routed-model"
    sel = next(e for e in events if e.type == "llm.model_selected")  # type: ignore[attr-defined]
    assert sel.model == "routed-model"  # type: ignore[attr-defined]
    assert sel.strategy == "rule_based"  # type: ignore[attr-defined]
