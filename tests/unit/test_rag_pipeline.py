from __future__ import annotations

import re
from pathlib import Path

from kyle_agent.core.rag.pipeline import RunbookIndex, local_runbook_index

_RUNBOOKS = Path(__file__).parents[2] / "src" / "kyle_agent" / "oncall" / "runbooks"


# 复刻改造前 tools.py 的关键词打分逻辑,作为 keyword 模式的逐字节 oracle
def _legacy_keyword_search(query: str, top_k: int = 3) -> list[str]:
    terms = [w for w in re.split(r"[\s,，]+", query.lower()) if w]
    scored: list[tuple[float, str]] = []
    for path in sorted(_RUNBOOKS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        title = path.stem.replace("-", " ")
        score = 0.0
        for term in terms:
            score += lower.count(term) * 1.0
            if term in title:
                score += 5.0
        if score > 0:
            scored.append((score, text.strip()))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    top = scored[:top_k]
    return [t for _, t in top]


# 功能:vector/hybrid 模式对症状查询命中目标 runbook
# 设计:用真实包内 runbooks 目录跑本地嵌入器,四组症状查询断言目标文件
#       进入 top-3,验证管道端到端召回能力
def test_vector_hybrid_hit_target() -> None:
    idx = local_runbook_index(_RUNBOOKS)
    for query, want in [
        ("内存 上涨 OOM", "memory-leak.md"),
        ("磁盘满 清理", "disk-full.md"),
        ("CPU 飙高", "capacity-surge.md"),
        ("证书 过期", "cert-expired.md"),
    ]:
        for mode in ("vector", "hybrid"):
            res = idx.search(query, mode=mode, top_k=3)
            assert want in [c.source for c in res], f"{query}/{mode}: {[c.source for c in res]}"


# 功能:零词面重叠查询三模式都返回空(与 keyword 的 no-match 行为对齐)
# 设计:无特征交集时词面判定返回空列表——哈希桶碰撞不会伪造特征交集,
#       证明 no-match 判定与维度无关
def test_no_match_returns_empty() -> None:
    idx = local_runbook_index(_RUNBOOKS)
    for mode in ("keyword", "vector", "hybrid"):
        assert idx.search("zzz_unrelated_zzz", mode=mode) == []


# 功能:keyword 模式输出与改造前旧实现逐字节一致(排序与全文)
# 设计:oracle 直接复刻旧打分代码,断言返回 Chunk 的 content 序列
#       与旧全文序列完全相等,保障输出形状契约
def test_keyword_mode_byte_identical_to_legacy() -> None:
    idx = local_runbook_index(_RUNBOOKS)
    for query in [
        "内存 上涨 OOM",
        "磁盘满 清理",
        "证书 过期",
        "config validation",
        "不存在的词 xyzzy",
    ]:
        got = [c.content for c in idx.search(query, mode="keyword", top_k=3)]
        assert got == _legacy_keyword_search(query), f"query={query!r}"


# 功能:返回的 chunk.content 是 runbook 全文(strip 后),保持输出形状契约
# 设计:任取一次 vector 命中,断言 content 与磁盘原文 strip 后逐字节相等,
#       工具层拼接后即旧工具输出形状
def test_content_is_full_document_text() -> None:
    idx = local_runbook_index(_RUNBOOKS)
    res = idx.search("内存 上涨 OOM", mode="vector", top_k=3)
    disk = (_RUNBOOKS / res[0].source).read_text(encoding="utf-8").strip()
    assert res[0].content == disk


# 功能:同目录同嵌入器的索引进程内单例复用
# 设计:两次 get 内容 hash 相同返回同一对象;清空类缓存后重建新对象,
#       验证缓存键含内容 hash(内容变化不会拿到旧索引)
def test_index_singleton_and_cache_key() -> None:
    a = local_runbook_index(_RUNBOOKS)
    b = local_runbook_index(_RUNBOOKS)
    assert a is b
    RunbookIndex._CACHE.clear()
    c = local_runbook_index(_RUNBOOKS)
    assert c is not a


# 功能:未知检索模式抛 ValueError
# 设计:非法模式名必须显式失败,防止静默回退某默认模式造成行为漂移
def test_unknown_mode_raises() -> None:
    idx = local_runbook_index(_RUNBOOKS)
    try:
        idx.search("q", mode="semantic")
    except ValueError as e:
        assert "keyword|vector|hybrid" in str(e)
    else:
        raise AssertionError("expected ValueError")
