from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ 不是 Python 包，按文件路径加载 run_metrics 模块并注册进 sys.modules
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_metrics.py"
_spec = importlib.util.spec_from_file_location("run_metrics", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
rm = importlib.util.module_from_spec(_spec)
sys.modules["run_metrics"] = rm
_spec.loader.exec_module(rm)

_TS = "2026-05-11T00:00:00Z"


# 构造覆盖全部被统计事件类型的合成日志（字段名与 core/bus/events.py 保持一致）
def _base_events() -> list[dict[str, Any]]:
    return [
        {"type": "run.started", "run_id": "run-1", "goal": "整理仓库", "ts": _TS},
        {"type": "llm.model_selected", "run_id": "run-1", "model": "glm-4.7", "strategy": "static", "ts": _TS},
        {"type": "llm.usage", "run_id": "run-1", "input_tokens": 100, "output_tokens": 50,
         "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5, "context_pct": 30.0, "ts": _TS},
        {"type": "step.started", "run_id": "run-1", "step": 1, "ts": _TS},
        {"type": "tool.call_started", "run_id": "run-1", "tool_use_id": "t1",
         "tool_name": "bash", "params": {"cmd": "ls"}, "ts": _TS},
        {"type": "tool.call_finished", "run_id": "run-1", "tool_use_id": "t1",
         "tool_name": "bash", "elapsed_ms": 100, "output": "ok", "ts": _TS},
        {"type": "step.finished", "run_id": "run-1", "step": 1, "ts": _TS},
        {"type": "step.started", "run_id": "run-1", "step": 2, "ts": _TS},
        {"type": "tool.call_started", "run_id": "run-1", "tool_use_id": "t2",
         "tool_name": "read_file", "params": {"path": "a.py"}, "ts": _TS},
        {"type": "tool.call_failed", "run_id": "run-1", "tool_use_id": "t2",
         "tool_name": "read_file", "error_class": "runtime_error", "error_message": "boom",
         "elapsed_ms": 40, "attempt": 1, "ts": _TS},
        {"type": "llm.model_selected", "run_id": "run-1", "model": "glm-4.7", "strategy": "cost_budget", "ts": _TS},
        {"type": "llm.usage", "run_id": "run-1", "input_tokens": 200, "output_tokens": 80,
         "cache_read_input_tokens": 20, "cache_creation_input_tokens": 8, "context_pct": 60.0, "ts": _TS},
        {"type": "step.finished", "run_id": "run-1", "step": 2, "ts": _TS},
        {"type": "context.compacted", "session_id": "sess-1", "run_id": "run-1",
         "original_tokens": 3000, "summary_tokens": 500, "ts": _TS},
        {"type": "llm.model_selected", "run_id": "run-1", "model": "glm-4.7-air", "strategy": "rule_based", "ts": _TS},
        {"type": "llm.usage", "run_id": "run-1", "input_tokens": 60, "output_tokens": 30,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "context_pct": 10.0, "ts": _TS},
        {"type": "run.finished", "run_id": "run-1", "status": "success", "reason": None, "steps": 2, "ts": _TS},
    ]


# 把事件 dict 列表序列化为 JSONL 文本行
def _to_lines(events: list[dict[str, Any]]) -> list[str]:
    return [json.dumps(e, ensure_ascii=False) for e in events]


# 把事件列表（可穿插坏行）写入临时 events.jsonl 并返回路径
def _write_events(path: Path, events: list[dict[str, Any]], extra_lines: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = _to_lines(events) + (extra_lines or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# 功能：验证 parse_lines 对非法 JSON / 非对象 / 缺 type 的坏行跳过并给出带行号的告警
# 设计：好行与三类坏行混排（外加一行空行），断言好行全部保留且恰好产生 3 条告警，坏行不影响后续解析
def test_parse_lines_skips_bad_lines() -> None:
    lines = [
        *(_to_lines(_base_events()[:2])),
        "{oops 不是 json",
        '["not", "dict"]',
        '{"no_type": 1}',
        "",
        *(_to_lines(_base_events()[2:3])),
    ]

    events, warnings = rm.parse_lines(lines)

    assert len(events) == 3
    assert events[0]["type"] == "run.started"
    assert len(warnings) == 3
    assert "第 3 行" in warnings[0]
    assert "第 4 行" in warnings[1]
    assert "第 5 行" in warnings[2]


# 功能：验证 compute_metrics 对覆盖全部事件类型的合成日志统计出正确的指标
# 设计：手工核算每项期望值（token 求和、按 model/strategy 分组、分工具成败与耗时），逐项断言，保证口径准确
def test_compute_metrics_covers_all_event_types() -> None:
    metrics = rm.compute_metrics(_base_events(), skipped_lines=1)

    assert metrics["event_count"] == 17
    assert metrics["skipped_lines"] == 1
    assert metrics["status"] == "success"
    assert metrics["reason"] is None
    assert metrics["steps"] == 2
    assert metrics["step_started"] == 2
    assert metrics["step_finished"] == 2

    llm = metrics["llm"]
    assert llm["calls"] == 3
    assert llm["input_tokens"] == 360
    assert llm["output_tokens"] == 160
    assert llm["cache_read_input_tokens"] == 30
    assert llm["cache_creation_input_tokens"] == 13
    assert llm["total_tokens"] == 563
    assert llm["context_pct_max"] == 60.0
    assert llm["models"]["glm-4.7"] == {"calls": 2, "strategies": {"static": 1, "cost_budget": 1}}
    assert llm["models"]["glm-4.7-air"] == {"calls": 1, "strategies": {"rule_based": 1}}

    tools = metrics["tools"]
    assert tools["total"] == 2
    assert tools["finished"] == 1
    assert tools["failed"] == 1
    assert tools["avg_ms"] == 70.0  # (100 + 40) / 2，失败调用也计入耗时
    assert tools["max_ms"] == 100
    assert tools["by_tool"]["bash"] == {
        "calls": 1, "finished": 1, "failed": 0, "avg_ms": 100.0, "max_ms": 100,
    }
    assert tools["by_tool"]["read_file"] == {
        "calls": 1, "finished": 0, "failed": 1, "avg_ms": 40.0, "max_ms": 40,
    }

    assert metrics["compact"] == {"count": 1, "original_tokens": 3000, "summary_tokens": 500}


# 功能：验证 run 缺少 run.finished 时步数用 step.started 数兜底、状态保持 None
# 设计：删掉末尾的 run.finished 事件，模拟异常中断的 run，断言 steps 回退且 status 为空（渲染为 "-"）
def test_compute_metrics_steps_fallback_without_run_finished() -> None:
    events = [e for e in _base_events() if e["type"] != "run.finished"]

    metrics = rm.compute_metrics(events)

    assert metrics["status"] is None
    assert metrics["steps"] == 2  # 取 step.started 计数


# 功能：验证 render_report 输出的 Markdown 报告包含全部章节和关键数值
# 设计：只断言章节标题与代表性单元格文本（状态、工具名、token 合计），不逐字比对整份文档以免脆弱
def test_render_report_markdown_sections() -> None:
    metrics = rm.compute_metrics(_base_events())

    report = rm.render_report(metrics, "runs/run-1/events.jsonl")

    assert "# 运行指标报告" in report
    assert "## 总览" in report
    assert "## LLM" in report
    assert "## 工具" in report
    assert "## Compact" in report
    assert "| 状态 | success |" in report
    assert "| token 合计 | 563 |" in report
    assert "| bash | 1 | 1 | 0 | 100.0 | 100 |" in report
    assert "| compact 次数 | 1 |" in report


# 功能：验证 render_compare 输出 指标 | A | B | Δ 对比表且数值行给出带符号差值
# 设计：B 在 A 基础上多一次 llm.usage，手工核算 input tokens Δ=+130，断言表头与该行完整文本
def test_render_compare_contains_delta() -> None:
    events_a = _base_events()
    events_b = [*_base_events(), {
        "type": "llm.usage", "run_id": "run-1", "input_tokens": 130, "output_tokens": 10,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "context_pct": 5.0, "ts": _TS,
    }]
    metrics_a = rm.compute_metrics(events_a)
    metrics_b = rm.compute_metrics(events_b)

    table = rm.render_compare(metrics_a, metrics_b, "a.jsonl", "b.jsonl")

    assert "# 运行指标对比" in table
    assert "| 指标 | A | B | Δ |" in table
    assert "| input tokens | 360 | 490 | +130 |" in table
    assert "| LLM 调用次数 | 3 | 4 | +1 |" in table
    assert "| token 合计 | 563 | 703 | +140 |" in table


# 功能：验证 resolve_target 按文件 → 目录 → cwd 下 runs/<id> → ~/.kyle/sessions/*/runs/<id> 的顺序定位
# 设计：用 monkeypatch 隔离 cwd 与 SESSIONS_DIR，四种形态各建一个真实文件，逐一断言解析结果与 None 分支
def test_resolve_target_file_dir_and_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    sessions_root = tmp_path / "fake-home" / ".kyle" / "sessions"
    monkeypatch.setattr(rm, "SESSIONS_DIR", sessions_root)

    direct = _write_events(tmp_path / "direct.jsonl", _base_events()[:1])
    run_dir = tmp_path / "my_run"
    run_dir.mkdir()
    _write_events(run_dir / "events.jsonl", _base_events()[:1])
    _write_events(Path("runs/run-cwd/events.jsonl"), _base_events()[:1])
    _write_events(sessions_root / "s1" / "runs" / "run-sess" / "events.jsonl", _base_events()[:1])

    assert rm.resolve_target(str(direct)) == direct
    assert rm.resolve_target(str(run_dir)) == run_dir / "events.jsonl"
    assert rm.resolve_target("run-cwd") == Path("runs") / "run-cwd" / "events.jsonl"
    assert rm.resolve_target("run-sess") == sessions_root / "s1" / "runs" / "run-sess" / "events.jsonl"
    assert rm.resolve_target("no-such-run") is None


# 功能：验证 main 对不存在的目标打印友好错误并返回退出码 1
# 设计：传入必然不存在的 run_id，用 capsys 断言 stderr 提示包含目标名与"错误"，不抛异常
def test_main_missing_target_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = rm.main(["definitely-not-exist-run"])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "错误" in stderr
    assert "definitely-not-exist-run" in stderr


# 功能：验证 main 接受 events.jsonl 文件、run 目录、run_id 三种输入并输出 Markdown 报告
# 设计：同一份合成日志以三种形态各跑一次，断言退出码 0 且 stdout 含报告标题与关键统计；坏行走 stderr 告警
def test_main_accepts_file_dir_and_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "runs" / "run-1"
    events = _base_events()
    _write_events(run_dir / "events.jsonl", events, extra_lines=["{bad json"])

    for target in (str(run_dir / "events.jsonl"), str(run_dir), "run-1"):
        capsys.readouterr()
        assert rm.main([target]) == 0
        captured = capsys.readouterr()
        assert "# 运行指标报告" in captured.out
        assert "| 状态 | success |" in captured.out
        assert "| 跳过坏行 | 1 |" in captured.out
        assert "第 18 行" in captured.err


# 功能：验证 main --compare 输出 A/B/Δ 对比表
# 设计：写两份仅 LLM usage 不同的日志文件，断言退出码 0、stdout 含对比表头和带符号差值 +130
def test_main_compare_outputs_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path_a = _write_events(tmp_path / "a.jsonl", _base_events())
    events_b = [*_base_events(), {
        "type": "llm.usage", "run_id": "run-1", "input_tokens": 130, "output_tokens": 10,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "context_pct": 5.0, "ts": _TS,
    }]
    path_b = _write_events(tmp_path / "b.jsonl", events_b)

    exit_code = rm.main([str(path_a), "--compare", str(path_b)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "# 运行指标对比" in out
    assert "| 指标 | A | B | Δ |" in out
    assert "| input tokens | 360 | 490 | +130 |" in out


# 功能：验证 --compare 目标不存在时返回退出码 1 并给出针对 compare 目标的错误提示
# 设计：主目标用真实文件保证越过第一步，--compare 传不存在的 run_id，断言退出码与 stderr 提示
def test_main_compare_missing_target_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path_a = _write_events(tmp_path / "a.jsonl", _base_events())

    exit_code = rm.main([str(path_a), "--compare", "no-such-run"])

    assert exit_code == 1
    assert "no-such-run" in capsys.readouterr().err
