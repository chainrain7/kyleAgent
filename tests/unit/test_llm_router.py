from __future__ import annotations

import pytest

from kyle_agent.core.config import LlmConfig
from kyle_agent.core.context import ExecutionContext
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.router import (
    RuleBasedRouter,
    StaticRouter,
    UnsupportedRouterError,
    build_router,
)
from kyle_agent.core.llm.types import LlmResponse
from kyle_agent.core.loop import AgentLoop
from kyle_agent.core.tools.registry import ToolRegistry


# 构造 rule_based 路由器(规划/执行双档)
def _router() -> RuleBasedRouter:
    return RuleBasedRouter(
        planning_model="big-model",
        execution_model="cheap-model",
        default_model="default-model",
    )


# 功能:static 路由器永远返回默认模型
# 设计:三种输入下断言 model 相同,锁定改造前的行为基线不回归
def test_static_router_always_default() -> None:
    router = StaticRouter("m0")
    for kwargs in (
        {"step": 1, "messages": [], "tool_schemas": []},
        {"step": 9, "messages": [{"role": "user", "content": "x"}], "tool_schemas": []},
    ):
        assert router.select(**kwargs).model == "m0"
    assert router.strategy == "static"


# 功能:compact 用途选择执行档(便宜模型)
# 设计:压缩摘要属执行类工作,断言 reason 标注 compact-summary 便于 trace 核验
def test_rule_based_compact_uses_execution_model() -> None:
    d = _router().select(step=0, messages=[], tool_schemas=[], purpose="compact")
    assert d.model == "cheap-model"
    assert d.reason == "compact-summary"


# 功能:简单短目标的首轮调用选择执行档
# 设计:短文本且无复杂特征 → 便宜模型,对应"1+1 用 flash"的场景
def test_rule_based_simple_goal_uses_execution_model() -> None:
    messages = [{"role": "user", "content": "1+1 等于几?"}]
    d = _router().select(step=1, messages=messages, tool_schemas=[])
    assert d.model == "cheap-model"
    assert d.reason == "simple-goal"


# 功能:复杂目标的首轮调用选择规划档(大模型)
# 设计:代码块特征与超长目标两种输入都应路由到 planning,reason=planning
def test_rule_based_complex_goal_uses_planning_model() -> None:
    router = _router()
    code_msgs = [{"role": "user", "content": "重构这段代码:\n```python\nprint(1)\n```"}]
    assert router.select(step=1, messages=code_msgs, tool_schemas=[]).model == "big-model"
    long_msgs = [{"role": "user", "content": "写一个" + "很详细的" * 40 + "报告"}]
    d = router.select(step=1, messages=long_msgs, tool_schemas=[])
    assert d.model == "big-model"
    assert d.reason == "planning"


# 功能:已有工具结果回填的执行步选择执行档
# 设计:消息历史含 tool_result 块 → execution,这是路由规则中最常命中的分支
def test_rule_based_execution_step_uses_execution_model() -> None:
    messages = [
        {"role": "user", "content": "重构这段代码:\n```python\nprint(1)\n```"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    d = _router().select(step=2, messages=messages, tool_schemas=[])
    assert d.model == "cheap-model"
    assert d.reason == "execution"


# 功能:planning/execution 档位为空时回退 default_model
# 设计:单模型配置下 rule_based 退化为静态行为,不会把空字符串发给 API
def test_rule_based_falls_back_to_default() -> None:
    router = RuleBasedRouter(planning_model="", execution_model="", default_model="m0")
    assert router.select(step=1, messages=[{"role": "user", "content": "重构代码"}],
                         tool_schemas=[]).model == "m0"
    assert router.select(step=0, messages=[], tool_schemas=[], purpose="compact").model == "m0"


# 功能:build_router 按配置构造对应策略,未支持策略 fail-fast
# 设计:static/rule_based 返回正确类型;cost_budget 等预留值抛 UnsupportedRouterError
def test_build_router_dispatch() -> None:
    cfg_static = LlmConfig(router="static", default_model="m0")
    assert isinstance(build_router(cfg_static), StaticRouter)
    cfg_rule = LlmConfig(
        router="rule_based", default_model="m0",
        planning_model="big", execution_model="cheap",
    )
    router = build_router(cfg_rule)
    assert isinstance(router, RuleBasedRouter)
    with pytest.raises(UnsupportedRouterError):
        build_router(LlmConfig(router="cost_budget"))


# 功能:AgentLoop 把路由决策的 model/strategy 透传给 provider.chat
# 设计:记录型 fake provider 捕获关键字参数;复杂目标应收到 planning 模型与
#       rule_based 策略,未配置路由时收到 None/static——防止路由只在配置层生效
class _RecordingProvider:
    # 初始化记录器,保存每次 chat 收到的 model/strategy
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    # 记录 model/strategy 参数并返回 end_turn
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
        self.calls.append((model, strategy))
        return LlmResponse(stop_reason="end_turn", text="done")


async def test_loop_forwards_router_decision() -> None:
    provider = _RecordingProvider()
    ctx = ExecutionContext(
        run_id="r1", goal="重构这段代码:\n```python\nprint(1)\n```", max_steps=3
    )
    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(),  # type: ignore[arg-type]
        router=RuleBasedRouter("big-model", "cheap-model", "m0"),
    )
    await loop.run(ctx)
    assert provider.calls == [("big-model", "rule_based")]


# 功能:未配置路由时 loop 保持旧行为(model=None, strategy=static)
# 设计:与路由启用路径互补,保证默认配置零行为变化
async def test_loop_without_router_keeps_static() -> None:
    provider = _RecordingProvider()
    ctx = ExecutionContext(run_id="r1", goal="hi", max_steps=3)
    loop = AgentLoop(provider, ToolRegistry(), EventBus())  # type: ignore[arg-type]
    await loop.run(ctx)
    assert provider.calls == [(None, "static")]
