from __future__ import annotations

import asyncio
from pathlib import Path

from kyle_agent.oncall.faults import BY_ID
from kyle_agent.oncall.sim import SimEnv
from kyle_agent.oncall.tools import build_oncall_tools


# 按名取工具并调用的便捷封装
def _run(tools: list, name: str, params: dict) -> object:
    tool = next(t for t in tools if t.name == name)
    return asyncio.run(tool.invoke(params))


# 构造已注入指定剧本的环境与工具集
def _setup(tmp_path: Path, fault_id: str) -> tuple[SimEnv, list]:
    env = SimEnv.reset(tmp_path / "env")
    BY_ID[fault_id].inject(env)
    return env, build_oncall_tools(tmp_path / "env")


# 功能:只读四件套在故障环境下的输出可用性
# 设计:S12 注入后 status 显示 degraded、logs 命中 retr_interval、
#       metrics 显示 step change、alerts 列出 firing——LLM 的四类证据源
async def test_readonly_tools_surface_evidence(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "orders-config-typo-errors")

    status = await tools[1].invoke({"service": "orders"})
    assert "degraded" in status.content and "externals" in status.content

    logs = await tools[2].invoke(
        {"service": "orders", "keyword": "retr_interval", "level": "ERROR"}
    )
    assert "retr_interval" in logs.content

    metrics = await tools[3].invoke({"service": "orders", "metric": "error_rate"})
    assert "step change" in metrics.content

    alerts = await tools[0].invoke({})
    assert "firing" in alerts.content and "500" in alerts.content


# 功能:runbook_search 按症状检索到对应手册
# 设计:中文/英文混合查询都能命中目标篇目(标题加分项),返回含处置章节
async def test_runbook_search_relevance(tmp_path: Path) -> None:
    _, tools = _setup(tmp_path, "gateway-mem-leak")
    hit = await tools[4].invoke({"query": "内存 上涨 OOM"})
    assert "内存泄漏" in hit.content and "处置" in hit.content
    miss = await tools[4].invoke({"query": "zzz_unrelated_zzz"})
    assert "no matching runbook" in miss.content


# 功能:service_restart 精确恢复——清除匹配故障、回基线、告警 resolved
# 设计:S12(orders 坏配置)重启后 orders faults 清空状态 running、
#       告警转 resolved;再重启一次返回幂等提示
async def test_restart_recovers_and_idempotent(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "orders-config-typo-errors")
    r1 = await tools[5].invoke({"service": "orders"})
    assert "cleared fault" in r1.content
    state = env.read_state()
    assert state["orders"]["status"] == "running"
    assert state["orders"]["faults"] == []
    assert env.read_alerts()[0]["status"] == "resolved"

    r2 = await tools[5].invoke({"service": "orders"})
    assert "already running" in r2.content


# 功能:传导恢复——重启根因服务后,无故障的调用方自动复位
# 设计:S7 根因在 orders、表象在 gateway;restart orders 后 gateway
#       (自身无 fault 仅传导 degraded)也回 running——"治根因"语义
async def test_restart_cascade_recovers_caller(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "gateway-dep-orders-down")
    await tools[5].invoke({"service": "orders"})
    state = env.read_state()
    assert state["orders"]["status"] == "running"
    assert state["gateway"]["status"] == "running"


# 功能:误操作不越权恢复——重启告警方不能清除其传导故障以外的标记
# 设计:S7 中 gateway 无 fault(纯传导),restart gateway 后 orders
#       仍 down、gateway 状态依旧非 running(依赖仍病)——防止"重启表象服务"
#       被误判为有效处置
async def test_restart_wrong_service_keeps_fault(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "gateway-dep-orders-down")
    await tools[5].invoke({"service": "gateway"})
    state = env.read_state()
    assert state["orders"]["status"] == "down"
    assert state["orders"]["faults"] == ["gateway-dep-orders-down"]


# 功能:scale_up 恢复容量类故障并受副本上限约束
# 设计:S5 scale gateway 后故障清除;连续扩容到 6 后再扩报错
async def test_scale_up_recovery_and_limit(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "gateway-cpu-saturation")
    r = await tools[6].invoke({"service": "gateway", "delta": 2})
    assert "replicas -> 4" in r.content and "cleared fault" in r.content
    assert env.read_state()["gateway"]["status"] == "running"

    await tools[6].invoke({"service": "gateway", "delta": 4})
    r2 = await tools[6].invoke({"service": "gateway", "delta": 1})
    assert r2.is_error and "limit" in r2.content


# 功能:scale_up 不能恢复 crash 类故障(动作-故障类型错配防护)
# 设计:S2(crashloop,期望 restart)扩容后 fault 保留——工具语义
#       与剧本期望动作精确匹配,防止"什么都用扩容试一下"
async def test_scale_up_does_not_clear_crash_fault(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "orders-crashloop-badconfig")
    await tools[6].invoke({"service": "orders", "delta": 1})
    state = env.read_state()
    assert "orders-crashloop-badconfig" in state["orders"]["faults"]
    assert state["orders"]["status"] == "crashloop"


