from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

# scripts/ 不是 Python 包,按文件路径加载 eval_score 模块(与 test_run_metrics 同款)
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_score.py"
_spec = importlib.util.spec_from_file_location("eval_score", _SCRIPT_PATH)
es = importlib.util.module_from_spec(_spec)
sys.modules["eval_score"] = es
assert _spec.loader is not None
_spec.loader.exec_module(es)


# 构造一个任务的 events.jsonl 产物目录
def _make_artifacts(
    root: Path, task_id: str, events: list[dict[str, Any]]
) -> Path:
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )
    return root


# 标准时间轴骨架(run.started/finished + 变更完成 + submit)
def _base_events(root_cause: str, candidates: list[dict[str, Any]] | None,
                 with_finished: bool = True) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "run.started", "run_id": "r", "goal": "g",
         "ts": "2026-11-12T08:00:00+00:00"},
        {"type": "tool.call_started", "run_id": "r", "tool_use_id": "t1",
         "tool_name": "service_restart", "params": {"service": "gateway"},
         "ts": "2026-11-12T08:00:10+00:00"},
        {"type": "tool.call_finished", "run_id": "r", "tool_use_id": "t1",
         "tool_name": "service_restart", "elapsed_ms": 40, "output": "ok",
         "ts": "2026-11-12T08:00:12+00:00"},
        {"type": "tool.call_started", "run_id": "r", "tool_use_id": "t2",
         "tool_name": "submit_diagnosis",
         "params": {"root_cause": root_cause,
                    **({"ranked_candidates": candidates} if candidates is not None else {})},
         "ts": "2026-11-12T08:00:20+00:00"},
    ]
    if with_finished:
        events.append({"type": "tool.call_finished", "run_id": "r",
                       "tool_use_id": "t2", "tool_name": "submit_diagnosis",
                       "elapsed_ms": 5, "output": "ok",
                       "ts": "2026-11-12T08:00:21+00:00"})
        events.append({"type": "run.finished", "run_id": "r", "status": "success",
                       "steps": 6, "ts": "2026-11-12T08:00:22+00:00"})
    return events


# 功能:Top-1 未中但候选第 2 位命中——Top-3 与 Top-1 分道
# 设计:鉴别诊断的核心价值场景;首位一致但备选命中仍计入 Top-3
def test_top3_hits_when_top1_misses(tmp_path: Path) -> None:
    root = _make_artifacts(tmp_path, "oncall-gateway-mem-leak", _base_events(
        "gateway traffic surge caused 5xx",
        [{"root_cause": "gateway traffic surge", "confidence": 0.9},
         {"root_cause": "gateway memory leak", "confidence": 0.5}],
    ))
    row = es.score_task("gateway-mem-leak", root, {})
    assert row["top1_hit"] is False
    assert row["top3_hit"] is True
    assert row["has_candidates"] is True


# 功能:无候选时 Top-3 退化为 [root_cause],覆盖率单列
# 设计:老轨迹/未按协议提交的任务不得在 Top-3 上凭空得分或丢分
def test_top3_degenerates_without_candidates(tmp_path: Path) -> None:
    root = _make_artifacts(tmp_path, "oncall-gateway-mem-leak", _base_events(
        "gateway memory leak", None,
    ))
    row = es.score_task("gateway-mem-leak", root, {})
    assert row["top1_hit"] is True
    assert row["top3_hit"] is True
    assert row["has_candidates"] is False


# 功能:候选超过 3 条时只取前 3(复合故障多候选)
# 设计:协议约定 2–4 条;Top-3 口径必须截断,第 4 位命中不增益
def test_top3_only_counts_first_three(tmp_path: Path) -> None:
    root = _make_artifacts(tmp_path, "oncall-gateway-mem-leak", _base_events(
        "orders-db deadlock unrelated",
        [{"root_cause": "orders-db deadlock", "confidence": 0.9},
         {"root_cause": "network partition", "confidence": 0.6},
         {"root_cause": "dns failure", "confidence": 0.4},
         {"root_cause": "gateway memory leak", "confidence": 0.2}],
    ))
    row = es.score_task("gateway-mem-leak", root, {})
    assert row["top1_hit"] is False
    assert row["top3_hit"] is False
    assert row["n_candidates"] == 3


