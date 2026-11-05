from __future__ import annotations

import pytest

from kyle_agent.core.rag.chunking import Chunk
from kyle_agent.core.rag.store import VectorStore


# 构造测试 chunk 的便捷工厂
def _mk(idx: int) -> Chunk:
    return Chunk(content=f"c{idx}", source=f"s{idx}.md", title="t",
                 section_path="", index=idx)


# 功能:余弦相似度排序正确——与查询向量越近排名越高
# 设计:用坐标轴单位向量构造可控余弦(1.0/0.7/0.0),
#       断言返回顺序与分数,线性扫描的数值行为可直接验证
def test_cosine_ranking() -> None:
    store = VectorStore()
    store.add([_mk(0), _mk(1), _mk(2)], [[1.0, 0.0], [0.7, 0.7], [0.0, 1.0]])
    res = store.search([1.0, 0.0], 3)
    assert [c.index for c, _ in res] == [0, 1, 2]
    assert res[0][1] == pytest.approx(1.0)
    assert res[1][1] == pytest.approx(1 / (2 ** 0.5), abs=1e-9)  # [0.7,0.7] 归一后


# 功能:top_k 截断与零向量安全(分母为零不崩溃、给 0 分)
# 设计:库 3 条只取 top 1;零向量查询走零向量分支断言分数为 0,
#       覆盖除零保护路径
def test_topk_and_zero_vector() -> None:
    store = VectorStore()
    store.add([_mk(0), _mk(1), _mk(2)], [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    res = store.search([1.0, 1.0], 1)
    assert len(res) == 1 and res[0][0].index == 2
    res0 = store.search([0.0, 0.0], 3)
    assert all(s == 0.0 for _, s in res0)


# 功能:add 时 chunks 与 vectors 长度不一致抛 ValueError
# 设计:参数校验分支,长度不匹配必须显式失败而非静默错位入库
def test_add_length_mismatch() -> None:
    store = VectorStore()
    with pytest.raises(ValueError, match="length mismatch"):
        store.add([_mk(0), _mk(1)], [[1.0, 0.0]])
