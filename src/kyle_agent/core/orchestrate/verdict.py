"""reviewer 裁决解析(orchestrate 闭环支柱)。

reviewer 按约定在输出末行给出结构化裁决行 `VERDICT: ACHIEVED|PARTIAL|FAILED`,
供引擎程序化解析;引擎据此决定继续 replan 还是收束。
"""
from __future__ import annotations

import re
from enum import StrEnum


class ReviewVerdict(StrEnum):
    ACHIEVED = "achieved"
    PARTIAL = "partial"
    FAILED = "failed"


# 末行裁决行的匹配模式:大小写不敏感,允许结尾句号(中文/英文)与尾随空白
_VERDICT_RE = re.compile(
    r"^verdict\s*:\s*(achieved|partial|failed)\s*[。.]?\s*$", re.IGNORECASE
)
# 裁决行查找窗口:只认末尾 N 个非空行内的裁决,防止正文误含同类字样被误读
_VERDICT_WINDOW = 5
# "## 发现" 段落标题行(允许 1-3 级标题与前后空白)
_FINDINGS_HEADING_RE = re.compile(r"^#{1,3}\s*发现\s*$")
# markdown 标题行(任意级别),用于界定"发现"段落的结束
_HEADING_RE = re.compile(r"^#{1,3}\s+\S")


# 解析 reviewer 输出,返回(裁决, 发现段落原文)
def parse_verdict(reviewer_output: str) -> tuple[ReviewVerdict, str]:
    lines = reviewer_output.splitlines()
    # 裁决行:在末尾 5 个非空行内查找——约定是末行,但模型常在裁决后附加
    # 一两句收尾语;超出窗口视为未给出裁决。找不到时保守回退 PARTIAL——
    # reviewer 未遵守格式约定时按"未完全达成"处理,继续 replan 直到迭代上限,
    # 宁可多花一轮也不得在目标未证实时提前宣布成功
    verdict = ReviewVerdict.PARTIAL
    non_empty = [ln for ln in lines if ln.strip()]
    for line in reversed(non_empty[-_VERDICT_WINDOW:]):
        m = _VERDICT_RE.match(line.strip())
        if m:
            verdict = ReviewVerdict(m.group(1).lower())
            break

    return verdict, _extract_findings(lines)


# 提取"## 发现"段落原文;无该段落时回退为去掉裁决行后的全文
def _extract_findings(lines: list[str]) -> str:
    start = -1
    for i, line in enumerate(lines):
        if _FINDINGS_HEADING_RE.match(line.strip()):
            start = i + 1
            break
    if start < 0:
        # 没有结构化"发现"段:把裁决行之外的全文视为发现,保证回注 planner
        # 的证据不为空(裁决行本身对 replan 无信息量,剔除)
        return "\n".join(
            ln for ln in lines if not _VERDICT_RE.match(ln.strip())
        ).strip()
    end = len(lines)
    for j in range(start, len(lines)):
        if _HEADING_RE.match(lines[j].strip()):
            end = j
            break
    return "\n".join(lines[start:end]).strip()
