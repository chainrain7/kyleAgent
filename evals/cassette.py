"""Cassette 录制/回放(评测体系支柱 1)。

录制模式:CassetteRecorder 包住真实 provider,把每次 LlmResponse 按调用
顺序存成 JSON;回放模式:CassetteProvider 按顺序原样吐回,整条 agent 轨迹
即可离线、确定性重放(工具在本地真实重执行,工作区确定则轨迹一致)。

顺序匹配(而非按请求内容哈希)是刻意取舍:agent 轨迹里的工具输出含
时间戳/临时路径等易变字段,内容哈希在重放时会 miss;顺序匹配对"录制
与重放之间轨迹发散"的敏感度刚好——cassette 耗尽即报错,本身就是
非确定性检测器。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kyle_agent.core.bus.events import LlmModelSelectedEvent, LlmUsageEvent
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.provider import _context_window
from kyle_agent.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

logger = logging.getLogger(__name__)


class CassetteExhaustedError(RuntimeError):
    # 回放轨迹与录制轨迹发散:第 N 次调用超出了录制条数
    pass


@dataclass
class CassetteEntry:
    # 录制时该次调用的模型与策略(用于指标归因,回放行为不依赖它)
    model: str = ""
    strategy: str = "static"
    stop_reason: str = "end_turn"
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_blocks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


# 把 LlmResponse 转为可 JSON 序列化的 cassette 条目
def to_entry(response: LlmResponse, *, model: str = "", strategy: str = "static") -> CassetteEntry:
    usage = (
        {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": response.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
        }
        if response.usage
        else {}
    )
    return CassetteEntry(
        model=model,
        strategy=strategy,
        stop_reason=response.stop_reason,
        text=response.text,
        tool_calls=[
            {"id": tc.id, "name": tc.name, "input": dict(tc.input)}
            for tc in response.tool_calls
        ],
        thinking_blocks=[dict(b) for b in response.thinking_blocks],
        usage=usage,
    )


# 把 cassette 条目还原为 LlmResponse
def to_response(entry: CassetteEntry) -> LlmResponse:
    usage = None
    if entry.usage:
        u = entry.usage
        usage = UsageStats(
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0)),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0)),
        )
    return LlmResponse(
        stop_reason=entry.stop_reason,
        tool_calls=[
            ToolCallBlock(id=tc["id"], name=tc["name"], input=dict(tc["input"]))
            for tc in entry.tool_calls
        ],
        text=entry.text,
        usage=usage,
        thinking_blocks=[dict(b) for b in entry.thinking_blocks],
    )


# 序列化 cassette 到 JSON 文件
def save_cassette(path: Path, entries: list[CassetteEntry], *, task_id: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "entries": [asdict(e) for e in entries],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# 从 JSON 文件加载 cassette
def load_cassette(path: Path) -> list[CassetteEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [CassetteEntry(**e) for e in payload["entries"]]


class CassetteProvider:
    # 按调用顺序回放录制响应;耗尽即抛 CassetteExhaustedError
    def __init__(self, entries: list[CassetteEntry], *, model: str = "cassette") -> None:
        self._entries = entries
        self._pos = 0
        self._model = model

    # 返回第 pos 条录制响应并推进游标;发布 model_selected 事件保持事件流形状一致
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
        if self._pos >= len(self._entries):
            raise CassetteExhaustedError(
                f"cassette exhausted after {len(self._entries)} calls — "
                f"replay trajectory diverged from recording (call #{self._pos + 1})"
            )
        entry = self._entries[self._pos]
        self._pos += 1
        logger.debug(
            "cassette replay pos=%d/%d model=%s stop=%s",
            self._pos, len(self._entries), entry.model, entry.stop_reason,
        )
        # 回放录制事件(model_selected/usage),让回放报告保留真实成本口径
        now = datetime.now(UTC).isoformat()
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id, model=entry.model or self._model,
                strategy=entry.strategy or "static", ts=now,
            )
        )
        usage = entry.usage
        if usage:
            input_t = int(usage.get("input_tokens", 0) or 0)
            await bus.publish(
                LlmUsageEvent(
                    run_id=run_id,
                    input_tokens=input_t,
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                    cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                    cache_creation_input_tokens=int(
                        usage.get("cache_creation_input_tokens", 0) or 0
                    ),
                    context_pct=input_t / _context_window(entry.model or self._model),
                    ts=now,
                )
            )
        return to_response(entry)


class CassetteRecorder:
    # 包住真实 provider,把每次响应按顺序记录进 entries
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entries: list[CassetteEntry] = []

    # 调用真实 provider 并记录响应(含路由元数据)
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
        response = await self._inner.chat(
            messages, tool_schemas, bus, run_id,
            step=step, system=system, model=model, strategy=strategy,
        )
        self.entries.append(to_entry(response, model=model or "", strategy=strategy))
        return response
