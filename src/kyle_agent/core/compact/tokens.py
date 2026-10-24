"""真实 token 计数（替代按字符数/4 的粗略估算）。

基于 tiktoken 的 cl100k_base 编码，对纯文本与 Anthropic 形态消息列表
（messages: [{"role", "content"}]，content 为 str 或 block 列表）做精确
token 记账，供上下文水位检测、compact 压缩预估与 prompt 缓存指标使用。
"""

from __future__ import annotations

import json

import tiktoken
from tiktoken import Encoding

# 模块级编码缓存：懒加载，避免 import 阶段付出编码表初始化开销
_ENCODING: Encoding | None = None

# 每条消息的结构开销（近似 Anthropic 官方口径的固定附加值）
_MESSAGE_OVERHEAD = 4


# 懒加载并缓存 cl100k_base 编码实例，进程内只初始化一次
def _get_encoding() -> Encoding:
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


# 统计纯文本的 token 数（cl100k_base 编码），空串返回 0
def count_text(text: str) -> int:
    if not text:
        return 0
    # encode_ordinary 不会因文本中出现特殊标记（如 <|endoftext|>）而抛错
    return len(_get_encoding().encode_ordinary(text))


# 统计单个 content block 的 token 数，按 block 类型选取对应字段计数
def _count_block(block: object) -> int:
    if not isinstance(block, dict):
        return 0
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text")
        return count_text(text) if isinstance(text, str) else 0
    if block_type == "thinking":
        thinking = block.get("thinking")
        return count_text(thinking) if isinstance(thinking, str) else 0
    if block_type == "tool_use":
        name = block.get("name")
        tool_input = block.get("input")
        name_text = name if isinstance(name, str) else ""
        input_text = (
            json.dumps(tool_input, ensure_ascii=False)
            if isinstance(tool_input, dict)
            else ""
        )
        return count_text(name_text) + count_text(input_text)
    if block_type == "tool_result":
        content = block.get("content")
        return count_text(content) if isinstance(content, str) else 0
    return 0


# 统计 Anthropic 形态消息列表的总 token 数（含每条消息 +4 的近似结构开销）
def count_messages(messages: list[dict[str, object]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += count_text(content)
        elif isinstance(content, list):
            for block in content:
                total += _count_block(block)
        # content 缺失或形态未知时不计内容，仅保留下方的结构开销。
        # 近似说明：Anthropic 官方对每条消息附加固定结构开销，这里取 +4 与其
        # 口径相近即可，目标是水位检测/压缩预估的相对精度，而非逐 token 复刻。
        total += _MESSAGE_OVERHEAD
    return total
