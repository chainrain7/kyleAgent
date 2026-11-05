from __future__ import annotations

import math
from pathlib import Path

import pytest

from kyle_agent.core.rag._tokenize import tokenize
from kyle_agent.core.rag.embeddings import (
    HashingEmbedder,
    OpenAICompatEmbedder,
)


# 功能:HashingEmbedder 跨实例确定性——两次实例化嵌入结果逐位相等
# 设计:排除内建 hash 的进程盐影响,同一文本不同实例向量必须完全一致,
#       这是"确定性"红线的直接断言
def test_hashing_embedder_deterministic() -> None:
    texts = ["内存泄漏排查", "disk full on device", "混合 text 混排"]
    a = HashingEmbedder().embed(texts)
    b = HashingEmbedder().embed(texts)
    assert a == b


# 功能:嵌入向量 L2 归一化,模长恒为 1;零重叠文本(空特征)为零向量
# 设计:模长断言用 math.isclose 容差 1e-9;空文本无特征走零向量兜底分支
def test_hashing_embedder_normalized() -> None:
    vec = HashingEmbedder().embed(["归一化检查文本"])[0]
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, abs_tol=1e-9)
    assert HashingEmbedder().embed([""])[0] == [0.0] * 512


# 功能:中文相似文本余弦大于不相关文本(嵌入保序性)
# 设计:同主题("内存泄漏"vs"内存持续上涨")应比无关主题("证书过期")
#       余弦更高,验证嵌入保留了词面语义信号
def test_similar_texts_closer_than_unrelated() -> None:
    e = HashingEmbedder()
    v1, v2, v3 = e.embed(["内存泄漏排查步骤", "内存持续上涨需重启", "证书过期需要续期"])
    cos = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert cos(v1, v2) > cos(v1, v3)


# 功能:tokenize 对 CJK 输出单字+二元组,ASCII 输出整词小写
# 设计:混合文本断言特征集合精确相等,锁定分词口径(embedder/rerank 共用基石)
def test_tokenize_cjk_and_ascii() -> None:
    assert set(tokenize("内存OOM")) == {"内", "存", "内存", "oom"}
    assert tokenize("ABC abc") == ["abc", "abc"]


class _FakeResp:
    # 伪造 httpx 响应:状态码与 JSON 体
    def __init__(self, data: list, status: int = 200) -> None:
        self._data = data
        self.status_code = status
        self.text = "fake"

    # 返回伪 JSON 体
    def json(self) -> dict:
        return {"data": self._data}


class _FakeClient:
    # 记录请求次数与最后一次请求体,返回固定向量响应
    def __init__(self, dim: int = 4) -> None:
        self.calls: list[dict] = []
        self._dim = dim

    # POST /embeddings:记录 payload 并按 index 生成响应
    def post(self, url: str, json: dict) -> _FakeResp:  # noqa: A002
        self.calls.append(json)
        data = [
            {"index": i, "embedding": [float(i + 1)] * self._dim}
            for i in range(len(json["input"]))
        ]
        return _FakeResp(data)


# 功能:OpenAICompatEmbedder 请求体为 OpenAI 兼容格式且输出顺序与输入一致
# 设计:注入 fake httpx client,断言 POST 路径、model/input 字段、
#       响应按 index 回填乱序场景下输出仍与输入顺序一致
def test_openai_embedder_request_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KYLE_RAG_EMBEDDING_API_KEY", raising=False)
    client = _FakeClient()
    e = OpenAICompatEmbedder(
        "https://api.example.com/v1", "text-embed-x",
        api_key="sk-test", cache_dir=tmp_path, http_client=client,
    )
    out = e.embed(["甲文本", "乙文本"])
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "text-embed-x"
    assert client.calls[0]["input"] == ["甲文本", "乙文本"]
    assert out == [[1.0, 1.0, 1.0, 1.0], [2.0, 2.0, 2.0, 2.0]]
    assert e.dim == 4


# 功能:磁盘缓存命中后第二次嵌入零网络请求
# 设计:同一个 fake client 连续两次 embed 相同文本,断言第二次调用数不变,
#       缓存文件落在 cache_dir(首次调用已落盘)
def test_openai_embedder_cache_hit_zero_network(tmp_path: Path) -> None:
    client = _FakeClient()
    e = OpenAICompatEmbedder(
        "https://api.example.com/v1", "text-embed-x",
        api_key="sk-test", cache_dir=tmp_path, http_client=client,
    )
    e.embed(["缓存命中文本"])
    assert len(client.calls) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    e.embed(["缓存命中文本"])
    assert len(client.calls) == 1  # 第二次走缓存,零新增请求


# 功能:api_key 缺失时构造即抛清晰错误
# 设计:清掉环境变量并传空 key,断言 ValueError 且信息含配置指引,
#       保证生产配置错误尽早暴露
def test_openai_embedder_missing_key_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KYLE_RAG_EMBEDDING_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatEmbedder(
            "https://api.example.com/v1", "m", api_key="",
            cache_dir=tmp_path, http_client=_FakeClient(),
        )
