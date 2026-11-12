#!/usr/bin/env python3
"""端到端效果评分聚合:从记录轮产物(events.jsonl)提取效果指标。

指标口径(与 metrics.md §8 一致,全部来自 record 轮单轮实测):
- Top-1 命中率:最后一条 submit_diagnosis.root_cause 小写子串命中
  root_cause_keywords 任一的任务占比;
- Top-3 命中率:ranked_candidates 前 3 条任一命中的任务占比
  (缺失时退化为 [root_cause];候选覆盖率单列:提交时携带
  ranked_candidates 的任务占比);
- 完整通过率:三重断言全过(= replay passed;来自 --results 的
  run_evals 结果 JSON,未提供时该指标输出 null);
- wall = run.finished − run.started;MTTD = 最后一条 submit_diagnosis
  完成 − run.started;MTTR = 最后一个变更工具(service_restart /
  scale_up / log_cleanup)完成 − run.started(仅 auto_recoverable);
- 人工对比:median(manual_triage_minutes,估算参考值)vs
  median(wall 换算分钟),加速比 = 人工 / wall,缩短 = 1 − wall/人工。

分组:全量 / 类别 / 难度层(基础 20 / 单故障变体 22 / 误导表象 10 /
复合 8)。离线确定性、零 Key,时间指标禁止来自回放(铁律 6)。

    uv run python scripts/eval_score.py --artifacts-dir evals/results/<ts>-record \
        --results evals/results/<ts>-replay.json --out evals/results/<ts>-score.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kyle_agent.oncall.faults import _BASE, SCENARIOS  # noqa: E402
from kyle_agent.oncall.faults_xl import SCENARIOS_XL  # noqa: E402

_MUTATION_TOOLS = frozenset({"service_restart", "scale_up", "log_cleanup"})
_TOP_K = 3

# 难度层划分(XL 模块内顺序即分层:A 变体 → B 误导 → C 复合)
_LAYER_BY_ID: dict[str, str] = {
    **{s.id: "基础" for s in _BASE},
    **{s.id: "单故障变体" for s in SCENARIOS_XL[:22]},
    **{s.id: "误导表象" for s in SCENARIOS_XL[22:32]},
    **{s.id: "复合故障" for s in SCENARIOS_XL[32:40]},
}


# 解析 ISO 时间串为 datetime(容错:缺失/坏格式返回 None)
def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# 读取 events.jsonl 为事件列表(坏行跳过)
def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


# 单任务评分行:命中/候选/时间轴一次解析(时间轴缺失容错为 None)
def score_task(scenario_id: str, artifacts_dir: Path,
               passed_by_task: dict[str, bool]) -> dict[str, Any]:
    scenario = next(s for s in SCENARIOS if s.id == scenario_id)
    row: dict[str, Any] = {
        "task_id": f"oncall-{scenario_id}",
        "scenario_id": scenario_id,
        "category": scenario.category,
        "layer": _LAYER_BY_ID.get(scenario_id, "未知"),
        "severity": scenario.severity,
        "auto_recoverable": scenario.auto_recoverable,
        "manual_triage_minutes": scenario.manual_triage_minutes,
    }
    events_path = artifacts_dir / f"oncall-{scenario_id}" / "events.jsonl"
    if not events_path.exists():
        return {**row, "error": "events.jsonl 缺失", "top1_hit": None,
                "top3_hit": None, "has_candidates": False,
                "wall_s": None, "mttd_s": None, "mttr_s": None}

    events = _load_events(events_path)
    submits = [
        e for e in events
        if e.get("type") == "tool.call_started"
        and e.get("tool_name") == "submit_diagnosis"
    ]
    finishes = {
        (e.get("tool_use_id"), e.get("tool_name")): e
        for e in events if e.get("type") == "tool.call_finished"
    }
    run_started = next(
        (e for e in events if e.get("type") == "run.started"), None)
    run_finished = next(
        (e for e in events if e.get("type") == "run.finished"), None)

    # 命中判定:最后一条 submit 的 root_cause + ranked_candidates 前 3
    root_cause = ""
    candidates: list[dict[str, Any]] = []
    if submits:
        params = submits[-1].get("params") or {}
        root_cause = str(params.get("root_cause", "")).lower()
        raw = params.get("ranked_candidates")
        if isinstance(raw, list):
            candidates = [c for c in raw[:_TOP_K] if isinstance(c, dict)]
    keywords = [k.lower() for k in scenario.root_cause_keywords]

    def _hit(text: str) -> bool:
        return any(k in text for k in keywords)

    top1 = _hit(root_cause)
    cand_texts = [str(c.get("root_cause", "")).lower() for c in candidates]
    # Top-3:候选缺失时退化为 [root_cause](口径见模块 docstring)
    top3_pool = cand_texts if cand_texts else [root_cause]
    top3 = any(_hit(t) for t in top3_pool)

    # 时间轴(秒);任一端点缺失则该指标为 None,聚合时跳过
    t0 = _parse_ts((run_started or {}).get("ts")) if run_started else None

    def _delta_s(end: datetime | None) -> float | None:
        return round((end - t0).total_seconds(), 3) if (t0 and end) else None

    wall_s = _delta_s(_parse_ts((run_finished or {}).get("ts"))
                      if run_finished else None)
    mttd_s = None
    if submits:
        last_submit_id = submits[-1].get("tool_use_id")
        fin = finishes.get((last_submit_id, "submit_diagnosis"))
        mttd_s = _delta_s(_parse_ts((fin or {}).get("ts")))
    mttr_s = None
    if scenario.auto_recoverable:
        mutation_fins = [
            e for e in events
            if e.get("type") == "tool.call_finished"
            and e.get("tool_name") in _MUTATION_TOOLS
        ]
        if mutation_fins:
            last = mutation_fins[-1]
            mttr_s = _delta_s(_parse_ts(last.get("ts")))

    passed = passed_by_task.get(f"oncall-{scenario_id}")
    return {
        **row,
        "top1_hit": top1,
        "top3_hit": top3,
        "has_candidates": bool(cand_texts),
        "n_candidates": len(cand_texts),
        "passed": passed,
        "root_cause": root_cause[:200],
        "candidate_causes": cand_texts,
        "wall_s": wall_s,
        "mttd_s": mttd_s,
        "mttr_s": mttr_s,
    }


# 分位数(最近秩法);空列表返回 None
def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(q * len(ordered)) - 1)
    return round(ordered[min(idx, len(ordered) - 1)], 1)


# 一组评分行的聚合(命中率/时间分位/人工对比)
def aggregate(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    scored = [r for r in rows if r.get("top1_hit") is not None]
    n = len(scored)
    top1 = sum(1 for r in scored if r["top1_hit"]) / n if n else None
    top3 = sum(1 for r in scored if r["top3_hit"]) / n if n else None
    coverage = (
        sum(1 for r in scored if r["has_candidates"]) / n if n else None
    )
    passed_flags = [r["passed"] for r in scored if r.get("passed") is not None]
    pass_rate = (
        sum(1 for p in passed_flags if p) / len(passed_flags)
        if passed_flags else None
    )
    walls = [r["wall_s"] for r in scored if r["wall_s"] is not None]
    mttds = [r["mttd_s"] for r in scored if r["mttd_s"] is not None]
    mttrs = [r["mttr_s"] for r in scored if r["mttr_s"] is not None]

    def _median(xs: list[float]) -> float | None:
        return round(float(statistics.median(xs)), 1) if xs else None

    manual_med = _median(
        [float(r["manual_triage_minutes"]) for r in scored])
    wall_med_min = (
        round(_median(walls) / 60, 1)  # type: ignore[operator]
        if walls else None
    )
    speedup = (
        round(manual_med / (wall_med_min or 1e9), 2)
        if manual_med and wall_med_min else None
    )
    reduction = (
        round(1 - wall_med_min / manual_med, 4)
        if manual_med and wall_med_min else None
    )
    return {
        "group": label,
        "n": n,
        "top1_hit_rate": round(top1, 4) if top1 is not None else None,
        "top3_hit_rate": round(top3, 4) if top3 is not None else None,
        "candidate_coverage": round(coverage, 4) if coverage is not None else None,
        "full_pass_rate": round(pass_rate, 4) if pass_rate is not None else None,
        "wall_s_median": _median(walls),
        "wall_s_p90": _pct(walls, 0.9),
        "mttd_s_median": _median(mttds),
        "mttd_s_p90": _pct(mttds, 0.9),
        "mttr_s_median": _median(mttrs),
        "mttr_s_p90": _pct(mttrs, 0.9),
        "manual_minutes_median_est": manual_med,
        "wall_minutes_median": wall_med_min,
        "speedup_vs_manual_est": speedup,
        "reduction_vs_manual_est": reduction,
    }


# 渲染聚合表为 Markdown(stdout 用)
def render_markdown(aggregates: list[dict[str, Any]]) -> str:
    header = (
        "| 分组 | n | Top-1 | Top-3 | 候选覆盖 | 完整通过 | wall中位/P90(s)"
        " | MTTD中位(s) | MTTR中位(s) | 人工min(估算) | 加速比 |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for a in aggregates:
        def _f(v: Any, scale: float = 1) -> str:
            return "—" if v is None else f"{v * scale:.2f}".rstrip("0").rstrip(".")

        lines.append(
            f"| {a['group']} | {a['n']} | {_f(a['top1_hit_rate'])} |"
            f" {_f(a['top3_hit_rate'])} | {_f(a['candidate_coverage'])} |"
            f" {_f(a['full_pass_rate'])} |"
            f" {a['wall_s_median'] or '—'}/{a['wall_s_p90'] or '—'} |"
            f" {a['mttd_s_median'] or '—'} | {a['mttr_s_median'] or '—'} |"
            f" {a['manual_minutes_median_est'] or '—'} |"
            f" {a['speedup_vs_manual_est'] or '—'} |"
        )
    return "\n".join(lines)


# CLI 入口:扫描 artifacts 目录并输出评分 JSON + Markdown 表
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="eval_score", description="端到端效果评分聚合(record 产物)")
    parser.add_argument("--artifacts-dir", type=Path, required=True,
                        help="run_evals --record 的 --artifacts-dir 目录")
    parser.add_argument("--results", type=Path, default=None,
                        help="run_evals 结果 JSON(replay/record),供完整通过率")
    parser.add_argument("--out", type=Path, default=None,
                        help="评分 JSON 输出路径(缺省 evals/results/<ts>-score.json)")
    args = parser.parse_args()

    artifacts_dir = args.artifacts_dir
    if not artifacts_dir.exists():
        print(f"error: artifacts dir 不存在: {artifacts_dir}", file=sys.stderr)
        return 1

    passed_by_task: dict[str, bool] = {}
    if args.results and args.results.exists():
        payload = json.loads(args.results.read_text(encoding="utf-8"))
        for r in payload.get("results", []):
            passed_by_task[str(r.get("task_id"))] = bool(r.get("passed"))

    rows = [
        score_task(s.id, artifacts_dir, passed_by_task) for s in SCENARIOS
    ]
    missing = [r["scenario_id"] for r in rows if r.get("error")]
    if missing:
        print(f"error: {len(missing)} 个任务缺产物: {missing[:5]} …",
              file=sys.stderr)
        return 1

    groups: list[dict[str, Any]] = [aggregate(rows, "全量")]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_category.setdefault(str(r["category"]), []).append(r)
    for cat in sorted(by_category):
        groups.append(aggregate(by_category[cat], f"类别:{cat}"))
    by_layer: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_layer.setdefault(str(r["layer"]), []).append(r)
    for layer in ("基础", "单故障变体", "误导表象", "复合故障"):
        if layer in by_layer:
            groups.append(aggregate(by_layer[layer], f"难度层:{layer}"))

    out = args.out or (
        REPO_ROOT / "evals" / "results"
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-score.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "artifacts_dir": str(artifacts_dir),
        "results_source": str(args.results) if args.results else None,
        "metric_notes": {
            "top1": "最后一条 submit_diagnosis.root_cause 小写子串命中任一关键词",
            "top3": "ranked_candidates 前 3 任一命中;缺失退化 [root_cause]",
            "full_pass": "三重断言全过(= run_evals passed)",
            "time_source": "record 轮 events.jsonl,单轮实测;replay 耗时无效",
            "manual": "manual_triage_minutes 为 runbook 人工排查估算参考值,非实测",
        },
        "aggregate": groups,
        "tasks": rows,
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(render_markdown(groups))
    print(f"\nscore saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