# 功能:log_cleanup 恢复磁盘类故障,保留近端日志
# 设计:S4 清理后 disk 故障清除、状态 running;日志文件仍在(截断)
async def test_log_cleanup_recovers_disk_fault(tmp_path: Path) -> None:
    env, tools = _setup(tmp_path, "orders-disk-full")
    r = await tools[7].invoke({"service": "orders"})
    assert "freed" in r.content
    state = env.read_state()
    assert state["orders"]["status"] == "running"
    assert state["orders"]["faults"] == []
    assert (env.dir / "logs" / "orders.log").exists()


# 功能:submit_diagnosis 记录结构化结论,参数越界由校验层拒绝
# 设计:合法输入返回确认文本;confidence 越界抛 ValidationError——
#       与 bash 等内置工具一致,参数校验异常统一由 invocation 层捕获
async def test_submit_diagnosis(tmp_path: Path) -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    _, tools = _setup(tmp_path, "gateway-mem-leak")
    ok = await tools[8].invoke({
        "root_cause": "gateway memory leak",
        "recommended_actions": [{"tool": "service_restart",
                                 "params": {"service": "gateway"}}],
        "confidence": 0.9, "evidence": "OOMKilled x1",
    })
    assert "diagnosis recorded" in ok.content and "0.9" in ok.content

    with _pytest.raises(ValidationError):
        await tools[8].invoke({"root_cause": "x", "confidence": 1.5})


# 功能:未知服务名统一返回 schema_error
# 设计:所有工具对 unknown service 的容错一致,LLM 能自我纠正
async def test_unknown_service_errors(tmp_path: Path) -> None:
    _, tools = _setup(tmp_path, "gateway-mem-leak")
    for name, params in [
        ("service_status", {"service": "nope"}),
        ("logs_query", {"service": "nope"}),
        ("metrics_query", {"service": "nope"}),
        ("service_restart", {"service": "nope"}),
    ]:
        r = await _pick(tools, name, params)
        assert r.is_error, name


# 按名调用工具
async def _pick(tools: list, name: str, params: dict) -> object:
    tool = next(t for t in tools if t.name == name)
    return await tool.invoke(params)


# 功能:oncall 工具权限策略——读类/结论放行,变更类强制 ASK
# 设计:注册表层面锁定安全语义;evaluate 与 DEFAULT_POLICIES 双通道一致
def test_oncall_permission_policies() -> None:
    from kyle_agent.core.permissions.policy import (
        DEFAULT_POLICIES,
        PermissionDecision,
        evaluate,
    )

    for name in ("alerts_list", "service_status", "logs_query",
                 "metrics_query", "runbook_search", "submit_diagnosis"):
        assert DEFAULT_POLICIES[name].default is PermissionDecision.ALLOW, name
        assert evaluate(name, {}) is PermissionDecision.ALLOW, name
    for name in ("service_restart", "scale_up", "log_cleanup"):
        assert DEFAULT_POLICIES[name].default is PermissionDecision.ASK, name
        assert evaluate(name, {"service": "orders"}) is PermissionDecision.ASK, name


# 功能:runner 注册开关——[oncall] enabled 时领域工具入注册表
# 设计:CapturingProvider 截获 tool_schemas;enabled 后含 service_restart
#       与 submit_diagnosis,关闭时不含;默认关闭零行为变化
async def test_runner_registers_oncall_tools_when_enabled(tmp_path: Path) -> None:
    from kyle_agent.core.config import KyleConfig
    from kyle_agent.core.llm.types import LlmResponse
    from kyle_agent.core.runner import AgentRunner

    class _Cap:
        def __init__(self) -> None:
            self.names: list[str] = []

        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.names = [str(s.get("name")) for s in tool_schemas]
            return LlmResponse(stop_reason="end_turn", text="ok")

    env_dir = tmp_path / "oncall-env"
    from kyle_agent.oncall.sim import SimEnv as _Sim
    _Sim.reset(env_dir)

    cfg = KyleConfig()
    cfg.memory.enabled = False
    cfg.oncall.enabled = True
    cfg.oncall.env_dir = str(env_dir)
    provider_on = _Cap()
    await AgentRunner(cfg, provider=provider_on, runs_dir=tmp_path / "runs").run_and_capture("g")
    assert "service_restart" in provider_on.names
    assert "submit_diagnosis" in provider_on.names
    assert "alerts_list" in provider_on.names

    cfg2 = KyleConfig()
    cfg2.memory.enabled = False  # 默认 oncall.enabled=False
    provider_off = _Cap()
    await AgentRunner(cfg2, provider=provider_off, runs_dir=tmp_path / "runs2").run_and_capture("g")
    assert "service_restart" not in provider_off.names
    assert "submit_diagnosis" not in provider_off.names
