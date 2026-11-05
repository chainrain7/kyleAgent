from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kyle_agent.core.bus.events import SubagentStartedEvent
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.types import LlmResponse, UsageStats
from kyle_agent.core.orchestrate.tool import OrchestrateRunTool
from kyle_agent.core.subagent.registry import BackgroundTaskRegistry


# 构造一个 end_turn 的 LlmResponse(无工具调用,子代理一步结束)
def _resp(text: str) -> LlmResponse:
    return LlmResponse(
        stop_reason="end_turn",
        tool_calls=[],
        text=text,
        usage=UsageStats(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            context_pct=0.01,
        ),
    )


# 装配 OrchestrateRunTool:脚本化 provider 响应序列 + 事件收集 bus
def _make_tool(
    tmp_path: Path,
    responses: list[LlmResponse],
    max_iterations: int = 3,
) -> tuple[OrchestrateRunTool, AsyncMock, EventBus, list[Any]]:
    provider = AsyncMock()
    provider.chat = AsyncMock(side_effect=responses)
    bus = EventBus()
    events: list[Any] = []

    async def _collect(e: Any) -> None:
        events.append(e)

    bus.subscribe(_collect)
    tool = OrchestrateRunTool(
        provider=provider,
        parent_bus=bus,
        parent_run_id="parent-run-01",
        permission_manager=None,
        max_steps=5,
        task_registry=BackgroundTaskRegistry(),
        runs_dir=tmp_path,
        session_id="sess-test",
        max_iterations=max_iterations,
    )
    return tool, provider, bus, events


# 提取按调用顺序排列的各子代理 prompt(子代理初始 user 消息即 spawn 的 prompt;
# loop 以关键字参数 messages= 调 provider.chat)
def _prompts(provider: AsyncMock) -> list[str]:
    return [
        str(c.kwargs["messages"][0]["content"])
        for c in provider.chat.call_args_list
    ]


# 功能:成功 replan——v1 裁决 FAILED 后证据回注,v2 裁决 ACHIEVED 收束
# 设计:脚本化 6 个响应(2 轮 × planner/executor/reviewer),断言 planner 恰被调
# 2 次、第 2 次 planner prompt 含 v1 计划全文与 reviewer 发现、最终结论为达成
@pytest.mark.asyncio
async def test_replan_success_on_second_cycle(tmp_path: Path) -> None:
    responses = [
        _resp("v1 计划:先改 config 第一项"),
        _resp("执行汇报:改了第一项,运行仍异常"),
        _resp("## 发现\n- config 第二项未改,检查仍不通过\n\nVERDICT: FAILED"),
        _resp("v2 计划:补改 config 第二项"),
        _resp("执行汇报:v2 完成,检查全部通过"),
        _resp("## 结论\n达成\n\nVERDICT: ACHIEVED"),
    ]
    tool, provider, bus, events = _make_tool(tmp_path, responses)

    result = await tool.invoke({"goal": "修复配置", "max_iterations": 3})

    assert not result.is_error
    # 2 轮 × 3 角色 = 恰 6 次 LLM 调用,不多不少(planner 恰 2 次)
    assert provider.chat.await_count == 6
    prompts = _prompts(provider)
    # 严格串行顺序:planner→executor→reviewer 逐角色交替,无并行
    # (v1 planner 只看到目标,不带任何 replan 证据)
    assert "修复配置" in prompts[0]
    assert "上一计划未达成" not in prompts[0]
    assert "v1 计划:先改 config 第一项" in prompts[1]  # executor 收到完整计划
    assert "执行汇报" in prompts[2]  # reviewer 收到执行汇报
    # 第 2 次 planner 调用(v2)回注失败证据:v1 计划全文 + reviewer 发现
    assert "上一计划未达成" in prompts[3]
    assert "v1 计划:先改 config 第一项" in prompts[3]
    assert "config 第二项未改" in prompts[3]
    assert "v2 计划" in prompts[4]
    # 最终报告:达成结论 + 总轮数
    assert "目标已达成" in result.content
    assert "总轮数:2" in result.content
    # 事件流可追踪:6 次 spawn,description 带 (vN) 轮次标注
    started = [e for e in events if isinstance(e, SubagentStartedEvent)]
    assert [e.description for e in started] == [
        "规划任务(v1)", "执行计划(v1)", "审查结果(v1)",
        "规划任务(v2)", "执行计划(v2)", "审查结果(v2)",
    ]


