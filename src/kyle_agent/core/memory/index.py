"""分层记忆索引(支柱 2c):跨会话的持久记忆与检索注入。

存储采用 SQLite FTS5(标准库自带,零额外依赖):
- remember():写入记忆条目(来源:note_save 笔记 / 成功 run 的结论摘要)
- recall():按当前目标检索最相关的 top-k 条,经 runner 注入 system prompt
  的 "## Recalled Memories" 段——这是三层记忆(session/notes)之上的
  第四层:跨会话、可检索。

检索策略:FTS5 MATCH(英文分词好)→ 无结果时 LIKE 子串兜底(CJK 场景
unicode61 分词粒度过粗),两者都按时间倒序稳定排序。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class MemoryHit:
    # 命中的记忆条目
    content: str
    source: str
    session_id: str
    ts: str


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryIndex:
    # 打开(或创建)记忆库并确保表结构存在
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content, source, session_id, ts
            )
            """
        )
        self._conn.commit()

    # 写入一条记忆;content 为正文,source 标注来源(note/run)
    def remember(self, content: str, *, source: str, session_id: str = "") -> None:
        text = content.strip()
        if not text:
            return
        self._conn.execute(
            "INSERT INTO memory_fts (content, source, session_id, ts) VALUES (?, ?, ?, ?)",
            (text, source, session_id, _now()),
        )
        self._conn.commit()

    # 按查询检索最相关的 k 条记忆;FTS 无结果时 LIKE 兜底(CJK 友好)
    def recall(self, query: str, *, k: int = 3) -> list[MemoryHit]:
        q = query.strip()
        if not q:
            return []
        rows: list[tuple[str, str, str, str]] = self._fts_query(q, k)
        if not rows:
            rows = self._like_query(q, k)
        return [MemoryHit(content=c, source=s, session_id=sid, ts=ts) for c, s, sid, ts in rows]

    # FTS5 全文检索,按 bm25 相关性排序
    def _fts_query(self, q: str, k: int) -> list[tuple[str, str, str, str]]:
        # 去掉 FTS5 语法字符后按词项 OR 匹配(整句短语匹配过严,recall 场景要宽松)
        safe = "".join(ch for ch in q if ch.isalnum() or ch.isspace())
        terms = [t for t in safe.split() if t]
        if not terms:
            return []
        match_expr = " OR ".join(f'"{t}"' for t in terms)
        try:
            cur = self._conn.execute(
                "SELECT content, source, session_id, ts FROM memory_fts "
                "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT ?",
                (match_expr, k),
            )
            return list(cur.fetchall())
        except sqlite3.OperationalError:
            return []

    # LIKE 子串兜底检索,按时间倒序(CJK 分词粒度问题的补偿路径)
    def _like_query(self, q: int | str, k: int) -> list[tuple[str, str, str, str]]:
        cur = self._conn.execute(
            "SELECT content, source, session_id, ts FROM memory_fts "
            "WHERE content LIKE ? ORDER BY ts DESC LIMIT ?",
            (f"%{q}%", k),
        )
        return list(cur.fetchall())

    # 关闭底层连接
    def close(self) -> None:
        self._conn.close()
