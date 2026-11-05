"""Runbook 检索管道:懒构建索引 + keyword/vector/hybrid 三模式统一入口。

vector:查询嵌入 → 余弦 top-k(chunk 级),按文档去重后返回文档全文;
hybrid:余弦召回 top-8 → rerank 融合词面信号 → 取 top-k;
keyword:保留改造前的整篇关键词打分逻辑(输出逐字节兼容)。
索引按目录内容 hash 缓存,进程内单例复用(文件只读,重建仅发生在内容变化时)。
哈希嵌入配合库级 IDF:构建时按 chunk 文档频率统计 idf 并注入 embedder/rerank,
抑制高频汉字噪声(实测不加权时"完全无关的量子物理问题"噪声分可反超真命中)。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from hashlib import blake2b
from pathlib import Path
from typing import ClassVar

from kyle_agent.core.rag._tokenize import tokenize
from kyle_agent.core.rag.chunking import Chunk, chunk_markdown
from kyle_agent.core.rag.embeddings import EmbeddingProvider, HashingEmbedder
from kyle_agent.core.rag.rerank import rerank
from kyle_agent.core.rag.store import VectorStore

_HYBRID_RECALL = 8
# 无匹配判定:特征哈希的桶噪声(高权特征聚集)使分数绝对值不可靠,
# 实测噪声 cos 可达 0.15+ 且与弱真命中交叠;改用词面证据——
# query 与 top 候选的真实特征加权交集为零即视为无匹配
# (桶碰撞不产生特征交集,该判定与维度无关、物理可靠)


class RunbookIndex:
    """runbook 知识库索引:懒构建 chunk 向量库 + 整篇文档表(keyword 模式用)。"""

    # 进程内单例缓存:key = (目录, 内容 hash, embedder 标识)
    _CACHE: ClassVar[dict[tuple[str, str, str], RunbookIndex]] = {}

    # 绑定目录与嵌入器;索引延迟到首次 search 才构建
    def __init__(self, runbooks_dir: Path, embedder: EmbeddingProvider) -> None:
        self._dir = runbooks_dir
        self._embedder = embedder
        self._store: VectorStore | None = None
        self._docs: list[tuple[str, str, str]] = []  # (文件名, 标题, 全文)
        self._idf: dict[str, float] = {}

    # 工厂:按目录内容 hash 取进程内单例,内容变化自动换新索引
    @classmethod
    def get(cls, runbooks_dir: Path, embedder: EmbeddingProvider) -> RunbookIndex:
        digest = blake2b(digest_size=16)
        for path in sorted(runbooks_dir.glob("*.md")):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(path.read_bytes())
        content_hash = digest.hexdigest()
        key = (
            str(runbooks_dir.resolve()),
            content_hash,
            f"{embedder.name}:{embedder.dim}",
        )
        if key not in cls._CACHE:
            cls._CACHE[key] = cls(runbooks_dir, embedder)
        return cls._CACHE[key]

    # 懒构建:扫描文档 → 分片 → 统计 IDF → 嵌入 → 入库(仅首次查询执行一次)
    def _ensure_built(self) -> None:
        if self._store is not None:
            return
        docs: list[tuple[str, str, str]] = []
        chunks: list[Chunk] = []
        for path in sorted(self._dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            docs.append((path.name, path.stem.replace("-", " "), text.strip()))
            chunks.extend(chunk_markdown(text, path.name))
        store = VectorStore()
        if chunks:
            idf = _build_idf(chunks)
            if isinstance(self._embedder, HashingEmbedder):
                # 未见特征权重取 min(库内无稀有性证据,给低权防碰撞噪声放大)
                self._embedder.set_idf(idf, min(idf.values()) if idf else 1.0)
            vectors = self._embedder.embed([c.content for c in chunks])
            store.add(chunks, vectors)
            self._idf = idf
        self._docs = docs
        self._store = store

    # 三模式检索入口:返回代表 Chunk(content=文档全文),source 用于命中判定
    def search(self, query: str, mode: str = "hybrid", top_k: int = 3) -> list[Chunk]:
        self._ensure_built()
        assert self._store is not None
        if mode == "keyword":
            return self._search_keyword(query, top_k)
        if mode == "vector":
            return self._search_vector(query, top_k)
        if mode == "hybrid":
            return self._search_hybrid(query, top_k)
        raise ValueError(f"unknown search mode: {mode!r} (keyword|vector|hybrid)")

    # keyword 模式:整篇关键词打分(与改造前 tools.py 逻辑逐字节一致)
    def _search_keyword(self, query: str, top_k: int) -> list[Chunk]:
        terms = [w for w in re.split(r"[\s,，]+", query.lower()) if w]
        scored: list[tuple[float, str, str, str]] = []  # (score, name, title, text)
        for name, title, text in self._docs:
            lower = text.lower()
            score = 0.0
            for term in terms:
                score += lower.count(term) * 1.0
                if term in title:
                    score += 5.0
            if score > 0:
                scored.append((score, name, title, text))
        scored.sort(key=lambda x: -x[0])
        return [
            self._doc_chunk(name, title, text, "")
            for _, name, title, text in scored[:top_k]
        ]

    # vector 模式:纯余弦排序 + 词面门控,按文档去重取前 top_k 篇
    def _search_vector(self, query: str, top_k: int) -> list[Chunk]:
        assert self._store is not None
        qvec = self._embedder.embed([query])[0]
        candidates = self._store.search(qvec, len(self._store))
        candidates = self._filter_lexical(query, candidates)
        picked = self._dedupe_docs(candidates, top_k)
        if not picked:
            return []
        return [
            self._doc_chunk(c.source, self._title_of(c.source), self._text_of(c.source),
                            c.section_path)
            for c, _score in picked
        ]

    # hybrid 模式:余弦召回 → 词面门控 → rerank(带 IDF)→ 按文档去重取前 top_k 篇
    def _search_hybrid(self, query: str, top_k: int) -> list[Chunk]:
        assert self._store is not None
        qvec = self._embedder.embed([query])[0]
        candidates = self._store.search(qvec, min(_HYBRID_RECALL, len(self._store)))
        candidates = self._filter_lexical(query, candidates)
        reranked = rerank(
            query, candidates, len(candidates),
            idf=self._idf if self._idf else None,
        )
        picked = self._dedupe_docs(reranked, top_k)
        if not picked:
            return []
        return [
            self._doc_chunk(c.source, self._title_of(c.source), self._text_of(c.source),
                            c.section_path)
            for c, _score in picked
        ]

    # 词面门控:剔除与 query 特征零交集的候选(纯桶碰撞噪声),保持原有序
    def _filter_lexical(
        self, query: str, candidates: list[tuple[Chunk, float]]
    ) -> list[tuple[Chunk, float]]:
        return [
            (c, s) for c, s in candidates
            if _lexical_overlap(query, c, self._idf) > 0.0
        ]

    # 候选按文档去重(保留每篇最高分),返回 [(chunk, score)] 至多 top_k 篇
    def _dedupe_docs(
        self, candidates: list[tuple[Chunk, float]], top_k: int
    ) -> list[tuple[Chunk, float]]:
        seen: set[str] = set()
        picked: list[tuple[Chunk, float]] = []
        for chunk, score in candidates:
            if chunk.source in seen:
                continue
            seen.add(chunk.source)
            picked.append((chunk, score))
            if len(picked) >= top_k:
                break
        return picked

    # 构造文档级代表 Chunk(content=全文,检索结果输出形状与旧实现一致)
    def _doc_chunk(self, source: str, title: str, text: str, section_path: str) -> Chunk:
        return Chunk(
            content=text, source=source, title=title,
            section_path=section_path, index=0,
        )

    # 按文件名取文档标题
    def _title_of(self, source: str) -> str:
        for name, title, _ in self._docs:
            if name == source:
                return title
        return ""

    # 按文件名取文档全文
    def _text_of(self, source: str) -> str:
        for name, _, text in self._docs:
            if name == source:
                return text
        return ""


# 统计 chunk 特征的平滑 IDF:idf = log(1 + N/df),df 为含该特征的 chunk 数
def _build_idf(chunks: list[Chunk]) -> dict[str, float]:
    df: Counter[str] = Counter()
    for c in chunks:
        df.update(set(tokenize(c.content)))
    n = len(chunks)
    return {t: math.log(1 + n / d) for t, d in df.items()}


# query 与 chunk 的真实特征加权交集和(0 = 词面零重叠,判无匹配)
def _lexical_overlap(
    query: str, chunk: Chunk, idf: dict[str, float]
) -> float:
    q_tokens = set(tokenize(query))
    c_tokens = set(tokenize(chunk.content))
    common = q_tokens & c_tokens
    return sum(idf.get(t, 1.0) for t in common)


# 快捷工厂:本地确定性嵌入器的 runbook 索引单例
def local_runbook_index(runbooks_dir: Path) -> RunbookIndex:
    return RunbookIndex.get(runbooks_dir, HashingEmbedder())
