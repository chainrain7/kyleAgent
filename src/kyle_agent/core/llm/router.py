"""模型路由器:按调用特征选择 planning / execution 档位的模型。

设计动机(改造 A):config 的 llm.router 之前是死字段——所有调用永远打同一个
模型。规则式路由让"规划/复杂任务"走大模型、"执行/简单问答/摘要压缩"走便宜
模型,在不损失任务质量的前提下降低 token 成本;每次选择的模型与策略通过
LlmModelSelectedEvent 上报,trace 与 metrics 可回放验证。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kyle_agent.core.config import LlmConfig

# 命中任一特征即视为复杂任务(需要大模型规划);列表刻意保持短且可解释
_COMPLEX_HINTS: tuple[str, ...] = (
    "```", "def ", "class ", "函数", "重构", "实现", "修复", "bug", "调试",
    "架构", "设计", "multi", "agent", "规划",
)
# 目标文本短于该长度且无复杂特征时视为简单问答,直接用便宜模型
_SIMPLE_GOAL_MAX_CHARS = 120


@dataclass(frozen=True)
class RouterDecision:
    # 本次调用应使用的模型名
    model: str
    # 决策原因,用于日志与人工核验
    reason: str


class ModelRouter(Protocol):
    # 路由器协议:loop/compactor 只依赖 select 与 strategy,不依赖具体实现
    strategy: str

    # 依据步次/消息历史/用途选择模型
    def select(
        self,
        *,
        step: int,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        purpose: str = "chat",
    ) -> RouterDecision: ...


class StaticRouter:
    """永远返回默认模型,与改造前行为完全一致(strategy=static)。"""

    strategy = "static"

    # 初始化默认模型
    def __init__(self, default_model: str) -> None:
        self._default = default_model

    # 返回默认模型
    def select(
        self,
        *,
        step: int,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        purpose: str = "chat",
    ) -> RouterDecision:
        return RouterDecision(model=self._default, reason="static")


class RuleBasedRouter:
    """规则式路由:规划步/复杂目标走 planning 模型,执行步/简单问答/压缩走 execution 模型。

    规则(可解释、可在 trace 中核验):
    1. purpose == "compact":压缩摘要属于执行类工作 → execution
    2. 首次调用(历史中无 tool_result):目标简短且无复杂特征 → execution,否则 → planning
    3. 后续步(已有 tool_result 回填):执行跟进 → execution
    """

    strategy = "rule_based"

    # 两个档位的模型名;空字符串回退 default_model(单模型配置下退化为静态)
    def __init__(self, planning_model: str, execution_model: str, default_model: str) -> None:
        self._planning = planning_model or default_model
        self._execution = execution_model or default_model

    # 依据 step / 消息历史 / 用途选择模型
    def select(
        self,
        *,
        step: int,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        purpose: str = "chat",
    ) -> RouterDecision:
        if purpose == "compact":
            return RouterDecision(model=self._execution, reason="compact-summary")
        if not _has_tool_result(messages):
            if _is_simple_goal(messages):
                return RouterDecision(model=self._execution, reason="simple-goal")
            return RouterDecision(model=self._planning, reason="planning")
        return RouterDecision(model=self._execution, reason="execution")


# 判断消息历史中是否已有工具结果回填(区分规划步与执行步)
def _has_tool_result(messages: list[dict[str, object]]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


# 判断当前目标是否为简单问答(短文本且无复杂任务特征)
def _is_simple_goal(messages: list[dict[str, object]]) -> bool:
    user_text = ""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            user_text = content
            break
    if len(user_text) > _SIMPLE_GOAL_MAX_CHARS:
        return False
    return not any(hint in user_text for hint in _COMPLEX_HINTS)


class UnsupportedRouterError(ValueError):
    # 配置了未实现的路由策略时抛出,启动期 fail-fast
    pass


# 根据 LlmConfig 构建路由器;未实现策略抛 UnsupportedRouterError
def build_router(config: LlmConfig) -> ModelRouter:
    if config.router == "static":
        return StaticRouter(config.default_model)
    if config.router == "rule_based":
        return RuleBasedRouter(
            planning_model=config.planning_model,
            execution_model=config.execution_model,
            default_model=config.default_model,
        )
    raise UnsupportedRouterError(
        f"llm.router={config.router!r} is reserved but not implemented "
        f"(supported: static, rule_based)"
    )
