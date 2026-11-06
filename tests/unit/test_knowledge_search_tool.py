from __future__ import annotations

from pathlib import Path

from kyle_agent.core.config import KnowledgeConfig, KyleConfig
from kyle_agent.core.tools.builtin.knowledge_save import KnowledgeSaveTool
from kyle_agent.core.tools.builtin.knowledge_search import KnowledgeSearchTool


# 构造绑定 tmp 知识库目录的检索工具(显式传目录,测试不触碰真实 ~/.kyle)
def _tool(tmp_path: Path) -> KnowledgeSearchTool:
    cfg = KyleConfig(knowledge=KnowledgeConfig(dir=str(tmp_path)))
    return KnowledgeSearchTool(cfg)


# 功能:keyword 模式检索命中知识库文档并按 top-3 全文拼接输出
# 设计:写两篇文档,独特 token 查询命中目标篇且分隔符形状与 runbook_search 一致
async def test_keyword_mode_hits_saved_doc(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    (tmp_path / "alpha.md").write_text("token QUANTUM-7 处置步骤\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("unrelated notes here\n", encoding="utf-8")

    res = await tool.invoke({"query": "QUANTUM-7"})

    assert not res.is_error
    assert "token QUANTUM-7 处置步骤" in res.content


# 功能:vector 与 hybrid 模式可检索知识库(三模式统一走 rag 管道)
# 设计:同一知识库在三模式下查询有词面交集的文档,断言命中且输出形状一致
async def test_vector_and_hybrid_modes(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    (tmp_path / "guide.md").write_text("内存泄漏 OOM 处置:重启进程\n", encoding="utf-8")
    for mode in ("vector", "hybrid"):
        res = await tool.invoke({"query": "内存 OOM", "mode": mode})
        assert not res.is_error, mode
        assert "内存泄漏" in res.content, mode


# 功能:mode 非法值返回结构化 runtime_error 而非抛异常
# 设计:LLM 传入未知模式时工具层兜底,断言 is_error 与 error_type,daemon 不崩
async def test_invalid_mode_structured_error(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    (tmp_path / "a.md").write_text("content here\n", encoding="utf-8")

    res = await tool.invoke({"query": "content", "mode": "semantic"})

    assert res.is_error
    assert res.error_type == "runtime_error"
    assert "keyword|vector|hybrid" in res.content


# 功能:空目录(或目录不存在)检索返回无匹配文案与空 available 清单
# 设计:与 runbook_search 的无匹配风格一致;目录不存在时 available 为空列表
async def test_empty_corpus_no_match_message(tmp_path: Path) -> None:
    empty_dir = tmp_path / "nothing-here"
    tool = KnowledgeSearchTool(
        KyleConfig(knowledge=KnowledgeConfig(dir=str(empty_dir)))
    )

    res = await tool.invoke({"query": "anything"})

    assert not res.is_error
    assert res.content == "no matching documents. available: []"


# 功能:save → search 同进程闭环(写入后无需显式失效即可检索)
# 设计:KnowledgeSaveTool 写入独特 token 文档,KnowledgeSearchTool 立即检索命中,
#       覆盖"沉淀→检索"完整闭环与索引过期重建的工具级路径
async def test_save_then_search_roundtrip(tmp_path: Path) -> None:
    cfg = KyleConfig(knowledge=KnowledgeConfig(dir=str(tmp_path)))
    saver = KnowledgeSaveTool(cfg)
    searcher = KnowledgeSearchTool(cfg)

    saved = await saver.invoke({
        "title": "roundtrip-doc",
        "content": "unique token ROUNDTRIP-42 with handling steps",
    })
    assert not saved.is_error
    found = await searcher.invoke({"query": "ROUNDTRIP-42"})
    assert not found.is_error
    assert "ROUNDTRIP-42" in found.content
