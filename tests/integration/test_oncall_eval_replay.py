"""OnCallCopilot 评测的离线回归:全部剧本 cassette 回放(Phase 3 CI 门禁)。

与 test_eval_replay 同构:无网络零 Key,LLM 响应来自
evals/cassettes/oncall-*.json,工具在隔离模拟环境真实重执行。
三重断言(定位/动作集/终态)在回放下确定性成立——任何影响
值班行为的改动(工具语义/恢复规则/prompt)破坏通过率,这里直接红。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from evals.cassette import CassetteProvider, load_cassette
from evals.harness import run_task
from evals.oncall_tasks import ONCALL_TASKS

REPO_ROOT = Path(__file__).parent.parent.parent
CASSETTE_DIR = REPO_ROOT / "evals" / "cassettes"


# 功能:每个有 cassette 的 oncall 剧本离线回放三重断言全过
# 设计:参数化 20 剧本;回放确定性由纯文件 env + 顺序匹配 cassette 保证
@pytest.mark.parametrize("task", ONCALL_TASKS, ids=[t.id for t in ONCALL_TASKS])
async def test_oncall_eval_replay(task, tmp_path: Path) -> None:
    cassette = CASSETTE_DIR / f"{task.id}.json"
    if not cassette.exists():
        pytest.skip(f"cassette not recorded: {cassette.name}")
    result = await run_task(task, CassetteProvider(load_cassette(cassette)), tmp_path)
    assert result.passed, f"{task.id} 回放失败: {result.error}"


# 功能:cassette 覆盖率——全部 oncall 剧本必须有录制
# 设计:单独断言(而非 skip),新增剧本忘录制时 CI 立即红
async def test_all_oncall_tasks_have_cassettes() -> None:
    missing = [
        t.id for t in ONCALL_TASKS
        if not (CASSETTE_DIR / f"{t.id}.json").exists()
    ]
    assert not missing, f"缺少 cassette 的 oncall 任务: {missing}"
