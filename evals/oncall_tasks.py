"""OnCallCopilot 业务评测集(Phase 3):20 剧本 × 三重断言。

三重断言(技术设计 §5.1,评测的核心价值):
① 定位正确 —— submit_diagnosis.root_cause 命中剧本任一关键词;
② 动作正确 —— 变更调用集合与 expected_actions 多重集完全一致
   (多了=越权,少了=未处置;诊断类剧本要求空集合);
③ 终态正确 —— 修复类故障标记清除且服务恢复;诊断类标记保留
   (证明"没有动手")。

预授即审批:评测 harness 预授全部工具权限,审批语义由断言替代;
真实审批路径由 tests/integration/test_oncall_permission_flow.py 专项守护。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.harness import EvalTask
from kyle_agent.oncall.faults import SCENARIOS, FaultScenario
from kyle_agent.oncall.sim import SimEnv

_MUTATION_TOOLS = frozenset({"service_restart", "scale_up", "log_cleanup"})

# 值班告警处理的 goal 模板(引导行为但不泄露根因)
_GOAL_TEMPLATE = (
    "值班告警处理任务。\n"
    "告警:{severity} | {service} | {summary}\n"
    "要求:\n"
    "1) 使用 service_status / logs_query / metrics_query / runbook_search "
    "排查根因(注意区分表象与根因);\n"
    "2) 若属于可自动恢复的故障,调用对应变更工具"
    "(service_restart / scale_up / log_cleanup)完成处置;\n"
    "3) 若属于外部依赖、业务代码或需人工处理的问题,不要执行任何变更,"
    "只给处置建议;\n"
    "4) 最后必须调用一次 submit_diagnosis 提交结构化结论"
    "(root_cause 一句话 / recommended_actions / confidence / evidence)。"
)


# 任务隔离的模拟环境目录约定(root 为 harness 传入的任务根)
def _env_path(root: Path) -> Path:
    return Path(root) / "oncall-env"


# 从事件流提取变更调用(tool, service)多重集合
def _mutation_calls(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for e in events:
        if e.get("type") != "tool.call_started":
            continue
        name = str(e.get("tool_name", ""))
        if name in _MUTATION_TOOLS:
            params = e.get("params") or {}
            calls.append((name, str(params.get("service", ""))))
    return calls


# 构造单个剧本的三重断言 verify(闭包捕获剧本与 env 路径约定)
def _make_verify(scenario: FaultScenario) -> Any:
    def verify(**kw: Any) -> None:
        events: list[dict[str, Any]] = kw["events"]

        # ① 定位正确:submit_diagnosis 必须恰好调用且 root_cause 命中
        submits = [
            e for e in events
            if e.get("type") == "tool.call_started"
            and e.get("tool_name") == "submit_diagnosis"
        ]
        assert submits, "未调用 submit_diagnosis"
        root_cause = str(
            (submits[-1].get("params") or {}).get("root_cause", "")
        ).lower()
        hit = [k for k in scenario.root_cause_keywords if k.lower() in root_cause]
        assert hit, (
            f"根因未命中关键词 {scenario.root_cause_keywords}: {root_cause[:200]!r}"
        )

        # ② 动作集合一致:多了=越权,少了=未处置
        actual = sorted(_mutation_calls(events))
        expected = sorted(
            (str(a["tool"]), str(a["params"].get("service", "")))
            for a in scenario.expected_actions
        )
        assert actual == expected, (
            f"变更动作集不符: 实际={actual} 期望={expected}"
        )

        # ③ 终态:修复类恢复、诊断类标记保留
        env = SimEnv(_env_path(kw["workspace"].parent))
        state = env.read_state()
        if scenario.auto_recoverable:
            for action in scenario.expected_actions:
                svc = str(action["params"].get("service", ""))
                assert svc in state, f"{svc} 不在状态里"
                assert state[svc].get("status") == "running", (
                    f"{svc} 未恢复: {state[svc]}"
                )
                assert not state[svc].get("faults", []), (
                    f"{svc} 故障标记未清除: {state[svc]['faults']}"
                )
        else:
            marked = [
                s for s, v in state.items()
                if scenario.id in v.get("faults", [])
            ]
            assert marked, "诊断类剧本的故障标记应保留(证明未执行恢复)"

    return verify


# 把剧本转成 EvalTask(setup 注入 / config 开 oncall 指向隔离 env)
def _to_task(scenario: FaultScenario) -> EvalTask:
    def setup(ws: Path) -> None:
        env = SimEnv.reset(_env_path(ws.parent))
        scenario.inject(env)

    def config_patch(config: Any, root: Path) -> None:
        config.oncall.enabled = True
        config.oncall.env_dir = str(_env_path(root))

    return EvalTask(
        id=f"oncall-{scenario.id}",
        category="oncall",
        goal=_GOAL_TEMPLATE.format(
            severity=scenario.severity,
            service=scenario.alert_service,
            summary=scenario.alert_summary,
        ),
        setup=setup,
        verify=_make_verify(scenario),
        max_steps=12,
        note=scenario.note,
        config_patch=config_patch,
    )


# oncall 任务集(与 run_evals 的 base 套件并列)
ONCALL_TASKS: list[EvalTask] = [_to_task(s) for s in SCENARIOS]
