from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from evals.oncall_tasks import ONCALL_TASKS

from kyle_agent.oncall.faults import BY_ID, SCENARIOS
from kyle_agent.oncall.sim import SimEnv


# 事件工厂:submit_diagnosis + 任意变更调用序列
def _events(root_cause: str, mutations: list[tuple[str, str]]) -> list[dict[str, Any]]:
    evts: list[dict[str, Any]] = []
    for tool, svc in mutations:
        evts.append({"type": "tool.call_started", "tool_name": tool,
                     "params": {"service": svc}})
    evts.append({"type": "tool.call_started", "tool_name": "submit_diagnosis",
                 "params": {"root_cause": root_cause}})
    return evts


# 构造 verify 执行环境(workspace.parent 约定 = 任务根)
def _kw(tmp_path: Path, fault_id: str, root_cause: str,
        mutations: list[tuple[str, str]]) -> dict[str, Any]:
    from evals.oncall_tasks import _env_path

    scenario = BY_ID[fault_id]
    root = tmp_path / "task"
    env = SimEnv.reset(_env_path(root))
    scenario.inject(env)
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)  # 同测试二次构造(多动作场景)
    return {
        "workspace": workspace,
        "events": _events(root_cause, mutations),
    }


# 功能:修复类剧本三重断言全过(定位命中+动作一致+终态恢复)
# 设计:S12 全绿路径——合成事件流 + 真实工具执行后的 env 终态,
#       验证 verify 判定逻辑本身正确(评测器的元测试)
async def test_verify_passes_on_good_run(tmp_path: Path) -> None:
    task = next(t for t in ONCALL_TASKS if t.id == "oncall-orders-config-typo-errors")
    kw = _kw(tmp_path, "orders-config-typo-errors",
             "发布引入坏配置 retr_interval 导致下单 500",
             [("service_restart", "orders")])
    # 模拟工具执行:应用恢复

    from kyle_agent.oncall.tools import build_oncall_tools
    tools = {t.name: t for t in build_oncall_tools(kw["workspace"].parent / "oncall-env")}
    await tools["service_restart"].invoke({"service": "orders"})
    task.verify(**kw)


# 功能:定位失败被断言抓住(根因答偏)
# 设计:root_cause 不含任何关键词 → AssertionError 带关键词列表
async def test_verify_catches_wrong_root_cause(tmp_path: Path) -> None:
    task = next(t for t in ONCALL_TASKS if t.id == "oncall-gateway-mem-leak")
    kw = _kw(tmp_path, "gateway-mem-leak", "磁盘满了放不下",
             [("service_restart", "gateway")])
    with pytest.raises(AssertionError, match="根因未命中"):
        task.verify(**kw)


# 功能:越权动作被抓(诊断类剧本执行了重启)
# 设计:S16(业务 bug,期望零变更)事件流含 service_restart → 断言红;
#       这是"不该动手时不动手"的机器判定
async def test_verify_catches_unauthorized_mutation(tmp_path: Path) -> None:
    task = next(t for t in ONCALL_TASKS if t.id == "oncall-payments-business-rounding")
    kw = _kw(tmp_path, "payments-business-rounding",
             "业务代码 rounding 计算差错",
             [("service_restart", "payments")])
    with pytest.raises(AssertionError, match="变更动作集不符"):
        task.verify(**kw)


# 功能:动作不足被抓(修复类只诊断不动手)
# 设计:S1 期望 restart gateway 但事件流无变更 → 断言红
async def test_verify_catches_missing_action(tmp_path: Path) -> None:
    task = next(t for t in ONCALL_TASKS if t.id == "oncall-gateway-mem-leak")
    kw = _kw(tmp_path, "gateway-mem-leak", "gateway memory leak(OOM)",
             [])
    with pytest.raises(AssertionError, match="变更动作集不符"):
        task.verify(**kw)


# 功能:诊断类零变更全绿(定位+建议,不动手)
# 设计:S9 外部 DB 慢:正确 root_cause + 空变更集合 + 标记保留 → pass
async def test_verify_passes_diagnosis_only(tmp_path: Path) -> None:
    task = next(t for t in ONCALL_TASKS if t.id == "oncall-orders-dep-db-slow")
    kw = _kw(tmp_path, "orders-dep-db-slow",
             "外部依赖 orders-db 慢查询导致全量变慢", [])
    task.verify(**kw)


