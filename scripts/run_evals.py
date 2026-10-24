#!/usr/bin/env python3
"""评测流水线 CLI(支柱 1)。

三种模式:
- --record  用真实 LLM(.env 配置)跑任务集并录制 cassette → evals/cassettes/
- --replay  (默认)离线确定性回放 cassette,CI 可跑
- --live    直接用真实 LLM 跑(不录制)
对比:--compare <results.json> 输出通过率/步数/token 的前后 delta。

    uv run python scripts/run_evals.py --replay
    uv run python scripts/run_evals.py --record --task fix-pytest-bug
    uv run python scripts/run_evals.py --compare evals/results/baseline.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evals.cassette import (  # noqa: E402
    CassetteProvider,
    CassetteRecorder,
    load_cassette,
    save_cassette,
)
from evals.harness import TaskResult, run_task, summarize  # noqa: E402
from evals.tasks import TASKS  # noqa: E402

CASSETTE_DIR = REPO_ROOT / "evals" / "cassettes"
RESULTS_DIR = REPO_ROOT / "evals" / "results"


# 构造真实 LLM provider(读 .env;复用项目 provider 工厂与路由)
def _live_provider() -> object:
    from dotenv import load_dotenv

    from kyle_agent.core.config import get_config
    from kyle_agent.core.llm.factory import build_provider

    load_dotenv(REPO_ROOT / ".env")
    config = get_config()
    return build_provider(config.llm)


# 渲染单次评测结果的 Markdown 报告
def _render(results: list[TaskResult], summary: dict[str, Any]) -> str:
    lines = [
        "# 评测报告",
        "",
        f"- 时间: {datetime.now(UTC).isoformat()}",
        f"- 通过率: **{summary['passed']}/{summary['total']}"
        f" ({summary['pass_rate']:.0%})**",
        f"- LLM 调用: {summary['llm_calls']}  input tokens: {summary['input_tokens']:,}"
        f"  output tokens: {summary['output_tokens']:,}",
        "",
        "| 任务 | 类别 | 结果 | 步数 | LLM 调用 | 失败原因 |",
        "|---|---|---|---|---|---|",
    ]
    by_id = {t.id: t for t in TASKS}
    for r in results:
        llm = r.metrics.get("llm") or {}
        steps = r.metrics.get("steps") or 0
        lines.append(
            f"| {r.task_id} | {by_id[r.task_id].category} |"
            f" {'✅' if r.passed else '❌'} | {steps} | {llm.get('calls', 0)} |"
            f" {r.error[:60] or ''} |"
        )
    return "\n".join(lines)


# 渲染两次评测的对比表
def _render_compare(old: dict[str, Any], new: dict[str, Any]) -> str:
    def row(name: str, key: str) -> str:
        a, b = old["summary"].get(key, 0), new["summary"].get(key, 0)
        delta = b - a  # type: ignore[operator]
        sign = "+" if delta > 0 else ""
        return f"| {name} | {a} | {b} | {sign}{delta} |"

    lines = [
        "## 对比",
        "",
        "| 指标 | A | B | Δ |",
        "|---|---|---|---|",
        row("通过率", "pass_rate"),
        row("LLM 调用", "llm_calls"),
        row("input tokens", "input_tokens"),
        row("output tokens", "output_tokens"),
    ]
    old_pass = {r["task_id"]: r["passed"] for r in old["results"]}
    new_pass = {r["task_id"]: r["passed"] for r in new["results"]}
    flipped = [t for t in new_pass if old_pass.get(t) in (True, False) and old_pass.get(t) != new_pass[t]]
    if flipped:
        lines.append("")
        lines.append("状态翻转的任务: " + ", ".join(
            f"{t}({'FAIL→PASS' if new_pass[t] else 'PASS→FAIL'})" for t in flipped
        ))
    return "\n".join(lines)


# 序列化结果为 JSON 存档
def _dump(results: list[TaskResult], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "results": [
            {
                "task_id": r.task_id,
                "passed": r.passed,
                "run_status": r.run_status,
                "error": r.error,
                "metrics": r.metrics,
            }
            for r in results
        ],
    }


# 执行评测主流程
async def _run(mode: str, task_ids: list[str], out: Path | None) -> tuple[str, int]:
    selected = [t for t in TASKS if not task_ids or t.id in task_ids]
    if not selected:
        return f"error: no task matched {task_ids}", 1

    results: list[TaskResult] = []
    with tempfile.TemporaryDirectory(prefix="kyle-evals-") as tmp:
        root = Path(tmp)
        for i, task in enumerate(selected, 1):
            if mode == "replay":
                cas = CASSETTE_DIR / f"{task.id}.json"
                if not cas.exists():
                    results.append(TaskResult(
                        task_id=task.id, passed=False, run_status="missing-cassette",
                        error=f"cassette 不存在: {cas.name}(先 --record)",
                    ))
                    continue
                provider: object = CassetteProvider(load_cassette(cas))
            elif mode == "record":
                recorder = CassetteRecorder(_live_provider())
                provider = recorder
            else:
                provider = _live_provider()

            print(f"[{i}/{len(selected)}] {task.id} ...", flush=True)
            result = await run_task(task, provider, root)
            results.append(result)
            print(f"    -> {'PASS' if result.passed else 'FAIL'}"
                  f"{'' if result.passed else ' ' + result.error[:80]}", flush=True)

            if mode == "record":
                save_cassette(
                    CASSETTE_DIR / f"{task.id}.json",
                    recorder.entries,  # type: ignore[attr-defined]
                    task_id=task.id,
                )

    summary = summarize(results)
    report = _render(results, summary)
    print("\n" + report)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(_dump(results, summary), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\nresults saved to {out}")
    return report, 0 if summary["passed"] == summary["total"] else 1


# CLI 入口
def main() -> int:
    parser = argparse.ArgumentParser(prog="run_evals", description="kyleAgent 评测流水线")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="离线回放 cassette(默认)")
    mode.add_argument("--record", action="store_true", help="真实 LLM 跑并录制 cassette")
    mode.add_argument("--live", action="store_true", help="真实 LLM 跑,不录制")
    parser.add_argument("--task", action="append", metavar="ID", help="只跑指定任务(可多次)")
    parser.add_argument("--out", type=Path, help="结果 JSON 存档路径")
    parser.add_argument("--compare", type=Path, metavar="OLD.json", help="与历史结果对比")
    parser.add_argument("--list", action="store_true", help="列出全部任务")
    args = parser.parse_args()

    if args.list:
        for t in TASKS:
            cas = CASSETTE_DIR / f"{t.id}.json"
            print(f"{t.id:22s} [{t.category:8s}] cassette={'Y' if cas.exists() else '-'}  {t.note}")
        return 0

    mode_name = "record" if args.record else "live" if args.live else "replay"
    out = args.out or (
        RESULTS_DIR / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{mode_name}.json"
    )
    _, code = asyncio.run(_run(mode_name, args.task or [], out))

    if args.compare:
        if not args.compare.exists() or not out.exists():
            print("error: --compare 需要历史文件且本次需有 --out 存档", file=sys.stderr)
            return 1
        old = json.loads(args.compare.read_text(encoding="utf-8"))
        new = json.loads(out.read_text(encoding="utf-8"))
        print("\n" + _render_compare(old, new))
    return code


if __name__ == "__main__":
    sys.exit(main())
