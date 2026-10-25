from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from kyle_agent.core.memory.index import MemoryIndex
from kyle_agent.core.session.store import SessionStore
from kyle_agent.core.tools.base import BaseTool, ToolResult


class NoteSaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str


class NoteSaveTool(BaseTool):
    params_model = NoteSaveParams
    name = "note_save"
    description = (
        "Save a concise fact or decision to this session's notes. "
        "These notes are visible in future turns of the same session."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The durable fact or decision to remember.",
            },
        },
        "required": ["content"],
    }

    # 绑定当前 session 与 run，使工具调用能写入对应 notes.md;可选跨会话记忆库
    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        run_id: str,
        memory_index: MemoryIndex | None = None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id
        self._memory = memory_index

    # 将非空 content 追加到 session notes.md
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        content = NoteSaveParams.model_validate(params).content.strip()
        if not content:
            return ToolResult(
                content="empty content",
                is_error=True,
                error_type="runtime_error",
            )
        self._store.append_note(self._session_id, content, self._run_id)
        # 双写跨会话记忆库(FTS5),供后续任意会话按目标检索
        if self._memory is not None:
            self._memory.remember(content, source="note", session_id=self._session_id)
        return ToolResult(content="saved")
