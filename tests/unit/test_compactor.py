from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kyle_agent.core.compact.compactor import Compactor
from kyle_agent.core.context import ExecutionContext
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.types import LlmResponse, UsageStats


def _stub_provider(summary: str = "## 1. Original Goal\nTest\n## 2. Completed Steps\n- done") -> Any:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text=summary,
        usage=UsageStats(input_tokens=100, output_tokens=30),
    ))
    return provider


def _make_messages(n: int = 5) -> list[dict[str, Any]]:
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": "user message " + "x" * 200})
        msgs.append({"role": "assistant", "content": "assistant reply " + "y" * 200})
    return msgs


# 功能：验证 compact_messages 成功时 provider.chat 被调用一次且不传工具 schema
# 设计：stub provider 返回非空摘要，断言 chat 调用一次，tool_schemas=[]
def test_compact_messages_calls_provider(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    messages = _make_messages()

    result = asyncio.run(
        compactor.compact_messages(messages, provider)
    )

    assert result is not None
    provider.chat.assert_called_once()
    call_kwargs = provider.chat.call_args
    assert call_kwargs.kwargs.get("tool_schemas") == [] or call_kwargs.args[1] == []


# 功能：验证 compact_messages 返回的摘要文本来自 provider 响应
# 设计：stub provider 返回固定摘要字符串，断言 result.summary_text 等于该字符串
def test_compact_messages_returns_summary(tmp_path: Path) -> None:
    expected = "## 1. Original Goal\nDo X\n## 2. Completed\n- step one"
    provider = _stub_provider(summary=expected)
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")

    result = asyncio.run(
        compactor.compact_messages(_make_messages(), provider)
    )

    assert result is not None
    assert result.summary_text == expected


# 功能：验证 compact() 将 context.messages 替换为两条摘要消息对
# 设计：调用 compact() 后断言 messages 长度为 2，role 分别为 user/assistant
def test_compact_replaces_context_messages(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.run(compactor.compact(ctx, provider))

    assert len(ctx.messages) == 2
    assert ctx.messages[0]["role"] == "user"
    assert ctx.messages[1]["role"] == "assistant"


# 功能：验证 compact() 在 session 目录写入 summary_*.md 文件
# 设计：使用 tmp_path，调用 compact() 后检查目录内是否存在 summary_ 开头的文件
def test_compact_writes_summary_file(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.run(compactor.compact(ctx, provider))

    summary_files = list(tmp_path.glob("summary_*.md"))
    assert len(summary_files) == 1


# 功能：验证 compact() 成功后发布 ContextCompactedEvent 事件
# 设计：订阅 EventBus，收集事件，断言收到类型为 context.compacted 的事件
def test_compact_publishes_event(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    received: list[Any] = []

    async def handler(event: Any) -> None:
        received.append(event)

    bus.subscribe(handler)
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.run(compactor.compact(ctx, provider))

    types = [getattr(e, "type", None) for e in received]
    assert "context.compacted" in types


# 功能：验证 provider 抛异常时 context.messages 保持不变
# 设计：stub provider.chat 抛 RuntimeError，断言 compact() 返回 None 且 messages 未被修改
def test_compact_failure_preserves_context(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("LLM error"))
    bus = EventBus()
    compactor = Compactor(bus, tmp_path, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    original_messages = _make_messages()
    ctx.messages = list(original_messages)

    result = asyncio.run(compactor.compact(ctx, provider))

    assert result is None
    assert ctx.messages == original_messages


# 功能:结构化 JSON 压缩响应被解析为状态快照并渲染为 Markdown
# 设计:fake provider 返回围栏包裹的 JSON;断言 structured 非空、
#       summary_text 含 Goal/TODOs 渲染、session 目录同时落 md+json
async def test_structured_compaction_parse_and_persist(tmp_path: Path) -> None:
    from kyle_agent.core.compact.compactor import Compactor
    from kyle_agent.core.context import ExecutionContext
    from kyle_agent.core.events.bus import EventBus
    from kyle_agent.core.llm.types import LlmResponse

    class _JsonProvider:
        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            return LlmResponse(
                stop_reason="end_turn",
                text='```json\n{"goal": "fix bug", "completed": ["read code"], '
                     '"files": {"a.py": "fixed"}, "todos": ["run tests"], '
                     '"facts": ["magic=42"]}\n```',
            )

    compactor = Compactor(EventBus(), tmp_path, "sess-1")
    ctx = ExecutionContext(
        run_id="r1", goal="fix bug", max_steps=5,
        prefill_messages=[
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": "working"},
        ],
    )
    result = await compactor.compact(ctx, _JsonProvider())  # type: ignore[arg-type]
    assert result is not None and result.structured is not None
    assert result.structured["goal"] == "fix bug"
    assert "Goal: fix bug" in result.summary_text
    assert "- run tests" in result.summary_text
    assert ctx.messages[0]["content"] == result.summary_text
    files = list(tmp_path.glob("summary_*.md")) + list(tmp_path.glob("summary_*.json"))
    assert len(files) == 2


# 功能:非 JSON 响应回退纯文本摘要(兼容任意模型输出)
# 设计:fake provider 返回散文;structured 为 None,行为与旧版一致
async def test_structured_parse_fallback(tmp_path: Path) -> None:
    from kyle_agent.core.compact.compactor import (
        _parse_structured,
    )
    assert _parse_structured("just plain summary") is None
    assert _parse_structured('{"no_goal": 1}') is None
    assert _parse_structured('{"goal": "g"}') == {"goal": "g"}


# 功能:增量压缩保留最近 N 轮原文,仅压缩更早历史
# 设计:6 条消息(3 assistant 轮),keep=1 → 前缀 3 条被摘要、最后 1 轮
#       (assistant+user)原样保留;context.messages = 摘要对 + 尾部
async def test_incremental_compact_keeps_recent_turns(tmp_path: Path) -> None:
    from kyle_agent.core.compact.compactor import Compactor
    from kyle_agent.core.context import ExecutionContext
    from kyle_agent.core.events.bus import EventBus
    from kyle_agent.core.llm.types import LlmResponse

    class _SummaryProvider:
        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            # 只应收到前缀(3 条),不应包含尾部
            assert len(messages) == 1  # 压缩请求本身是单条 user
            return LlmResponse(stop_reason="end_turn", text="SUMMARY")

    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "latest"},
    ]
    compactor = Compactor(EventBus(), tmp_path, "sess-1", keep_recent_turns=1)
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5, prefill_messages=msgs)
    result = await compactor.compact(ctx, _SummaryProvider())  # type: ignore[arg-type]
    assert result is not None
    assert result.preserved_tail == msgs[-2:]
    assert ctx.messages[0]["content"] == "SUMMARY"
    assert ctx.messages[2:] == msgs[-2:]
    assert ctx.compacted is True


# 功能:会话太短时增量压缩跳过(无更早历史可压)
# 设计:只有 1 轮,keep=1 → compact 返回 None 且 context 未被改写
async def test_incremental_compact_skips_short_history(tmp_path: Path) -> None:
    from kyle_agent.core.compact.compactor import Compactor
    from kyle_agent.core.context import ExecutionContext
    from kyle_agent.core.events.bus import EventBus

    class _Never:
        async def chat(self, *a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("不应发起 LLM 调用")

    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    compactor = Compactor(EventBus(), tmp_path, "sess-1", keep_recent_turns=1)
    ctx = ExecutionContext(run_id="r1", goal="g", max_steps=5, prefill_messages=msgs)
    result = await compactor.compact(ctx, _Never())  # type: ignore[arg-type]
    assert result is None
    assert ctx.messages == msgs
