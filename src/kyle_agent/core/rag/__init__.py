"""通用 RAG 引擎:分片 → 嵌入 → 召回 → 重排(OnCallCopilot runbook 检索为首个使用方)。"""
from __future__ import annotations

from kyle_agent.core.rag.chunking import Chunk, chunk_markdown
from kyle_agent.core.rag.embeddings import (
    EmbeddingProvider,
    HashingEmbedder,
    OpenAICompatEmbedder,
)
from kyle_agent.core.rag.pipeline import RunbookIndex, local_runbook_index
from kyle_agent.core.rag.rerank import rerank
from kyle_agent.core.rag.store import VectorStore

__all__ = [
    "Chunk",
    "chunk_markdown",
    "EmbeddingProvider",
    "HashingEmbedder",
    "OpenAICompatEmbedder",
    "VectorStore",
    "rerank",
    "RunbookIndex",
    "local_runbook_index",
]
