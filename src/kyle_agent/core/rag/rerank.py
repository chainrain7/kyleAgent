"""混合重排:向量召回结果与查询词面信号融合打分。

打分 = 0.6*余弦 + 0.3*加权词重叠率(query/chunk 特征的加权 Jaccard)
     + 0.1*标题命中(query 特征出现在 chunk 标题或层级路径时加分)。
分词复用 _tokenize.tokenize,与嵌入特征同口径,不另起炉灶;
IDF 表由索引方注入,加权 Jaccard 抑制高频字(的/理)对重叠率的虚增。
"""
from __future__ import annotations

from kyle_agent.core.rag._tokenize import tokenize
from kyle_agent.core.rag.chunking import Chunk

_COSINE_W = 0.6
_JACCARD_W = 0.3
_TITLE_W = 0.1


# 混合重排:对 (chunk, 余弦分) 候选打综合分,降序取 top_k(idf 缺省等权)
def rerank(
    query: str,
    candidates: list[tuple[Chunk, float]],
    top_k: int,
    idf: dict[str, float] | None = None,
) -> list[tuple[Chunk, float]]:
    # 特征权重:表内查 idf,未见特征取表内最小值(库内无稀有性证据,保守低权)
    default_w = min(idf.values()) if idf else 1.0

    # 查特征权重(无表时等权 1.0)
    def _weight(t: str) -> float:
        return idf.get(t, default_w) if idf else 1.0

    q_tokens = set(tokenize(query))
    q_weighted = {t: _weight(t) for t in q_tokens}
    scored: list[tuple[float, int]] = []
    for i, (chunk, cos) in enumerate(candidates):
        c_tokens = set(tokenize(chunk.content))
        c_weighted = {t: _weight(t) for t in c_tokens}
        union = set(q_weighted) | set(c_weighted)
        inter = sum(min(q_weighted.get(t, 0.0), c_weighted.get(t, 0.0)) for t in union)
        maxi = sum(max(q_weighted.get(t, 0.0), c_weighted.get(t, 0.0)) for t in union)
        jaccard = inter / maxi if maxi > 0 else 0.0
        title_blob = chunk.title + " " + chunk.section_path
        bonus = _TITLE_W if q_tokens & set(tokenize(title_blob)) else 0.0
        score = _COSINE_W * cos + _JACCARD_W * jaccard + bonus
        scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(candidates[i][0], s) for s, i in scored[:max(0, top_k)]]
