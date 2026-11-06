from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kyle_agent.core.tools.base import BaseTool, ToolResult

# 文件名非法字符(Windows 保留字符 + 控制字符);集合不含 CJK,中文标题原样保留
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_FILENAME_CHARS = 80


class KnowledgeSaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)


class KnowledgeSaveTool(BaseTool):
    params_model = KnowledgeSaveParams
    name = "knowledge_save"
    description = (
        "Save a markdown document to the persistent knowledge base "
        "(~/.kyle/knowledge by default). Saved documents are immediately "
        "retrievable via knowledge_search and runbook_search. Use it to "
        "persist reusable troubleshooting experience, decisions or how-tos."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Document title (1-120 chars); also becomes "
                    "the file name (unsafe chars are sanitized, CJK kept).",
            },
            "content": {
                "type": "string",
                "description": "The full markdown body of the document.",
            },
        },
        "required": ["title", "content"],
    }

    # 绑定配置(config 缺省时首次 invoke 惰性加载,避免启动期急切读取)
    def __init__(self, config: Any | None = None) -> None:
        self._config = config

    # 解析知识库目录(expanduser 展开;config 缺省时走全局配置单例)
    def _knowledge_dir(self) -> Path:
        cfg = self._config
        if cfg is None:
            from kyle_agent.core.config import get_config

            cfg = get_config()
        return Path(cfg.knowledge.dir).expanduser()

    # 净化标题为安全文件 stem:非法字符替换为 -,去首尾空白/点号/连字符,
    # 截断 80 字符;全斜杠/纯点号等无信息标题净化后为空
    @staticmethod
    def _sanitize_title(title: str) -> str:
        cleaned = _UNSAFE_FILENAME_RE.sub("-", title).strip().strip(".- ")
        return cleaned[:_MAX_FILENAME_CHARS].rstrip(".- ")

    # 把文档写入知识库目录(同名覆写,幂等),返回入库摘要
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = KnowledgeSaveParams.model_validate(params)
        content = p.content.strip()
        if not content:
            return ToolResult(
                content="empty content",
                is_error=True,
                error_type="runtime_error",
            )
        name = self._sanitize_title(p.title)
        if not name:
            return ToolResult(
                content=f"title {p.title!r} sanitizes to an empty file name",
                is_error=True,
                error_type="runtime_error",
            )
        kb_dir = self._knowledge_dir()
        kb_dir.mkdir(parents=True, exist_ok=True)
        path = kb_dir / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return ToolResult(
            content=(
                f"saved to knowledge base: {name}.md\n"
                f"title: {p.title}\n"
                f"chars: {len(content)}\n"
                "indexed for retrieval by knowledge_search and runbook_search."
            )
        )