# 功能:多动作剧本要求动作完全一致(S20 双扩容)
# 设计:只扩一个 → 红;两个都对 → 过(终态同时校验两个服务)
async def test_verify_multi_action_exact_match(tmp_path: Path) -> None:
    task = next(t for t in ONCALL_TASKS if t.id == "oncall-blackfriday-surge")
    kw_bad = _kw(tmp_path, "blackfriday-surge", "大促流量激增",
                 [("scale_up", "gateway")])
    with pytest.raises(AssertionError, match="变更动作集不符"):
        task.verify(**kw_bad)

    kw = _kw(tmp_path, "blackfriday-surge", "大促流量 surge",
             [("scale_up", "gateway"), ("scale_up", "orders")])

    from kyle_agent.oncall.tools import build_oncall_tools
    tools = {t.name: t for t in build_oncall_tools(kw["workspace"].parent / "oncall-env")}
    await tools["scale_up"].invoke({"service": "gateway"})
    await tools["scale_up"].invoke({"service": "orders"})
    task.verify(**kw)


# 功能:任务集完整性——60 条 oncall 任务、id 前缀、goal 含告警文本
# 设计:与 SCENARIOS 一一对应;goal 不泄露根因(只含表象告警)
def test_oncall_task_registry() -> None:
    assert len(ONCALL_TASKS) == len(SCENARIOS) == 60
    for task, scenario in zip(ONCALL_TASKS, SCENARIOS, strict=True):
        assert task.id == f"oncall-{scenario.id}"
        assert scenario.alert_summary in task.goal
        # goal 不应包含根因关键词类内容(那是要 LLM 自己查的)
        for kw in ("retr_interval", "OOMKilled", "ConnectionPool"):
            assert kw not in task.goal


# 功能:60 条剧本的人工排查估算参考值全部回填且为正
# 设计:MTTD/MTTR 对比的基线字段;缺失会让人工对比静默失真
def test_manual_triage_minutes_backfilled() -> None:
    assert all(s.manual_triage_minutes > 0 for s in SCENARIOS)


# 功能:抽查 6 条 XL 新剧本 inject 后可被 --reset 完整复位到基线
# 设计:注入只改动态文件(state/metrics/logs/alerts),reset 重建即基线;
#       覆盖 A/B/C 三层各 2 条(含复合双 fault 场景)
def test_xl_inject_reset_idempotent(tmp_path: Path) -> None:
    from kyle_agent.oncall.faults import _BASE

    picks = [
        "payments-mem-leak",          # A 单故障变体
        "gateway-connpool-exhaust",   # A 单故障变体
        "gateway-p99-risk-svc-latency",  # B 误导表象(两级传导)
        "payments-risk-cert-chain",   # B 误导表象(零变更)
        "composite-orders-disk-crashloop",  # C 复合(同服务双 fault)
        "composite-payments-cert-orders-disk",  # C 复合(跨服务双 fault)
    ]
    baseline = SimEnv.reset(tmp_path / "env0").read_state()
    assert len(_BASE) == 20  # 防御:基础层不被扩容污染
    for fid in picks:
        env = SimEnv.reset(tmp_path / "env")
        scenario = BY_ID[fid]
        scenario.inject(env)
        injected = env.read_state()
        assert any(s.get("faults") for s in injected.values()), fid
        SimEnv.reset(tmp_path / "env")
        restored = SimEnv(tmp_path / "env").read_state()
        for svc, snap in baseline.items():
            assert restored[svc]["status"] == snap["status"], fid
            assert restored[svc]["faults"] == [], fid


# 功能:全量 60 条剧本按各自期望动作序列执行后,三重断言全部可满足
# 设计:注册表自洽性闸门——任何新剧本若"期望动作无法恢复自身注入"或
#       "关键词不可命中"直接红灯;录制前零成本验证恢复语义闭环
async def test_all_scenarios_solvable_by_expected_actions(tmp_path: Path) -> None:
    from kyle_agent.oncall.tools import build_oncall_tools

    for task in ONCALL_TASKS:
        sid = task.id.removeprefix("oncall-")
        scenario = BY_ID[sid]
        env_dir = tmp_path / sid / "oncall-env"
        env = SimEnv.reset(env_dir)
        scenario.inject(env)
        tools = {t.name: t for t in build_oncall_tools(env_dir)}
        events: list[dict[str, Any]] = []
        for action in scenario.expected_actions:
            params: dict[str, Any] = {"service": action["params"]["service"]}
            if "delta" in action["params"]:
                params["delta"] = action["params"]["delta"]
            result = await tools[action["tool"]].invoke(params)
            assert not result.is_error, f"{sid}: {action['tool']} 执行失败"
            events.append({"type": "tool.call_started",
                           "tool_name": action["tool"], "params": params})
        events.append({
            "type": "tool.call_started", "tool_name": "submit_diagnosis",
            "params": {"root_cause": f"issue is {scenario.root_cause_keywords[0]}"},
        })
        workspace = tmp_path / sid / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        task.verify(workspace=workspace, events=events, outcome=None,
                    runs_root=tmp_path / sid, store=None, session=None)
