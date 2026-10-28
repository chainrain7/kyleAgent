"""OnCallCopilot 变更工具的真实审批流验证(Phase 3)。

评测 harness 预授了全部权限(审批语义由断言替代);本测试走相反路径:
不预授,fake provider 直接发起 service_restart,断言——
① service_restart 触发 permission.requested(策略 ASK 生效)
② allow_once 后工具真实执行且故障恢复
③ 诊断类路径:LLM 若对诊断类故障发起变更,同样必须过审批
(审批是最后一道闸,与剧本期望无关)。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from kyle_agent.core.config import KyleConfig
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.llm.types import LlmResponse, ToolCallBlock
from kyle_agent.core.permissions.manager import PermissionManager
from kyle_agent.core.runner import AgentRunner
from kyle_agent.oncall.sim import SimEnv


# 构造已注入故障的 env + 开启 oncall 的 config
def _setup(tmp_path: Path, fault_id: str) -> tuple[KyleConfig, Path]:
    from kyle_agent.oncall.faults import BY_ID

    env_dir = tmp_path / "oncall-env"
    env = SimEnv.reset(env_dir)
    BY_ID[fault_id].inject(env)
    cfg = KyleConfig()
    cfg.memory.enabled = False
    cfg.oncall.enabled = True
    cfg.oncall.env_dir = str(env_dir)
    return cfg, env_dir


# 功能:service_restart 未经预授必须走 ASK 审批,放行后真实恢复
# 设计:fake provider 第一步发 service_restart;事件回调收到
#       permission.requested 即回 allow_once;断言事件序列含
#       permission.requested、工具执行后故障标记清除
async def test_mutation_requires_approval_then_recovers(tmp_path: Path) -> None:
    cfg, env_dir = _setup(tmp_path, "orders-config-typo-errors")

    class _RestartProvider:
        def __init__(self) -> None:
            self.n = 0

        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.n += 1
            if self.n == 1:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(
                        id="t1", name="service_restart",
                        input={"service": "orders"},
                    )],
                )
            return LlmResponse(
                stop_reason="end_turn",
                text="diagnosis: bad config; restarted",
            )

    manager = PermissionManager()
    bus = EventBus()
    seen: list[str] = []

    async def collect(e: BaseModel) -> None:
        t = getattr(e, "type", "")
        seen.append(t)
        if t == "permission.requested":
            manager.respond(getattr(e, "tool_use_id", ""), "allow_once")

    bus.subscribe(collect)
    runner = AgentRunner(
        cfg, bus=bus, provider=_RestartProvider(),
        permission_manager=manager, runs_dir=tmp_path / "runs",
    )
    outcome = await runner.run_and_capture("handle alert")

    assert outcome.status == "success"
    assert "permission.requested" in seen
    state = SimEnv(env_dir).read_state()
    assert state["orders"]["status"] == "running"
    assert state["orders"]["faults"] == []


# 功能:审批拒绝时变更不执行,故障保留且 run 继续收尾
# 设计:同样场景回 deny_once;断言状态未被动、fault 保留——
#       审批是否真的挡住了执行(而非仅发事件)
async def test_mutation_denied_keeps_fault(tmp_path: Path) -> None:
    cfg, env_dir = _setup(tmp_path, "gateway-mem-leak")

    class _RestartProvider:
        def __init__(self) -> None:
            self.n = 0

        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.n += 1
            if self.n == 1:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(
                        id="t1", name="service_restart",
                        input={"service": "gateway"},
                    )],
                )
            return LlmResponse(stop_reason="end_turn", text="user denied; advised manual fix")

    manager = PermissionManager()
    bus = EventBus()

    async def collect(e: BaseModel) -> None:
        if getattr(e, "type", "") == "permission.requested":
            manager.respond(getattr(e, "tool_use_id", ""), "deny_once")

    bus.subscribe(collect)
    runner = AgentRunner(
        cfg, bus=bus, provider=_RestartProvider(),
        permission_manager=manager, runs_dir=tmp_path / "runs",
    )
    outcome = await runner.run_and_capture("handle alert")

    assert outcome.status == "success"  # 拒绝不终止 run
    state = SimEnv(env_dir).read_state()
    assert "gateway-mem-leak" in state["gateway"]["faults"]
    assert state["gateway"]["status"] == "degraded"
