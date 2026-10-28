"""OnCallCopilot 处置报告生成器(Phase 4)。

从一次 run 的 events.jsonl(权威事件流)+ 模拟环境终态渲染 Markdown 报告:
结论(submit_diagnosis)/处置(变更动作+审批决策)/结果(告警与状态)/
时间线(告警→查证→审批→执行→完成)/证据链(诊断引用的工具输出行)。

触发:runner 在 oncall.enabled 且 run 含 submit_diagnosis 时于 run 结束后调用;
无 submit_diagnosis 的普通 run 静默跳过(返回 None)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kyle_agent.oncall.sim import SimEnv

logger = logging.getLogger(__name__)

_MUTATION_TOOLS = frozenset({"service_restart", "scale_up", "log_cleanup"})
_MAX_EVIDENCE_LINES = 8


# 读取 events.jsonl 为事件 dict 列表(坏行跳过)
def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


# 渲染处置报告 Markdown;非 oncall run(无 submit_diagnosis)返回 None
def generate_report(run_dir: Path, env: SimEnv, run_id: str) -> Path | None:
    events = _load_events(run_dir)
    submits = [
        e for e in events
        if e.get("type") == "tool.call_started"
        and e.get("tool_name") == "submit_diagnosis"
    ]
    if not submits:
        return None

    diag = (submits[-1].get("params") or {})
    root_cause = str(diag.get("root_cause", ""))
    confidence = diag.get("confidence", "")
    evidence = str(diag.get("evidence", ""))

    # 变更动作与审批决策(tool_use_id → 决策)
    mutations: list[dict[str, Any]] = []
    decisions: dict[str, str] = {}
    outputs: dict[str, str] = {}
    for e in events:
        t = e.get("type", "")
        if t == "permission.granted":
            decisions[e.get("tool_use_id", "")] = str(e.get("decision", "allow"))
        elif t == "permission.denied":
            decisions[e.get("tool_use_id", "")] = str(e.get("decision", "deny"))
        elif t == "tool.call_started" and e.get("tool_name") in _MUTATION_TOOLS:
            mutations.append({
                "ts": e.get("ts", ""),
                "tool": e.get("tool_name", ""),
                "params": e.get("params") or {},
                "id": e.get("tool_use_id", ""),
            })
        elif t == "tool.call_finished":
            outputs[e.get("tool_use_id", "")] = str(e.get("output", ""))

    # 终态与告警
    state = env.read_state()
    alerts = env.read_alerts()
    resolved = [a for a in alerts if a.get("status") == "resolved"]
    unhealthy = {n: s for n, s in state.items() if s.get("status") != "running"}

    lines: list[str] = []
    lines.append(f"# 处置报告 · run {run_id}")
    lines.append("")
    lines.append(f"**结论**  {root_cause}(置信度 {confidence})")
    if mutations:
        acted = ";".join(
            f"{m['tool']} {m['params'].get('service', '')}"
            f"({decisions.get(m['id'], 'preapproved')})"
            for m in mutations
        )
        lines.append(f"**处置**  {acted}")
    else:
        lines.append("**处置**  未执行变更(诊断/建议类,零变更)")
    if resolved:
        lines.append("**结果**  " + "; ".join(
            f"{a['alert_id']} {a['service']} 已解决" for a in resolved
        ))
    else:
        lines.append("**结果**  无已解决告警(人工跟进)")
    if unhealthy:
        detail = "; ".join(f"{n}={s['status']}" for n, s in unhealthy.items())
        lines.append(f"**遗留**  {detail}")

    # 时间线:告警/查证/审批/执行/完成
    lines.append("")
    lines.append("## 时间线")
    for e in events:
        t = e.get("type", "")
        ts = str(e.get("ts", ""))[11:19] or "--:--:--"
        if t == "run.started":
            lines.append(f"- {ts} 诊断会话开始(goal: {str(e.get('goal', ''))[:60]})")
        elif t == "tool.call_started":
            name = e.get("tool_name", "")
            if name in _MUTATION_TOOLS or name == "submit_diagnosis":
                params = e.get("params") or {}
                target = params.get("service", params.get("root_cause", ""))
                lines.append(f"- {ts} `{name}` {str(target)[:70]}")
        elif t in ("permission.granted", "permission.denied"):
            lines.append(f"- {ts} 审批:{e.get('decision', '')}")

    # 证据链:诊断 evidence + 变更工具输出
    lines.append("")
    lines.append("## 证据链")
    if evidence:
        for ln in evidence.splitlines()[:_MAX_EVIDENCE_LINES]:
            if ln.strip():
                lines.append(f"> {ln.strip()}")
    for m in mutations:
        out = outputs.get(m["id"], "").strip()
        if out:
            first = out.splitlines()[0]
            lines.append(f"> [{m['tool']} 输出] {first[:100]}")

    report_dir = env.dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("oncall report generated: %s", path)
    return path