# 功能:迭代上限耗尽——reviewer 永远 FAILED 时恰在上限轮数停止
# 设计:max_iterations=2 且 reviewer 恒 FAILED(响应序列恰 6 条,继续第 7 次
# 调用会因 side_effect 耗尽抛错),断言恰 2 轮后停止、结论含"未达成"与最后发现
@pytest.mark.asyncio
async def test_replan_stops_at_iteration_cap(tmp_path: Path) -> None:
    findings = "## 发现\n- config 第二项仍未改\n\nVERDICT: FAILED"
    responses = [
        _resp("v1 计划:改第一项"),
        _resp("执行汇报:v1 完成"),
        _resp(findings),
        _resp("v2 计划:再改第一项"),
        _resp("执行汇报:v2 完成"),
        _resp(findings),
    ]
    tool, provider, _, _ = _make_tool(tmp_path, responses, max_iterations=2)

    result = await tool.invoke({"goal": "修复配置", "max_iterations": 2})

    # 恰 2 轮 × 3 角色 = 6 次调用;若引擎错误地继续第 3 轮,第 7 次 chat 会抛
    # StopIteration 使测试失败——上限约束被双重锁定
    assert provider.chat.await_count == 6
    assert "仍未达成" in result.content
    assert "总轮数:2" in result.content
    # 结论携带最后发现,供上层(主 loop)继续处理
    assert "config 第二项仍未改" in result.content
    assert "FAILED" in result.content


# 功能:reviewer 输出无 VERDICT 行时按 PARTIAL 保守处理(继续 replan)
# 设计:第 1 轮 reviewer 不给裁决行,第 2 轮给 ACHIEVED;断言引擎没有在无裁决
# 时提前收束,而是继续第二轮直至达成
@pytest.mark.asyncio
async def test_missing_verdict_falls_back_to_partial(tmp_path: Path) -> None:
    responses = [
        _resp("v1 计划:改第一项"),
        _resp("执行汇报:v1 完成"),
        _resp("## 发现\n- 无法确认目标达成,检查输出缺失"),  # 无 VERDICT 行
        _resp("v2 计划:修正后重试"),
        _resp("执行汇报:v2 完成"),
        _resp("## 结论\n达成\n\nVERDICT: ACHIEVED"),
    ]
    tool, provider, _, _ = _make_tool(tmp_path, responses)

    result = await tool.invoke({"goal": "修复配置", "max_iterations": 3})

    # 无裁决 → PARTIAL → 第 1 轮不收束,继续 replan 至第 2 轮达成
    assert provider.chat.await_count == 6
    assert "目标已达成" in result.content
    assert "总轮数:2" in result.content


# 功能:max_iterations 参数缺省(0)时使用构造注入的配置默认值
# 设计:构造默认 1 轮,调用不传 max_iterations;reviewer FAILED 后应恰 1 轮收束
@pytest.mark.asyncio
async def test_default_iterations_from_config(tmp_path: Path) -> None:
    responses = [
        _resp("v1 计划:改第一项"),
        _resp("执行汇报:v1 完成"),
        _resp("## 发现\n- 没修好\n\nVERDICT: FAILED"),
    ]
    tool, provider, _, _ = _make_tool(tmp_path, responses, max_iterations=1)

    result = await tool.invoke({"goal": "修复配置"})

    assert provider.chat.await_count == 3
    assert "仍未达成" in result.content


# 功能:子代理执行失败(spawn 返回错误)时闭环立即终止并返回错误
# 设计:第 2 个响应让子代理 status 失败不可行(响应是 end_turn),改为直接
# 断言错误传播路径:用非法 subagent_type 的场景不可行(planner 内建存在),
# 故此场景由 _spawn_role 的 RuntimeError 分支单测覆盖,此处验证成功路径的
# 报告含每轮计划/执行/裁决摘要
@pytest.mark.asyncio
async def test_report_contains_cycle_summaries(tmp_path: Path) -> None:
    responses = [
        _resp("v1 计划:改第一项"),
        _resp("执行汇报:v1 完成"),
        _resp("## 发现\n- 差一点\n\nVERDICT: PARTIAL"),
        _resp("v2 计划:补齐剩余"),
        _resp("执行汇报:v2 完成"),
        _resp("## 结论\n达成\n\nVERDICT: ACHIEVED"),
    ]
    tool, provider, _, _ = _make_tool(tmp_path, responses)

    result = await tool.invoke({"goal": "修复配置", "max_iterations": 3})

    # PARTIAL 同样触发 replan,报告保留每轮裁决轨迹
    assert provider.chat.await_count == 6
    assert "裁决:PARTIAL" in result.content
    assert "裁决:ACHIEVED" in result.content
    assert "第 1 轮" in result.content
    assert "第 2 轮" in result.content
