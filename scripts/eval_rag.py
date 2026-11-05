"""runbook 检索质量评测:keyword / vector / hybrid 三模式离线对比。

用法:uv run python scripts/eval_rag.py
读取 evals/rag_queries.json(36 条标注查询),对三种模式各跑全量检索,
按"返回 top-3 全文所属 runbook 文件名 == 标注 target"判定命中,
输出 Recall@1 / Recall@3 / MRR 的 Markdown 对比表(含 literal/paraphrase 分组)。
全程离线(本地哈希嵌入器,零网络零 API Key)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from kyle_agent.core.rag.pipeline import RunbookIndex, local_runbook_index

_REPO = Path(__file__).parents[1]
_RUNBOOKS = _REPO / "src" / "kyle_agent" / "oncall" / "runbooks"
_QUERIES = _REPO / "evals" / "rag_queries.json"
_MODES = ("keyword", "vector", "hybrid")


# 从标注集 JSON 读取查询列表
def _load_queries() -> list[dict]:
    return json.loads(_QUERIES.read_text(encoding="utf-8"))["queries"]


# 单条查询的命中名次(1 起,未命中 0)
def _first_hit_rank(sources: list[str], target: str) -> int:
    for i, s in enumerate(sources, start=1):
        if s == target:
            return i
    return 0


# 对单模式计算全量指标的 (recall1, recall3, mrr)
def _eval_mode(idx: RunbookIndex, queries: list[dict], mode: str) -> tuple[float, float, float]:
    r1 = r3 = mrr = 0.0
    for q in queries:
        res = idx.search(q["query"], mode=mode, top_k=3)
        rank = _first_hit_rank([c.source for c in res], q["target"])
        r1 += 1.0 if rank == 1 else 0.0
        r3 += 1.0 if rank >= 1 else 0.0
        mrr += 1.0 / rank if rank >= 1 else 0.0
    n = len(queries)
    return r1 / n, r3 / n, mrr / n


# 渲染一组(全部/literal/paraphrase)指标行
def _rows_for(title: str, subset: list[dict]) -> list[str]:
    lines = [f"### {title}(n={len(subset)})", ""]
    lines.append("| 模式 | Recall@1 | Recall@3 | MRR |")
    lines.append("|---|---|---|---|")
    for mode in _MODES:
        r1, r3, mrr = _eval_mode(_INDEX, subset, mode)
        lines.append(f"| {mode} | {r1:.3f} | {r3:.3f} | {mrr:.3f} |")
    lines.append("")
    return lines


# 主流程:全量跑三模式并输出 Markdown 对比表
def main() -> int:
    global _INDEX
    queries = _load_queries()
    _INDEX = local_runbook_index(_RUNBOOKS)
    literal = [q for q in queries if q["kind"] == "literal"]
    para = [q for q in queries if q["kind"] == "paraphrase"]

    out: list[str] = [
        "# runbook 检索三模式对比(本地哈希嵌入器,离线真跑)",
        "",
        f"- 标注集:{_QUERIES.name}(9 篇 runbook × 4 条查询)",
        "- 命中判定:返回 top-3 全文所属 runbook 文件名 == 标注 target",
        "",
    ]
    out += _rows_for("全量", queries)
    out += _rows_for("字面查询 literal", literal)
    out += _rows_for("改写查询 paraphrase", para)
    report = "\n".join(out)
    print(report)

    # 结果落盘:2 开头命名命中 .gitignore 的 evals/results/2*(时间戳存档不入库惯例)
    results_dir = _REPO / "evals" / "results"
    results_dir.mkdir(exist_ok=True)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (results_dir / f"{stamp}-rag-eval.json").write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "queries": len(queries),
                "modes": {
                    m: {
                        "all": _eval_mode(_INDEX, queries, m),
                        "literal": _eval_mode(_INDEX, literal, m),
                        "paraphrase": _eval_mode(_INDEX, para, m),
                    }
                    for m in _MODES
                },
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"results saved to {results_dir / f'{stamp}-rag-eval.json'}")
    return 0


_INDEX: RunbookIndex

if __name__ == "__main__":
    raise SystemExit(main())
