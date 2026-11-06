from __future__ import annotations

from pathlib import Path

from kyle_agent.core.config import KnowledgeConfig, KyleConfig
from kyle_agent.core.tools.builtin.knowledge_save import KnowledgeSaveTool


# 构造绑定 tmp 知识库目录的工具(显式传目录,测试不触碰真实 ~/.kyle)
def _tool(tmp_path: Path) -> KnowledgeSaveTool:
    cfg = KyleConfig(knowledge=KnowledgeConfig(dir=str(tmp_path)))
    return KnowledgeSaveTool(cfg)


# 功能:验证 knowledge_save 正常调用会把 content 写入知识库目录的 <title>.md
# 设计:显式绑定 tmp 目录,断言返回摘要三要素与磁盘文件内容逐字节一致,
#       覆盖"配置目录解析 → 写盘 → 返回"的完整路径
async def test_knowledge_save_writes_markdown(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.invoke({"title": "memory leak playbook", "content": "# steps\n\nrestart first"})

    assert not result.is_error
    assert "memory leak playbook.md" in result.content
    assert "title: memory leak playbook" in result.content
    assert "chars:" in result.content
    saved = tmp_path / "memory leak playbook.md"
    assert saved.read_text(encoding="utf-8") == "# steps\n\nrestart first"


# 功能:验证中文标题原样保留(不做 slug 化,CJK 不被清空)
# 设计:用纯中文标题调用,断言磁盘文件名与内容都为中文原样——
#       运行时知识库的主要沉淀语言是中文,slug 化会破坏这一核心场景
async def test_knowledge_save_keeps_cjk_title(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.invoke({"title": "网关5xx排障手册", "content": "症状:5xx 飙升"})

    assert not result.is_error
    assert (tmp_path / "网关5xx排障手册.md").read_text(encoding="utf-8") == "症状:5xx 飙升"


# 功能:验证标题中的文件名非法字符被替换为连字符(跨平台安全文件名)
# 设计:构造含 / : * 的标题,断言净化后的文件名逐字符正确且文件真实落盘
async def test_knowledge_save_sanitizes_unsafe_chars(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.invoke({"title": "a/b:c*d", "content": "x"})

    assert not result.is_error
    assert "saved to knowledge base: a-b-c-d.md" in result.content
    assert (tmp_path / "a-b-c-d.md").exists()


# 功能:验证净化后为空的标题(纯点号/全斜杠)返回 runtime_error 且不写文件
# 设计:两类无信息标题都断言 is_error 与 error_type,并确认目录仍为空——
#       防止空文件名或全连字符文件名污染知识库目录
async def test_knowledge_save_rejects_empty_sanitized_title(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    for bad_title in ("...", "///"):
        result = await tool.invoke({"title": bad_title, "content": "x"})
        assert result.is_error, bad_title
        assert result.error_type == "runtime_error", bad_title
    assert list(tmp_path.iterdir()) == []


# 功能:验证空白 content 返回 runtime_error 且不写文件
# 设计:schema 只约束非空字符串,业务层 strip 后空白仍须拒绝(schema 之外的业务校验)
async def test_knowledge_save_rejects_blank_content(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.invoke({"title": "t", "content": "   "})

    assert result.is_error
    assert result.error_type == "runtime_error"
    assert list(tmp_path.iterdir()) == []


# 功能:验证同标题两次保存为幂等覆写(第二次内容覆盖第一次)
# 设计:回放时工具重执行,覆写幂等是 cassette 确定性的前提;断言文件数仍为 1
async def test_knowledge_save_same_title_overwrites(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    await tool.invoke({"title": "playbook", "content": "v1"})
    result = await tool.invoke({"title": "playbook", "content": "v2"})

    assert not result.is_error
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "v2"


# 功能:验证超长标题截断到 80 字符(文件名长度上限)
# 设计:构造 100 字符标题,断言落盘文件 stem 长度为 80 且首 80 字符一致,
#       防止超长文件名触发文件系统错误
async def test_knowledge_save_truncates_long_title(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    long_title = "k" * 100

    result = await tool.invoke({"title": long_title, "content": "x"})

    assert not result.is_error
    files = list(tmp_path.glob("*.md"))
    assert files[0].stem == "k" * 80
    assert f"{'k' * 80}.md" in result.content
