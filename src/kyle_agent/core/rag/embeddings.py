"""嵌入层:确定性本地哈希嵌入 + OpenAI 兼容 API 嵌入(带磁盘缓存)。

零第三方依赖红线:HashingEmbedder 纯标准库(特征哈希用 hashlib.blake2b,
禁用内建 hash——每进程加盐不定),向量运算纯 Python math,不引 numpy。
OpenAICompatEmbedder 仅在生产配置下启用;磁盘缓存命中后零网络请求,
保证首次调用之后离线可复现。
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from hashlib import blake2b
from pathlib import Path
from typing import Protocol

import httpx

from kyle_agent.core.rag._tokenize import tokenize

_DEFAULT_CACHE_DIR = "~/.kyle/cache/embeddings"


class EmbeddingProvider(Protocol):
    """嵌入提供方协议:批量文本 → 向量列表(与输入同序)。"""

    # 批量嵌入文本(实现方保证返回顺序与输入一致)
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    dim: int
    name: str


class HashingEmbedder:
    """确定性特征哈希嵌入:blake2b 映射桶 + 符号位 + 可选 IDF 加权 + L2 归一化。

    idf 由索引方(pipeline)按库内文档频率统计后 set_idf 注入——高频汉字
    (的/理/量)不加权会让噪声分压过真信号;无表时等权 1.0,行为退化为纯哈希。
    """

    # 固定名称与维度(跨进程/跨平台可复现的关键是特征函数与哈希均确定)
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hashing-v1-{dim}"
        self.idf: dict[str, float] = {}
        self.default_idf = 1.0

    # 注入 IDF 权重表与未见特征缺省权重(库级统计,确定性纯函数)
    def set_idf(self, idf: dict[str, float], default_w: float) -> None:
        self.idf = idf
        self.default_idf = default_w

    # 批量嵌入:每文本独立计算,返回顺序与输入一致
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    # 单文本嵌入:特征计数 → sqrt 压缩 tf × IDF 权重入桶,L2 归一化
    # (sublinear tf:线性累加会让高频单字主导模长,压过高 idf 区分词)
    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat, cnt in Counter(tokenize(text)).items():
            digest = blake2b(feat.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest, "big") % self.dim
            sign = 1.0 if digest[0] & 0x80 else -1.0
            vec[idx] += sign * math.sqrt(cnt) * self.idf.get(feat, self.default_idf)
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            return [0.0] * self.dim
        return [x / norm for x in vec]


class OpenAICompatEmbedder:
    """OpenAI 兼容 /embeddings 端点嵌入,磁盘缓存(模型+文本 hash 命中即零网络)。"""

    # 构造同步 httpx 客户端;api_key 缺失即抛错(生产配置问题要尽早暴露)
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        key = api_key or os.environ.get("KYLE_RAG_EMBEDDING_API_KEY", "")
        if not key:
            raise ValueError(
                "embedding api_key missing: set rag.embedding_api_key or "
                "KYLE_RAG_EMBEDDING_API_KEY (required when "
                'rag.embedding_provider = "openai")'
            )
        self.dim = 0  # 首次响应后回填(API 嵌入维度由服务端决定)
        self.name = f"openai-{model}"
        self._model = model
        self._cache_dir = Path(cache_dir or _DEFAULT_CACHE_DIR).expanduser()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = http_client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=60.0,
        )

    # 批量嵌入:先查磁盘缓存,只对未命中的文本发一次批量请求
    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, t in enumerate(texts):
            cached = self._read_cache(t)
            if cached is not None:
                out[i] = cached
            else:
                missing.append(i)
        if missing:
            payload = {"model": self._model, "input": [texts[i] for i in missing]}
            resp = self._client.post("/embeddings", json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"embedding api error {resp.status_code}: {resp.text[:500]}"
                )
            data = resp.json()["data"]
            # 响应按 index 回填并写缓存,保证输出顺序与输入一致
            for item in data:
                i = missing[int(item["index"])]
                vec = [float(x) for x in item["embedding"]]
                out[i] = vec
                self._write_cache(texts[i], vec)
        if self.dim == 0 and out and out[0] is not None:
            self.dim = len(out[0])
        return [v if v is not None else [] for v in out]

    # 缓存文件名:blake2b(模型 + \x00 + 文本) 十六进制
    def _cache_path(self, text: str) -> Path:
        digest = blake2b(
            (self._model + "\x00" + text).encode("utf-8"), digest_size=16
        ).hexdigest()
        return self._cache_dir / f"{digest}.json"

    # 读缓存:命中返回向量,未命中返回 None
    def _read_cache(self, text: str) -> list[float] | None:
        path = self._cache_path(text)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [float(x) for x in raw]
        except (json.JSONDecodeError, OSError, ValueError):
            return None  # 缓存损坏按未命中处理,重新请求覆盖

    # 写缓存:JSON 数组落盘(显式 UTF-8,Windows 兼容红线)
    def _write_cache(self, text: str, vec: list[float]) -> None:
        self._cache_path(text).write_text(
            json.dumps(vec), encoding="utf-8"
        )
