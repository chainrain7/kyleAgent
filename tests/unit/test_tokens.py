from __future__ import annotations

from kyle_agent.core.compact.tokens import count_messages, count_text


# 功能：验证空输入返回 0
# 设计：空字符串与空消息列表分别调用，断言计数均为 0
def test_empty_inputs_return_zero() -> None:
    assert count_text("") == 0
    assert count_messages([]) == 0


# 功能：验证 count_text 对英文/中文/代码片段计数大于 0，且与字符数的方向关系符合 cl100k 特性
# 设计：英文与代码因词级打包 token 数小于字符数；中文常见汉字常被拆成 1~2 个 token，token 数大于字符数
def test_count_text_positive_and_direction_vs_chars() -> None:
    english = "The quick brown fox jumps over the lazy dog."
    chinese = "你好，世界！这是一段用于测试的中文文本，包含若干常见汉字。"
    code = "def add(a, b):\n    return a + b"

    assert count_text(english) > 0
    assert count_text(chinese) > 0
    assert count_text(code) > 0

    # 英文/代码：多个字符被打包进同一个 token，计数小于字符数
    assert count_text(english) < len(english)
    assert count_text(code) < len(code)
    # 中文：cl100k 下常见汉字经常拆成多 token，计数大于字符数（实测 29 字符 -> 32 token）
    assert count_text(chinese) > len(chinese)


# 功能：验证同一文本重复计数结果稳定
# 设计：对同一字符串（含代码片段）连续调用两次 count_text，断言两次结果相等
def test_count_text_stable() -> None:
    text = "def compact(messages):\n    return [m for m in messages if keep(m)]"
    assert count_text(text) == count_text(text)


# 功能：验证混合消息列表（user 纯文本、assistant 含 thinking/text/tool_use、user 含 tool_result）计数合理
# 设计：断言总计数满足可加性（等于逐条单消息计数之和），且大于任一单条消息的计数
def test_count_messages_mixed_additive_and_dominant() -> None:
    tool_input = {"path": "src/main.py", "limit": 100}
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "请帮我读取 main.py 并统计行数"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "用户要读取文件，先调用 read_file 工具"},
                {"type": "text", "text": "好的，我来读取该文件。"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": tool_input,
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "line 1 ... line 100 (file content omitted)",
                }
            ],
        },
    ]

    total = count_messages(messages)
    per_message = [count_messages([m]) for m in messages]

    assert all(n > 0 for n in per_message)  # 每条消息单独计数均为正
    assert total == sum(per_message)  # 可加性：总数等于逐条之和
    assert total > max(per_message)  # 混合后总量大于任一单条消息


# 功能：验证每条消息附加 +4 的结构开销
# 设计：两条纯文本消息的计数应等于 2 份文本计数 + 2*4，且严格大于纯文本内容之和
def test_message_overhead_added_per_message() -> None:
    text = "same content in both messages"
    messages: list[dict[str, object]] = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": text},
    ]
    pure = 2 * count_text(text)
    assert count_messages(messages) == pure + 8
    assert count_messages(messages) > pure
