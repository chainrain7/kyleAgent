#!/usr/bin/env python3
"""
运行指标统计脚本：解析 run 的 events.jsonl，输出 Markdown 指标报告。

用法:
    python scripts/run_metrics.py <events.jsonl 路径 | run 目录 | run_id>
    python scripts/run_metrics.py <目标> --compare <另一份 events.jsonl>

目标解析顺序:
  1. 存在的文件 → 直接作为事件日志
  2. 存在的目录 → 取其下的 events.jsonl
  3. 其余 → 视为 run_id，先找 ./runs/<run_id>/events.jsonl，
     再回退 ~/.kyle/sessions/*/runs/<run_id>/events.jsonl

统计口径（事件字段以 src/kyle_agent/core/bus/events.py 为准）:
  - 总览: run.finished 的 status/reason/steps、事件总数、跳过的坏行数
  - LLM:  llm.usage 计数与 token 合计；llm.model_selected 按 model/strategy 分组
  - 工具: tool.call_started/finished/failed 各工具次数、成败分布、平均/最大耗时
  - Compact: context.compacted 次数与压缩前后 token 合计
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

RUNS_DIR = Path("runs")  # cwd 下的 run 目录约定，与 core/runs.py 保持一致
SESSIONS_DIR = Path("~/.kyle/sessions")  # 会话模式下 run 目录的根
EVENTS_FILENAME = "events.jsonl"


# 将 JSONL 文本行解析为事件列表；坏行（非法 JSON / 非对象 / 缺 type）跳过并返回带行号的告警
def parse_lines(lines: Iterable[str]) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"第 {lineno} 行不是合法 JSON，已跳过：{exc.msg}")
            continue
        if not isinstance(obj, dict) or "type" not in obj:
            warnings.append(f"第 {lineno} 行不是 JSON 对象或缺少 type 字段，已跳过")
            continue
        events.append(obj)
    return events, warnings


# 读取事件文件并调用 parse_lines 解析，返回 (事件列表, 告警列表)
def parse_events_file(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    with path.open(encoding="utf-8") as fh:
        return parse_lines(fh)


# 计算整数列表的平均值并保留一位小数，空列表返回 0.0
def _avg(values: list[int]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


# 从事件列表计算指标字典（纯函数：不涉及任何 IO，便于单测）
def compute_metrics(events: list[dict[str, Any]], skipped_lines: int = 0) -> dict[str, Any]:
    llm: dict[str, Any] = {
        "calls": 0,
        "models": {},
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 0,
        "context_pct_max": 0.0,
        "cache_hit_rate_last": 0.0,
    }
    by_tool: dict[str, dict[str, Any]] = {}
    all_elapsed: list[int] = []
    tools_total = tools_finished = tools_failed = 0
    metrics: dict[str, Any] = {
        "goal": "",
        "status": None,
        "reason": None,
        "steps": 0,
        "step_started": 0,
        "step_finished": 0,
        "event_count": len(events),
        "skipped_lines": skipped_lines,
        "llm": llm,
        "tools": {},
        "compact": {"count": 0, "original_tokens": 0, "summary_tokens": 0},
    }

    for event in events:
        etype = event.get("type", "")
        if etype == "run.started":
            metrics["goal"] = str(event.get("goal", ""))
        elif etype == "run.finished":
            metrics["status"] = event.get("status")
            metrics["reason"] = event.get("reason")
            metrics["steps"] = int(event.get("steps") or 0)
        elif etype == "step.started":
            metrics["step_started"] += 1
        elif etype == "step.finished":
            metrics["step_finished"] += 1
        elif etype == "llm.usage":
            llm["calls"] += 1
            llm["input_tokens"] += int(event.get("input_tokens") or 0)
            llm["output_tokens"] += int(event.get("output_tokens") or 0)
            llm["cache_read_input_tokens"] += int(event.get("cache_read_input_tokens") or 0)
            llm["cache_creation_input_tokens"] += int(event.get("cache_creation_input_tokens") or 0)
            llm["context_pct_max"] = max(llm["context_pct_max"], float(event.get("context_pct") or 0.0))
            llm["cache_hit_rate_last"] = float(event.get("cache_hit_rate") or 0.0)
        elif etype == "llm.model_selected":
            entry = llm["models"].setdefault(
                str(event.get("model") or "?"), {"calls": 0, "strategies": {}}
            )
            entry["calls"] += 1
            strategy = str(event.get("strategy") or "?")
            entry["strategies"][strategy] = entry["strategies"].get(strategy, 0) + 1
        elif etype in ("tool.call_started", "tool.call_finished", "tool.call_failed"):
            entry = by_tool.setdefault(
                str(event.get("tool_name") or "?"),
                {"calls": 0, "finished": 0, "failed": 0, "elapsed": []},
            )
            if etype == "tool.call_started":
                entry["calls"] += 1
                tools_total += 1
            elif etype == "tool.call_finished":
                entry["finished"] += 1
                tools_finished += 1
                elapsed = int(event.get("elapsed_ms") or 0)
                entry["elapsed"].append(elapsed)
                all_elapsed.append(elapsed)
            else:  # tool.call_failed（失败也有 elapsed_ms，计入耗时统计）
                entry["failed"] += 1
                tools_failed += 1
                elapsed = int(event.get("elapsed_ms") or 0)
                entry["elapsed"].append(elapsed)
                all_elapsed.append(elapsed)
        elif etype == "context.compacted":
            metrics["compact"]["count"] += 1
            metrics["compact"]["original_tokens"] += int(event.get("original_tokens") or 0)
            metrics["compact"]["summary_tokens"] += int(event.get("summary_tokens") or 0)

    llm["total_tokens"] = (
        llm["input_tokens"]
        + llm["output_tokens"]
        + llm["cache_read_input_tokens"]
        + llm["cache_creation_input_tokens"]
    )
    metrics["tools"] = {
        "total": tools_total,
        "finished": tools_finished,
        "failed": tools_failed,
        "avg_ms": _avg(all_elapsed),
        "max_ms": max(all_elapsed, default=0),
        "by_tool": {
            name: {
                "calls": e["calls"],
                "finished": e["finished"],
                "failed": e["failed"],
                "avg_ms": _avg(e["elapsed"]),
                "max_ms": max(e["elapsed"], default=0),
            }
            for name, e in sorted(by_tool.items())
        },
    }
    # run.finished 缺失时（异常中断的 run）用 step.started 数兜底
    if not metrics["steps"]:
        metrics["steps"] = metrics["step_started"]
    return metrics


# 格式化单元格取值：None/空 → "-"，浮点保留一位小数，其余原样
def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


# 将指标字典渲染为 Markdown 报告文本
def render_report(metrics: dict[str, Any], source: str) -> str:
    llm = metrics["llm"]
    tools = metrics["tools"]
    compact = metrics["compact"]
    lines = [
        "# 运行指标报告",
        "",
        f"- 来源：`{source}`",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 状态 | {_fmt(metrics['status'])} |",
        f"| 结束原因 | {_fmt(metrics['reason'])} |",
        f"| 步数 | {metrics['steps']} |",
        f"| 事件数 | {metrics['event_count']} |",
        f"| 跳过坏行 | {metrics['skipped_lines']} |",
        "",
        "## LLM",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 调用次数 | {llm['calls']} |",
        f"| input tokens | {llm['input_tokens']} |",
        f"| output tokens | {llm['output_tokens']} |",
        f"| cache read tokens | {llm['cache_read_input_tokens']} |",
        f"| cache creation tokens | {llm['cache_creation_input_tokens']} |",
        f"| token 合计 | {llm['total_tokens']} |",
        f"| 峰值 context | {llm['context_pct_max']:.1f}% |",
    ]
    if llm["models"]:
        lines += ["", "### 按 model 分组", "", "| model | 调用数 | strategy 分布 |", "|---|---|---|"]
        for model, entry in sorted(llm["models"].items()):
            strategies = ", ".join(f"{s}×{n}" for s, n in sorted(entry["strategies"].items()))
            lines.append(f"| {model} | {entry['calls']} | {strategies or '-'} |")
    lines += [
        "",
        "## 工具",
        "",
        f"共 {tools['total']} 次调用（成功 {tools['finished']} / 失败 {tools['failed']}）。",
        "",
        "| 工具 | 次数 | 成功 | 失败 | 平均耗时(ms) | 最大耗时(ms) |",
        "|---|---|---|---|---|---|",
    ]
    for name, entry in tools["by_tool"].items():
        lines.append(
            f"| {name} | {entry['calls']} | {entry['finished']} | {entry['failed']}"
            f" | {entry['avg_ms']:.1f} | {entry['max_ms']} |"
        )
    lines += [
        "",
        "## Compact",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| compact 次数 | {compact['count']} |",
        f"| 压缩前 tokens 合计 | {compact['original_tokens']} |",
        f"| 压缩后 tokens 合计 | {compact['summary_tokens']} |",
        "",
    ]
    return "\n".join(lines)


# 将指标字典展开为 (指标名, 值) 有序行，供对比表使用（数值保持类型以便计算 Δ）
def metric_rows(metrics: dict[str, Any]) -> list[tuple[str, int | float | str]]:
    llm = metrics["llm"]
    tools = metrics["tools"]
    compact = metrics["compact"]
    return [
        ("状态", _fmt(metrics["status"])),
        ("步数", int(metrics["steps"])),
        ("事件数", int(metrics["event_count"])),
        ("LLM 调用次数", int(llm["calls"])),
        ("input tokens", int(llm["input_tokens"])),
        ("output tokens", int(llm["output_tokens"])),
        ("cache read tokens", int(llm["cache_read_input_tokens"])),
        ("cache creation tokens", int(llm["cache_creation_input_tokens"])),
        ("token 合计", int(llm["total_tokens"])),
        ("工具调用次数", int(tools["total"])),
        ("工具成功次数", int(tools["finished"])),
        ("工具失败次数", int(tools["failed"])),
        ("工具平均耗时(ms)", float(tools["avg_ms"])),
        ("工具最大耗时(ms)", int(tools["max_ms"])),
        ("compact 次数", int(compact["count"])),
    ]


# 渲染 A/B 两份指标的 Markdown 对比表（指标 | A | B | Δ，数值行计算差值，文本行不计算）
def render_compare(
    metrics_a: dict[str, Any], metrics_b: dict[str, Any], source_a: str, source_b: str
) -> str:
    lines = [
        "# 运行指标对比",
        "",
        f"- A：`{source_a}`",
        f"- B：`{source_b}`",
        "",
        "| 指标 | A | B | Δ |",
        "|---|---|---|---|",
    ]
    rows_b = dict(metric_rows(metrics_b))
    for label, value_a in metric_rows(metrics_a):
        value_b = rows_b[label]
        if isinstance(value_a, (int, float)) and isinstance(value_b, (int, float)):
            delta = value_b - value_a
            delta_text = f"{delta:+d}" if isinstance(delta, int) else f"{delta:+.1f}"
        else:
            delta_text = "" if value_a == value_b else "-"
        lines.append(f"| {label} | {_fmt(value_a)} | {_fmt(value_b)} | {delta_text} |")
    return "\n".join(lines)


# 解析命令行目标为 events.jsonl 路径：文件 → 目录 → run_id（runs/ 与 ~/.kyle/sessions/*/runs/）
def resolve_target(target: str) -> Path | None:
    path = Path(target).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        candidate = path / EVENTS_FILENAME
        return candidate if candidate.is_file() else None
    run_file = RUNS_DIR / target / EVENTS_FILENAME
    if run_file.is_file():
        return run_file
    for candidate in SESSIONS_DIR.expanduser().glob(f"*/runs/{target}/{EVENTS_FILENAME}"):
        if candidate.is_file():
            return candidate
    return None


# CLI 入口：解析参数 → 定位文件 → 计算并输出报告/对比表，返回进程退出码
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_metrics.py",
        description="统计 run 的 events.jsonl 运行指标并输出 Markdown 报告",
    )
    parser.add_argument("target", help="events.jsonl 路径、run 目录或 run_id")
    parser.add_argument(
        "--compare",
        metavar="TARGET",
        help="另一份 events.jsonl（路径/目录/run_id），输出指标 | A | B | Δ 对比表",
    )
    args = parser.parse_args(argv)

    path_a = resolve_target(args.target)
    if path_a is None:
        print(
            f"错误：找不到目标 {args.target!r} 对应的 events.jsonl"
            "（支持文件路径、run 目录或 run_id）",
            file=sys.stderr,
        )
        return 1
    events_a, warnings_a = parse_events_file(path_a)
    for warning in warnings_a:
        print(f"警告（{path_a.name}）：{warning}", file=sys.stderr)

    if args.compare is None:
        print(render_report(compute_metrics(events_a, len(warnings_a)), str(path_a)))
        return 0

    path_b = resolve_target(args.compare)
    if path_b is None:
        print(
            f"错误：找不到 --compare 目标 {args.compare!r} 对应的 events.jsonl",
            file=sys.stderr,
        )
        return 1
    events_b, warnings_b = parse_events_file(path_b)
    for warning in warnings_b:
        print(f"警告（{path_b.name}）：{warning}", file=sys.stderr)

    metrics_a = compute_metrics(events_a, len(warnings_a))
    metrics_b = compute_metrics(events_b, len(warnings_b))
    print(render_compare(metrics_a, metrics_b, str(path_a), str(path_b)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
