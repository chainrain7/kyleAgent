from __future__ import annotations

from kyle_agent.core.rag.chunking import Chunk
from kyle_agent.core.rag.rerank import rerank


# 构造候选 chunk 的便捷工厂
def _mk(content: str, title: str = "t") -> Chunk:
    return Chunk(content=content, source="a.md", title=title,
                 section_path="", index=0)


# 功能:词重叠(Jaccard)在余弦相当时主导排序
# 设计:两个候选余弦同为 0.5,与查询共享特征多者应排前,
#       验证 0.3 权重的词面信号真实参与打分
def test_jaccard_lifts_overlap_candidate() -> None:
    cands = [(_mk("数据库 连接 超时 重试"), 0.5), (_mk("磁盘 空间 已满 清理"), 0.5)]
    out = rerank("数据库连接超时", cands, 2)
    assert out[0][0].content.startswith("数据库")


# 功能:标题命中获得 0.1 加分,可在余弦劣势下翻盘
# 设计:候选 A 余弦 0.5 但标题含查询词,候选 B 余弦 0.55 无标题命中,
#       断言总分 0.6*0.5+0.1 > 0.6*0.55,加分机制真实生效
def test_title_bonus_beats_higher_cosine() -> None:
    a = Chunk(content="正文不含查询词的特征", source="a.md",
              title="磁盘满", section_path="", index=0)
    b = Chunk(content="磁盘满 相关正文内容也出现", source="b.md",
              title="无关标题", section_path="", index=1)
    out = rerank("磁盘满", [(a, 0.5), (b, 0.55)], 2)
    assert out[0][0].source == "a.md"


# 功能:IDF 加权 Jaccard 抑制高频字虚增重叠率
# 设计:构造高频字(低 idf)与稀词(高 idf)两候选,等价重叠数下
#       稀词候选得分更高,验证加权重叠的真实区分力
def test_idf_weighted_jaccard() -> None:
    idf = {"的": 0.2, "泄漏": 3.0}
    a = _mk("的 的 泄漏")
    b = _mk("的 的 的")
    out = rerank("泄漏", [(a, 0.0), (b, 0.0)], 2, idf=idf)
    assert out[0][0] is a


# 功能:无 idf 表时等权退化路径可用
# 设计:不传 idf 走等权 1.0 分支,输出有序且不抛错,保证向后兼容
def test_no_idf_defaults_equal_weight() -> None:
    cands = [(_mk("连接 池 耗尽"), 0.4), (_mk("无关内容"), 0.6)]
    out = rerank("连接池耗尽", cands, 2)
    assert out[0][0].content.startswith("连接")
