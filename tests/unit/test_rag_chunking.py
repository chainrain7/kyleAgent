from __future__ import annotations

from kyle_agent.core.rag.chunking import Chunk, chunk_markdown


# 功能:按 ATX 标题切分并保留标题在 chunk 内容内,层级路径正确
# 设计:构造三级标题文档(每节正文 >50 字符避免触发碎块合并),
#       断言 chunk 数、title、section_path 与内容首行,
#       覆盖 H1/H2/H3 全层级路径拼接
def test_headings_split_and_section_path() -> None:
    body = "这是足够长的正文内容,确保每个分片都不会被当作碎块合并掉。" * 3
    doc = (
        f"# 内存泄漏\n\n{body}\n\n## 触发特征\n\n{body}\n\n"
        f"### 日志特征\n\n{body}\n"
    )
    chunks = chunk_markdown(doc, "memory-leak.md")
    assert [c.title for c in chunks] == [
        "内存泄漏", "触发特征", "日志特征",
    ]
    assert chunks[0].section_path == "内存泄漏"
    assert chunks[1].section_path == "内存泄漏 > 触发特征"
    assert chunks[2].section_path == "内存泄漏 > 触发特征 > 日志特征"
    assert chunks[1].content.startswith("## 触发特征")
    assert all(c.source == "memory-leak.md" for c in chunks)
    assert [c.index for c in chunks] == [0, 1, 2]


# 功能:超长段落被二次切分,单 chunk 不超过 800 字符上限
# 设计:单个无标题段落塞 2000 字符,断言产出多个 chunk 且长度全部 ≤800,
#       覆盖硬切分支(单段无空行可依)
def test_long_paragraph_split_at_limit() -> None:
    doc = "# 大块\n\n" + "x" * 2000 + "\n"
    chunks = chunk_markdown(doc, "big.md")
    assert len(chunks) > 1
    assert all(len(c.content) <= 800 for c in chunks)
    assert "".join(c.content for c in chunks).startswith("# 大块")


# 功能:小于 50 字符的碎块向后合并,末尾碎块向前并入
# 设计:两个正常节 + 末尾一个碎块节,断言碎块被并入相邻 chunk,
#       覆盖合并后不再有 <50 的块
def test_small_fragments_merged() -> None:
    body = "这一段正文足够长,用来保证前置 chunk 不会触发碎块合并逻辑。" * 2
    doc = (
        f"# 文档\n\n## 第一节\n\n{body}\n\n## 禁止\n\n短。\n"
    )
    chunks = chunk_markdown(doc, "doc.md")
    assert len(chunks) == 1  # 碎块向后无块可并,向前并入第一节
    assert "禁止" in chunks[0].content and "短。" in chunks[0].content


# 功能:无任何标题的纯文本整体成单 chunk,路径为空
# 设计:零标题输入走兜底分支,断言单 chunk、title 为空、section_path 为空
def test_plain_text_single_chunk() -> None:
    chunks = chunk_markdown("纯文本内容没有标题行。\n" * 10, "plain.md")
    assert len(chunks) == 1
    assert chunks[0].title == ""
    assert chunks[0].section_path == ""
    assert isinstance(chunks[0], Chunk)
