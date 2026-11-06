"""评测任务集(支柱 1)。

设计原则:
- 判定用工作区终态断言 + run 结果,不依赖 LLM 打分——确定性、可离线;
- setup 全部使用确定性内容(固定文件/固定数字),保证 cassette 回放时
  工具重执行结果与录制时一致(顺序匹配的确定性前提);
- 覆盖面:只读检索 / 写创建 / bash 副作用 / 多文件编辑 / 任务规划 /
  记忆笔记 / 多工具汇总 / 修 bug(测试驱动)/ 子 Agent / 引擎级 replan。
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.harness import EvalTask

# verify 收到的关键字参数集合(具名判定函数的统一签名)
VerifyFn = Callable[..., None]


# ── 判定辅助 ──────────────────────────────────────────────────────


# 断言 run 最终文本包含指定子串(大小写不敏感)
def mentions(outcome: Any, *needles: str) -> None:
    text = (outcome.result or "").lower()
    for n in needles:
        assert n.lower() in text, f"最终回答未包含 {n!r}: {outcome.result[:200]!r}"


# 断言事件流中出现过指定类型
def events_include(events: list[dict[str, Any]], etype: str) -> None:
    assert any(e.get("type") == etype for e in events), f"事件流缺少 {etype}"


# 断言文件存在且内容精确等于(去首尾空白)
def file_equals(workspace: Path, rel: str, expected: str) -> None:
    p = workspace / rel
    assert p.exists(), f"{rel} 不存在"
    actual = p.read_text(encoding="utf-8").strip()
    assert actual == expected, f"{rel} 内容不符: {actual!r}"


# 断言文件存在且内容包含所有给定子串
def file_contains(workspace: Path, rel: str, *needles: str) -> None:
    p = workspace / rel
    assert p.exists(), f"{rel} 不存在"
    content = p.read_text(encoding="utf-8")
    for n in needles:
        assert n in content, f"{rel} 未包含 {n!r}: {content!r}"


# ── setup 函数 ────────────────────────────────────────────────────


# 准备:含 magic number 的文档
def _setup_read_report(ws: Path) -> None:
    (ws / "sample.txt").write_text(
        "# Manual\n\nThe magic number in this document is 86042.\n", encoding="utf-8"
    )


# 准备:三个候选文件,只有一个含 launch code
def _setup_locate(ws: Path) -> None:
    (ws / "alpha.txt").write_text("nothing interesting here\n", encoding="utf-8")
    (ws / "beta.txt").write_text("the launch code is LAUNCH-9931\n", encoding="utf-8")
    (ws / "gamma.md").write_text("just notes\n", encoding="utf-8")


# 准备:待精确编辑的配置文件
def _setup_edit_config(ws: Path) -> None:
    (ws / "config.txt").write_text(
        "host=127.0.0.1\nport=7437\nretries=1\nverbose=false\n", encoding="utf-8"
    )


# 准备:三个数值文件
def _setup_multi_sum(ws: Path) -> None:
    (ws / "a.txt").write_text("value: 11\n", encoding="utf-8")
    (ws / "b.txt").write_text("value: 22\n", encoding="utf-8")
    (ws / "c.txt").write_text("value: 33\n", encoding="utf-8")


# 准备:带 bug 的模块与失败测试
def _setup_fix_bug(ws: Path) -> None:
    (ws / "calc.py").write_text(
        "def add(a, b):\n    return a - b  # bug: should be +\n", encoding="utf-8"
    )
    (ws / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )


# 准备:仅子 agent 可读到的秘密文件
def _setup_subagent(ws: Path) -> None:
    (ws / "secret.txt").write_text("the password is KYLE-SECRET-7\n", encoding="utf-8")


# orchestrate-replan 工作区的多级闸门检查器:闸门 N+1 的要求只在闸门 N 通过后
# 才暴露在输出里——朴素计划(一轮全修)无从得知第二道的要求,必须 replan
_CHECK_PY = '''"""Multi-gate checker: gate N+1 requirements are revealed only after gate N."""
import sys
from pathlib import Path


def main() -> int:
    gate1 = Path("gate1.txt")
    if not gate1.exists() or gate1.read_text(encoding="utf-8").strip() != "PASS-1":
        print("GATE-1 FAILED: create gate1.txt containing exactly: PASS-1")
        return 1
    print("GATE-1 passed.")
    gate2 = Path("hidden_required.txt")
    if not gate2.exists() or gate2.read_text(encoding="utf-8").strip() != "OPEN-SESAME":
        print("GATE-2 hint revealed: create hidden_required.txt containing exactly: OPEN-SESAME")
        print("GATE-2 FAILED: hidden_required.txt missing or wrong content")
        return 2
    print("GATE-2 passed.")
    print("ALL GATES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# 准备:orchestrate-replan 的多级闸门工作区(仅 check.py,闸门产物由执行产生)
def _setup_orchestrate_replan(ws: Path) -> None:
    (ws / "check.py").write_text(_CHECK_PY, encoding="utf-8")


# ── verify 函数 ───────────────────────────────────────────────────


# 判定:回答包含 magic number
def _verify_read_report(**kw: Any) -> None:
    mentions(kw["outcome"], "86042")


# 判定:hello.txt 精确内容
def _verify_write_hello(**kw: Any) -> None:
    file_equals(kw["workspace"], "hello.txt", "kyle-e2e")


# 判定:嵌套目录下的 marker 文件
def _verify_bash_marker(**kw: Any) -> None:
    file_contains(kw["workspace"], "out/inner/marker.txt", "done")


# 判定:报告了来源文件名与 launch code
def _verify_locate(**kw: Any) -> None:
    mentions(kw["outcome"], "beta", "LAUNCH-9931")


# 判定:retries 已改且其余行未破坏
def _verify_edit_config(**kw: Any) -> None:
    file_contains(
        kw["workspace"], "config.txt",
        "retries=5", "host=127.0.0.1", "port=7437", "verbose=false",
    )


# 判定:至少一个任务文件处于 completed 状态
def _verify_plan_steps(**kw: Any) -> None:
    files = list(kw["runs_root"].rglob("task_*.json"))
    assert files, "runs 目录下没有任何任务文件"
    done = [p for p in files if '"completed"' in p.read_text(encoding="utf-8")]
    assert done, "没有任何任务文件达到 completed 状态"


# 判定:偏好已沉淀进 notes.md
def _verify_note_memory(**kw: Any) -> None:
    notes = kw["store"].read_notes(kw["session"].id)
    assert "docker" in notes.lower(), f"notes.md 未包含 docker 偏好: {notes!r}"


# 判定:三数之和 66
def _verify_multi_sum(**kw: Any) -> None:
    mentions(kw["outcome"], "66")


# 判定:calc.py 已修复为 a + b
def _verify_fix_bug(**kw: Any) -> None:
    file_contains(kw["workspace"], "calc.py", "a + b")


# 判定:回答包含密码,且事件流出现子 agent 启动
def _verify_subagent(**kw: Any) -> None:
    mentions(kw["outcome"], "KYLE-SECRET-7")
    events_include(kw["events"], "subagent.started")


# 判定:orchestrate-replan——两道闸门产物正确、check.py 真跑通过、
# 且事件流含至少两次规划子代理启动(replan 实证)
def _verify_orchestrate_replan(**kw: Any) -> None:
    file_equals(kw["workspace"], "gate1.txt", "PASS-1")
    file_equals(kw["workspace"], "hidden_required.txt", "OPEN-SESAME")
    # 终态确定性判定:真实重跑 check.py(工具在回放时已重执行,产物应存在)
    proc = subprocess.run(
        [sys.executable, "check.py"],
        cwd=kw["workspace"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, f"check.py 未通过: rc={proc.returncode} {proc.stdout!r}"
    assert "ALL GATES PASSED" in proc.stdout
    # replan 实证:≥2 次 description 含"规划"的 subagent.started
    planning = [
        e for e in kw["events"]
        if e.get("type") == "subagent.started"
        and "规划" in str(e.get("description", ""))
    ]
    assert len(planning) >= 2, (
        f"规划子代理启动仅 {len(planning)} 次,未发生引擎级 replan"
    )


# config_patch:把知识库目录指到任务工作区内(verify 可直接断言落盘文件;
# harness 已先强制隔离到 root/task.id/knowledge,此处为任务级覆盖)
def _patch_knowledge(config: Any, root: Path) -> None:
    config.knowledge.dir = str(root / "workspace" / "knowledge")


# knowledge-save-retrieval 的固定原文(症状/定位/处置各一句;
# NOVA-77 与 KYLE-NOVA 是独特 token,与内建 9 篇 runbook 词面不撞车)
_EXPERIENCE_DOC = (
    "症状:网关 5xx 比例突增,错误日志出现上游返回码 NOVA-77,伴随少量超时。\n"
    "定位:确认上游服务 KYLE-NOVA 的健康端点,比对发布时间线,锁定最近一次配置变更。\n"
    "处置:回滚 KYLE-NOVA 的配置变更并灰度放量,5xx 恢复基线后观察 30 分钟。"
)


# 判定:knowledge-save-retrieval——文档已沉淀到知识库目录、save/search 均真实
# 调用、检索输出命中独特标识、最终回答复述标题标识。
# 确定性论证:setup 空工作区 → agent 用 knowledge_save 写入固定内容文档 →
# 检索语料 = 9 内建 runbook + 1 新文档,keyword 检索确定性;回放时 save 幂等
# 覆写、search 重执行,语料与录制时逐位一致
def _verify_knowledge_save_retrieval(**kw: Any) -> None:
    kb_dir = kw["workspace"] / "knowledge"
    docs = list(kb_dir.glob("*.md"))
    assert docs, "知识库目录下没有任何 .md 文档"
    body = "".join(p.read_text(encoding="utf-8") for p in docs)
    assert "NOVA-77" in body and "KYLE-NOVA" in body, (
        f"知识库文档缺独特标识: {body[:200]!r}"
    )
    finished = [e for e in kw["events"] if e.get("type") == "tool.call_finished"]
    assert any(
        e.get("tool_name") == "knowledge_save" for e in finished
    ), "事件流缺少 knowledge_save 的 tool.call_finished"
    assert any(
        e.get("tool_name") == "knowledge_search"
        and "KYLE-NOVA" in str(e.get("output", ""))
        for e in finished
    ), "knowledge_search 的输出未命中 KYLE-NOVA"
    mentions(kw["outcome"], "KYLE-NOVA")


# ── 任务注册表 ────────────────────────────────────────────────────

TASKS: list[EvalTask] = [
    EvalTask(
        id="read-report-magic", category="read",
        goal="用 read_file 读取 sample.txt,告诉我文档里的 magic number 是多少,只回答数字。",
        setup=_setup_read_report, verify=_verify_read_report,
        note="单工具只读:read_file + 事实抽取",
    ),
    EvalTask(
        id="write-hello", category="write",
        goal="创建文件 hello.txt,内容正好是一行:kyle-e2e",
        setup=lambda ws: None, verify=_verify_write_hello,
        note="单工具写:write_file 精确内容",
    ),
    EvalTask(
        id="bash-marker", category="write",
        goal="用 bash 创建目录 out/inner,并在其中创建 marker.txt,内容为 done",
        setup=lambda ws: None, verify=_verify_bash_marker,
        note="bash 副作用 + 目录创建", max_steps=14,
    ),
    EvalTask(
        id="locate-launch-code", category="read",
        goal="当前目录有三个文件,找出哪个文件包含 launch code,告诉我文件名和完整的 launch code。",
        setup=_setup_locate, verify=_verify_locate,
        note="多文件检索:list_dir/read_file + 信息定位",
    ),
    EvalTask(
        id="edit-retries", category="write",
        goal="把 config.txt 里的 retries=1 改成 retries=5,其他内容保持不变。",
        setup=_setup_edit_config, verify=_verify_edit_config,
        note="读-改-写:精确编辑不破坏其余内容", max_steps=14,
    ),
    EvalTask(
        id="plan-three-steps", category="plan",
        goal="把目标「准备演示」拆成三个任务(写大纲、写正文、检查),用任务工具逐个创建并全部标记完成。",
        setup=lambda ws: None, verify=_verify_plan_steps,
        note="task_* 工具链:自主规划与状态流转", max_steps=10,
    ),
    EvalTask(
        id="note-preference", category="memory",
        goal="请记住这条偏好:本项目部署一律使用 docker。用笔记工具保存它。",
        setup=lambda ws: None, verify=_verify_note_memory,
        note="note_save 长期记忆沉淀",
    ),
    EvalTask(
        id="multi-file-sum", category="complex",
        goal="读取 a.txt、b.txt、c.txt 三个文件里的 value,告诉我它们的总和。",
        setup=_setup_multi_sum, verify=_verify_multi_sum,
        note="多工具聚合:并行候选(亦作并行执行的度量任务)",
    ),
    EvalTask(
        id="fix-pytest-bug", category="complex",
        goal="test_calc.py 里的测试失败了。运行 pytest 找到 bug,修复 calc.py 让测试通过,"
             "并确认最终 pytest 全绿。",
        setup=_setup_fix_bug, verify=_verify_fix_bug,
        note="bash+编辑闭环:测试驱动修复", max_steps=10,
    ),
    EvalTask(
        id="subagent-secret", category="complex",
        goal="用 spawn_agent 派一个子 agent 去读 secret.txt 并告诉你密码,然后你转述给我。",
        setup=_setup_subagent, verify=_verify_subagent,
        note="spawn_agent 派生与结果回传", max_steps=10,
    ),
    EvalTask(
        id="orchestrate-replan", category="complex",
        goal=(
            "工作区的 check.py 是多级闸门检查器(用 python check.py 运行),每道闸门的"
            "要求只会在前一道修好后才暴露。请用 orchestrate_run 编排工具完成它,"
            "goal 参数只传以下任务原文(不要添加其他内容):「让 python check.py 输出 "
            "ALL GATES PASSED。约束:所有操作只在当前目录用相对路径,禁止 cd 与上级"
            "目录;每轮只修复当前已暴露的这一道闸门;发现新闸门要求时不要顺手修复,"
            "把它的要求写入执行汇报交评审,等待重规划后在新一轮处理。」"
        ),
        setup=_setup_orchestrate_replan, verify=_verify_orchestrate_replan,
        note="引擎级 replan:评审未达成→证据回注重规划,须 ≥2 轮规划", max_steps=14,
    ),
    EvalTask(
        id="knowledge-save-retrieval", category="memory",
        goal=(
            "把下面这篇排障经验用 knowledge_save 存入知识库"
            "(title 用「网关5xx排障手册-KYLE-NOVA」,content 一字不改用原文),"
            "然后用 knowledge_search 检索「网关 5xx KYLE-NOVA」确认能命中,"
            "最后告诉我命中文档的标题。原文:\n"
            f"{_EXPERIENCE_DOC}"
        ),
        setup=lambda ws: None, verify=_verify_knowledge_save_retrieval,
        config_patch=_patch_knowledge,
        note="运行时知识库闭环:knowledge_save 沉淀 → knowledge_search 命中",
        max_steps=10,
    ),
]
