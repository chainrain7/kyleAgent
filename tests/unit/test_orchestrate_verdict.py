from __future__ import annotations

from kyle_agent.core.orchestrate.verdict import ReviewVerdict, parse_verdict


# 功能:三种合法裁决行(ACHIEVED/PARTIAL/FAILED)应被正确解析
# 设计:构造标准 reviewer 输出,断言裁决枚举值逐一对应
def test_parse_three_verdicts() -> None:
    for raw, expected in [
        ("## 结论\n达成\n\nVERDICT: ACHIEVED", ReviewVerdict.ACHIEVED),
        ("## 结论\n部分达成\n\nVERDICT: PARTIAL", ReviewVerdict.PARTIAL),
        ("## 结论\n未达成\n\nVERDICT: FAILED", ReviewVerdict.FAILED),
    ]:
        verdict, _ = parse_verdict(raw)
        assert verdict == expected


# 功能:裁决行大小写不敏感
# 设计:用小写/混合大小写的裁决行,断言均归一化为对应枚举
def test_parse_case_insensitive() -> None:
    for raw, expected in [
        ("done\nverdict: achieved", ReviewVerdict.ACHIEVED),
        ("so-so\nVerdict: Partial", ReviewVerdict.PARTIAL),
        ("bad\nVERDICT: failed", ReviewVerdict.FAILED),
    ]:
        verdict, _ = parse_verdict(raw)
        assert verdict == expected


# 功能:裁决行末尾允许句号(中英文)与尾随空白
# 设计:在裁决行追加"。"与"."变体,断言仍能解析出对应裁决
def test_parse_trailing_punctuation() -> None:
    for raw, expected in [
        ("VERDICT: FAILED。", ReviewVerdict.FAILED),
        ("VERDICT: FAILED.", ReviewVerdict.FAILED),
        ("VERDICT: ACHIEVED 。", ReviewVerdict.ACHIEVED),
    ]:
        verdict, _ = parse_verdict(raw)
        assert verdict == expected


# 功能:无 VERDICT 行时保守回退 PARTIAL
# 设计:reviewer 输出不含任何裁决行,断言返回 PARTIAL(继续 replan 直到上限)
def test_parse_missing_verdict_falls_back_partial() -> None:
    verdict, findings = parse_verdict("## 结论\n未达成\n目标没有完成。")
    assert verdict == ReviewVerdict.PARTIAL
    assert "目标没有完成" in findings


# 功能:发现段落原文提取——"## 发现"到下一个标题之间的内容
# 设计:标准三段式输出,断言 findings 只含"发现"段,不含结论/建议与裁决行
def test_parse_extracts_findings_section() -> None:
    raw = (
        "## 结论\n未达成\n\n## 发现\n- calc.py 的 add 仍是减法\n- 测试仍红\n\n"
        "## 建议\n修复 add\n\nVERDICT: FAILED"
    )
    verdict, findings = parse_verdict(raw)
    assert verdict == ReviewVerdict.FAILED
    assert "add 仍是减法" in findings
    assert "测试仍红" in findings
    assert "修复 add" not in findings
    assert "VERDICT" not in findings


# 功能:无"## 发现"段时回退为去除裁决行后的全文
# 设计:输出只有结论与裁决行,断言 findings 含正文且不含裁决行
def test_parse_findings_fallback_full_text() -> None:
    raw = "目标未达成:文件不存在。\nVERDICT: FAILED"
    verdict, findings = parse_verdict(raw)
    assert verdict == ReviewVerdict.FAILED
    assert "文件不存在" in findings
    assert "VERDICT" not in findings


# 功能:裁决行后有空行时仍按"最后一个非空行"解析
# 设计:末尾追加空白行,断言裁决不被空白行破坏
def test_parse_verdict_with_trailing_blank_lines() -> None:
    verdict, _ = parse_verdict("结果不错\nVERDICT: ACHIEVED\n\n  \n")
    assert verdict == ReviewVerdict.ACHIEVED


# 功能:裁决行后附加少量收尾语时(窗口内)仍可解析
# 设计:VERDICT 在倒数第 3 个非空行,后跟 2 行普通文本——模型常在裁决后补一句,
# 引擎对末尾 5 个非空行窗口内的裁决行容错
def test_parse_verdict_within_trailing_window() -> None:
    raw = "VERDICT: FAILED\n以上问题需在下一轮修复。\n请优先处理第一项。"
    verdict, findings = parse_verdict(raw)
    assert verdict == ReviewVerdict.FAILED
    # 发现段回退为去除裁决行后的全文
    assert "请优先处理第一项" in findings


# 功能:裁决行超出末尾窗口时视为未给出裁决,回退 PARTIAL
# 设计:VERDICT 出现在倒数第 6 个非空行(窗口外),其后还有 6 行正文——防止
# 正文夹带的同类字样被误读为裁决
def test_parse_verdict_outside_window_falls_back_partial() -> None:
    raw = "VERDICT: ACHIEVED\n" + "\n".join(f"第{i}条补充说明。" for i in range(1, 7))
    verdict, _ = parse_verdict(raw)
    assert verdict == ReviewVerdict.PARTIAL
