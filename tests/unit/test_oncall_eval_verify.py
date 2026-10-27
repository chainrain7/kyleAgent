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
    workspace.mkdir(parents=True)
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


# 功能:任务集完整性——20 条 oncall 任务、id 前缀、goal 含告警文本
# 设计:与 SCENARIOS 一一对应;goal 不泄露根因(只含表象告警)
def test_oncall_task_registry() -> None:
    assert len(ONCALL_TASKS) == len(SCENARIOS) == 20
    for task, scenario in zip(ONCALL_TASKS, SCENARIOS, strict=True):
        assert task.id == f"oncall-{scenario.id}"
        assert scenario.alert_summary in task.goal
        # goal 不应包含根因关键词类内容(那是要 LLM 自己查的)
        for kw in ("retr_interval", "OOMKilled", "ConnectionPool"):
            assert kw not in task.goal
