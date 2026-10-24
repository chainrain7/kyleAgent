from __future__ import annotations

from pathlib import Path

import pytest
from evals.cassette import (
    CassetteEntry,
    CassetteExhaustedError,
    CassetteProvider,
    CassetteRecorder,
    load_cassette,
    save_cassette,
    to_entry,
    to_response,
)

from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.types import LlmResponse, ToolCallBlock, UsageStats


# 功能：LlmResponse → cassette 条目 → LlmResponse 往返无损
# 设计：构造含 tool_calls/thinking/usage 的完整响应,断言序列化字段逐一还原
def test_entry_response_roundtrip() -> None:
    resp = LlmResponse(
        stop_reason="tool_use",
        tool_calls=[ToolCallBlock(id="t1", name="bash", input={"command": "echo hi"})],
        text="calling",
        thinking_blocks=[{"type": "thinking", "thinking": "plan", "signature": "s"}],
        usage=UsageStats(input_tokens=10, output_tokens=5, cache_read_input_tokens=3),
    )
    entry = to_entry(resp, model="m1", strategy="rule_based")
    assert entry.model == "m1" and entry.strategy == "rule_based"
    back = to_response(entry)
    assert back.stop_reason == "tool_use"
    assert back.tool_calls[0].input == {"command": "echo hi"}
    assert back.thinking_blocks[0]["thinking"] == "plan"
    assert back.usage is not None and back.usage.cache_read_input_tokens == 3


# 功能：cassette JSON 文件保存/加载往返一致
# 设计：两条条目存取后字段与顺序都不变
def test_cassette_file_roundtrip(tmp_path: Path) -> None:
    entries = [
        CassetteEntry(model="m", stop_reason="tool_use", text="", tool_calls=[
            {"id": "t1", "name": "read_file", "input": {"path": "a"}}
        ]),
        CassetteEntry(model="m", stop_reason="end_turn", text="done"),
    ]
    path = tmp_path / "task.json"
    save_cassette(path, entries, task_id="demo")
    loaded = load_cassette(path)
    assert loaded == entries


# 功能：CassetteProvider 按顺序回放并在耗尽时报错
# 设计：两条条目第三次调用必须抛 CassetteExhaustedError——轨迹发散信号
async def test_provider_sequential_replay_and_exhaustion() -> None:
    provider = CassetteProvider([
        CassetteEntry(stop_reason="tool_use", tool_calls=[
            {"id": "t1", "name": "bash", "input": {}}
        ]),
        CassetteEntry(stop_reason="end_turn", text="done"),
    ])
    r1 = await provider.chat([], [], EventBus(), "r1")
    assert r1.stop_reason == "tool_use"
    r2 = await provider.chat([], [], EventBus(), "r1")
    assert r2.text == "done"
    with pytest.raises(CassetteExhaustedError):
        await provider.chat([], [], EventBus(), "r1")


# 功能：CassetteRecorder 包装真实 provider 并按序记录响应
# 设计：fake provider 返回两条固定响应;记录条数与顺序一致,且透传原始响应
async def test_recorder_records_in_order() -> None:
    class _Fake:
        def __init__(self) -> None:
            self.n = 0

        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.n += 1
            return LlmResponse(stop_reason="end_turn", text=f"r{self.n}")

    recorder = CassetteRecorder(_Fake())
    bus = EventBus()
    a = await recorder.chat([], [], bus, "r", model="m1")
    b = await recorder.chat([], [], bus, "r", model="m2")
    assert (a.text, b.text) == ("r1", "r2")
    assert [e.text for e in recorder.entries] == ["r1", "r2"]
    assert [e.model for e in recorder.entries] == ["m1", "m2"]
