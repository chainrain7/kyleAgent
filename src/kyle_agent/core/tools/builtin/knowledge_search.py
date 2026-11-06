from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kyle_agent.core.tools.base import BaseTool, ToolResult

_VALID_MODES = ("keyword", "vector", "hybrid")


class KnowledgeSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1)
    mode: str = "keyword"


class KnowledgeSearchTool(BaseTool):
    params_model = KnowledgeSearchParams
    name = "knowledge_search"
    description = (
        "Search the user knowledge base (documents saved via knowledge_save). "
        "Returns the most relevant documents in full (top-3). Modes: "
        "keyword (default, literal scoring), vector (embedding cosine), "
        "hybrid (cosine recall + rerank)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "mode": {
                "type": "string",
                "enum": ["keyword", "vector", "hybrid"],
                "description": "Retrieval mode (default keyword).",
            },
        },
        "required": ["query"],
    }

    # 绑定配置(config 缺省时首次 invoke 惰性加载,避免启动期急切构建)
    def __init__(self, config: Any | None = None) -> None:
        self._config = config

    # 解析知识库目录(expanduser 展开;config 缺省时走全局配置单例)
    def _knowledge_dir(self) -> Path:
        cfg = self._config
        if cfg is None:
            from kyle_agent.core.config import get_config

            cfg = get_config()
        return Path(cfg.knowledge.dir).expanduser()

    # 解析知识库索引(仅知识库目录,内建 runbook 是 oncall 域语料不混入;
    # rag 配置为 openai 时用 OpenAICompatEmbedder,否则本地哈希;惰性 import,注解用 Any)
    def _resolve_index(self) -> Any:
        from kyle_agent.core.rag.embeddings import OpenAICompatEmbedder
        from kyle_agent.core.rag.pipeline import RunbookIndex, local_runbook_index

        cfg = self._config
        if cfg is None:
            from kyle_agent.core.config import get_config

            cfg = get_config()
        kb_dir = Path(cfg.knowledge.dir).expanduser()
        if cfg.rag.embedding_provider == "openai":
            embedder = OpenAICompatEmbedder(
                cfg.rag.embedding_base_url,
                cfg.rag.embedding_model,
                api_key=cfg.rag.embedding_api_key or None,
                cache_dir=cfg.rag.cache_dir,
            )
            return RunbookIndex.get(kb_dir, embedder)
        return local_runbook_index(kb_dir)

    # 三模式检索知识库(输出形状与 runbook_search 一致:top-3 篇全文拼接;
    # mode 非法值返回结构化运行时错误而非抛异常)
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = KnowledgeSearchParams.model_validate(params)
        if p.mode not in _VALID_MODES:
            return ToolResult(
                content=f"unknown search mode: {p.mode!r} (keyword|vector|hybrid)",
                is_error=True,
                error_type="runtime_error",
            )
        try:
            chunks = self._resolve_index().search(p.query, mode=p.mode, top_k=3)
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True, error_type="config_error")
        if not chunks:
            kb_dir = self._knowledge_dir()
            available = (
                [q.stem for q in sorted(kb_dir.glob("*.md"))] if kb_dir.is_dir() else []
            )
            return ToolResult(
                content=f"no matching documents. available: {available}"
            )
        return ToolResult(
            content="\n\n---\n\n".join(c.content for c in chunks)
        )
