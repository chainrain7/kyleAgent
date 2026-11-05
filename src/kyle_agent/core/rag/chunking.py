"""Markdown 分片:按 ATX 标题切 chunk,供向量化与重排使用。

规则:标题行(## /###)开启新 chunk 且标题保留在 chunk 内容内;
单 chunk 上限 800 字符(超长正文按空行段落二次切分,单段仍超长则硬切);
小于 50 字符的碎块向后合并(末尾碎块向前并入);
section_path 记录标题层级路径(如 "内存泄漏 > 排查步骤")。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_CHUNK_CHARS = 800
_MIN_CHUNK_CHARS = 50
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


@dataclass
class Chunk:
    content: str
    source: str   # 来源文件名(如 memory-leak.md)
    title: str    # 本 chunk 最近一级标题文本
    section_path: str  # 标题层级路径,如 "内存泄漏 > 排查步骤"
    index: int    # 文档内序号(0 起)


# 把 markdown 文本按 ATX 标题切分为带层级路径的 chunk 列表
def chunk_markdown(text: str, source: str) -> list[Chunk]:
    # 第一步:按标题行切成 (title, title_line, body_lines, path) 段
    sections: list[tuple[str, str, list[str], str]] = []
    stack: dict[int, str] = {}
    cur_title = ""
    cur_line = ""
    cur_body: list[str] = []
    cur_path = ""

    # 追加一个已完结 section(标题与正文分离存放)
    def _close() -> None:
        if cur_line or cur_body:
            sections.append((cur_title, cur_line, cur_body, cur_path))

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            _close()
            depth = len(m.group(1))
            title_text = m.group(2).strip()
            stack[depth] = title_text
            for d in list(stack):
                if d > depth:
                    del stack[d]
            cur_title = title_text
            cur_line = line.strip()
            cur_body = []
            cur_path = " > ".join(stack[d] for d in sorted(stack))
        else:
            cur_body.append(line)
    _close()

    # 无任何标题的纯文本:整体作为一个 section(路径为空)
    if not sections:
        sections = [("", "", text.splitlines(), "")]

    # 第二步:section 正文超长时按空行段落二次切分(预算计入标题行长度)
    raw: list[tuple[str, str, str]] = []  # (title_line, body_text, path)
    for title, title_line, body, path in sections:
        budget = _MAX_CHUNK_CHARS - (len(title_line) + 1 if title_line else 0)
        pieces = _split_body(body, max(1, budget))
        for i, piece in enumerate(pieces):
            head = title_line if i == 0 else ""
            raw.append((head, piece, path))

    # 第三步:组装 chunk(无标题行首的续块挂到前块标题);碎块向后合并
    chunks: list[Chunk] = []
    last_title_line = ""
    for head, body_text, path in raw:
        title_line = head or last_title_line
        last_title_line = title_line
        content = (title_line + "\n" + body_text).strip()
        if not content:
            continue
        title = ""
        if title_line:
            m2 = _HEADING_RE.match(title_line)
            title = m2.group(2).strip() if m2 else title_line.lstrip("# ").strip()
        chunks.append(Chunk(
            content=content, source=source, title=title,
            section_path=path, index=len(chunks),
        ))
    chunks = _merge_fragments(chunks)
    return chunks


# 把超长正文按空行分段累积为 ≤budget 字符的块;单段超长时硬切
def _split_body(body_lines: list[str], budget: int = _MAX_CHUNK_CHARS) -> list[str]:
    body = "\n".join(body_lines).strip()
    if len(body) <= budget:
        return [body] if body else []
    pieces: list[str] = []
    buf = ""
    # 空行分段累积;单段本身超长则按上限硬切
    for para in re.split(r"\n\s*\n", body):
        while len(para) > budget:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(para[:budget])
            para = para[budget:]
        if not para:
            continue
        cand = para if not buf else buf + "\n\n" + para
        if len(cand) <= budget:
            buf = cand
        else:
            if buf:
                pieces.append(buf)
            buf = para
    if buf:
        pieces.append(buf)
    return pieces


# 小于 50 字符的碎块向后合并,末尾碎块向前并入
def _merge_fragments(chunks: list[Chunk]) -> list[Chunk]:
    if len(chunks) <= 1:
        return chunks
    out: list[Chunk] = []
    for ch in chunks:
        if out and len(out[-1].content) < _MIN_CHUNK_CHARS:
            prev = out[-1]
            out[-1] = Chunk(
                content=prev.content + "\n" + ch.content,
                source=prev.source, title=prev.title,
                section_path=prev.section_path, index=prev.index,
            )
        else:
            out.append(ch)
    # 遍历后末尾若仍是碎块,并入前一块
    if len(out) > 1 and len(out[-1].content) < _MIN_CHUNK_CHARS:
        prev = out[-2]
        out[-2] = Chunk(
            content=prev.content + "\n" + out[-1].content,
            source=prev.source, title=prev.title,
            section_path=prev.section_path, index=prev.index,
        )
        out.pop()
    return out
