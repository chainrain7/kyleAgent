"""向量库:chunk + 向量的内存索引,余弦相似度线性扫描。

规模决策:runbook 库 <100 chunk,线性扫描(几百次点积,微秒级)
就是最优解——建倒排/ANN 的索引成本远超收益;库涨到万级再换方案。
"""
from __future__ import annotations

import math

from kyle_agent.core.rag.chunking import Chunk


class VectorStore:
    """内存向量索引:add 追加,search 余弦 top-k。"""

    # 空库构造
    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []

    # 追加一批 chunk 及其向量(长度必须一致)
    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks({len(chunks)}) and vectors({len(vectors)}) length mismatch"
            )
        self._chunks.extend(chunks)
        self._vectors.extend(vectors)

    # 库内 chunk 数量
    def __len__(self) -> int:
        return len(self._chunks)

    # 余弦相似度线性扫描,返回 [(chunk, score)] 按分数降序,最多 top_k 条
    def search(self, query_vec: list[float], top_k: int) -> list[tuple[Chunk, float]]:
        qnorm = math.sqrt(sum(x * x for x in query_vec))
        scored: list[tuple[float, int]] = []
        for i, vec in enumerate(self._vectors):
            vnorm = math.sqrt(sum(x * x for x in vec))
            if qnorm == 0.0 or vnorm == 0.0:
                scored.append((0.0, i))
                continue
            dot = sum(a * b for a, b in zip(query_vec, vec))
            scored.append((dot / (qnorm * vnorm), i))
        scored.sort(key=lambda x: -x[0])
        top = scored[:max(0, top_k)]
        return [(self._chunks[i], s) for s, i in top]