# 功能:时间轴缺失(run.finished 缺失)容错——wall=None 不炸,聚合跳过
# 设计:record 轮可能中途失败;评分须对残缺产物稳健
def test_missing_timeline_tolerated(tmp_path: Path) -> None:
    root = _make_artifacts(tmp_path, "oncall-gateway-mem-leak", _base_events(
        "gateway memory leak", None, with_finished=False,
    ))
    row = es.score_task("gateway-mem-leak", root, {})
    assert row["wall_s"] is None
    assert row["top1_hit"] is True
    agg = es.aggregate([row], "全量")
    assert agg["wall_s_median"] is None
    assert agg["top1_hit_rate"] == 1.0


# 功能:诊断类剧本 MTTR 恒为 None;聚合含人工对比口径(估算)
# 设计:零变更剧本无 MTTR;加速比必须落在有 wall 的子集上
def test_mttr_none_for_diagnosis_scenarios(tmp_path: Path) -> None:
    root = _make_artifacts(tmp_path, "oncall-payments-cert-expired", _base_events(
        "tls certificate has expired", None,
    ))
    row = es.score_task("payments-cert-expired", root, {})
    assert row["mttr_s"] is None
    assert row["wall_s"] == 22.0
    assert row["layer"] == "基础"


# 功能:聚合的正确性——median/加速比/缩短比例数字核对
# 设计:两条 wall 20s/40s → 中位 30s=0.5min;人工 35/30min → 中位 32.5min,
#       加速比 = 32.5/0.5 = 65,缩短 = 1 − 0.5/32.5 ≈ 98.5%
def test_aggregate_math(tmp_path: Path) -> None:
    rows = []
    for tid, wall in (("gateway-mem-leak", 20.0), ("orders-mem-leak-slow", 40.0)):
        root = _make_artifacts(
            tmp_path / tid, f"oncall-{tid}",
            _base_events("memory leak", None))
        row = es.score_task(tid, root, {})
        row["wall_s"] = wall  # 归一化两条时间轴便于断言
        row["passed"] = True
        rows.append(row)
    agg = es.aggregate(rows, "难度层:基础")
    assert agg["n"] == 2
    assert agg["top1_hit_rate"] == 1.0
    assert agg["wall_s_median"] == 30.0
    assert agg["manual_minutes_median_est"] == 32.5
    assert agg["wall_minutes_median"] == 0.5
    assert agg["speedup_vs_manual_est"] == 65.0
    assert abs(agg["reduction_vs_manual_est"] - 0.9846) < 0.001


# 功能:harness 产物持久化——events.jsonl 与 oncall 报告复制到 artifacts 目录
# 设计:评测默认临时目录即焚;记录轮必须留存可复盘产物(评分数据来源)
def test_save_artifacts_copies_events_and_report(tmp_path: Path) -> None:
    from evals.harness import _save_artifacts

    runs = tmp_path / "runs" / "run-1"
    runs.mkdir(parents=True)
    (runs / "events.jsonl").write_text('{"type": "run.started"}\n', encoding="utf-8")
    env_reports = tmp_path / "oncall-env" / "reports"
    env_reports.mkdir(parents=True)
    (env_reports / "run-1.md").write_text("# 处置报告", encoding="utf-8")

    class _Cfg:
        oncall = type("O", (), {"enabled": True, "env_dir": str(tmp_path / "oncall-env")})()

    dest_root = tmp_path / "artifacts"
    _save_artifacts(dest_root, "oncall-gateway-mem-leak", tmp_path / "runs", _Cfg())
    assert (dest_root / "oncall-gateway-mem-leak" / "events.jsonl").exists()
    assert (dest_root / "oncall-gateway-mem-leak" / "report.md").read_text(
        encoding="utf-8") == "# 处置报告"


# 功能:无产物任务返回 error 行,评分主流程拒绝产出成绩
# 设计:防"静默少算"——缺产物的任务不得混入分母
def test_score_task_missing_artifacts(tmp_path: Path) -> None:
    row = es.score_task("gateway-mem-leak", tmp_path, {})
    assert row.get("error") == "events.jsonl 缺失"
    assert row["top1_hit"] is None
