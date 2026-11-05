"""RAG 共享分词:HashingEmbedder 特征提取与 rerank 词重叠共用同一实现。

特征粒度:CJK 字符输出单字 + 相邻二字 2-gram(单字是中文最小语义单元,
纯 2-gram 会让短查询的字面泛化能力归零);ASCII 连续字母数字段按整词小写。
该函数是检索语义的基石,embedder 与 rerank 必须共用,
避免两套分词导致打分口径漂移。
"""
from __future__ import annotations


# 判定字符是否为 CJK 统一表意文字(基本区)
def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


# 把文本切分为特征 token 序列:CJK 单字+2-gram + ASCII 整词(小写)
def tokenize(text: str) -> list[str]:
    out: list[str] = []
    cjk_run: list[str] = []
    ascii_run: list[str] = []

    # 冲刷累积缓冲:ASCII 词整词落盘,CJK run 输出全部单字与相邻 2-gram
    def _flush() -> None:
        if ascii_run:
            out.append("".join(ascii_run))
            ascii_run.clear()
        if cjk_run:
            out.extend(cjk_run)
            for i in range(len(cjk_run) - 1):
                out.append(cjk_run[i] + cjk_run[i + 1])
            cjk_run.clear()

    for raw in text:
        ch = raw.lower()
        if _is_cjk(ch):
            if ascii_run:
                _flush()
            cjk_run.append(ch)
        elif ch.isascii() and ch.isalnum():
            if cjk_run:
                _flush()
            ascii_run.append(ch)
        else:
            _flush()
    _flush()
    return out
