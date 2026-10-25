"""评测体系的离线回归:全部任务 cassette 回放必须全绿(支柱 1 的 CI 门禁)。

无网络、无 API Key;工具在临时工作区真实重执行,LLM 响应来自
evals/cassettes/ 的录制。该测试即"策略变更 → 评测自动回归"闭环的
CI 端:任何影响 agent 行为的改动若破坏任务通过率,这里直接红。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from evals.cassette import CassetteProvider, load_cassette
from evals.harness import run_task
from evals.tasks import TASKS

REPO_ROOT = Path(__file__).parent.parent.parent
CASSETTE_DIR = REPO_ROOT / "evals" / "cassettes"


# 功能:每个有 cassette 的任务离线回放全部通过
# 设计:参数化覆盖全部任务;判定交由任务自身 verify(终态断言),
#       这里只要求 passed——行为回归会让具体任务红并显示失败原因
@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_eval_task_replay_passes(task, tmp_path: Path) -> None:
    cassette = CASSETTE_DIR / f"{task.id}.json"
    if not cassette.exists():
        pytest.skip(f"cassette not recorded: {cassette.name}")
    result = await run_task(task, CassetteProvider(load_cassette(cassette)), tmp_path)
    assert result.passed, f"{task.id} 回放失败: {result.error}"


# 功能:cassette 覆盖率——所有任务都必须有录制,防止任务集悄悄失效
# 设计:单独一条断言(而非 skip),新加任务忘录制时 CI 立即红
async def test_all_tasks_have_cassettes() -> None:
    missing = [t.id for t in TASKS if not (CASSETTE_DIR / f"{t.id}.json").exists()]
    assert not missing, f"缺少 cassette 的任务: {missing}(用 run_evals.py --record 补录)"
