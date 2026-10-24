"""评测 harness:在隔离工作区内执行单个评测任务并判定结果(支柱 1)。

每个任务获得独立的 workspace / sessions / runs 目录与独立的 session;
权限对全部内置工具预授(评测无人工交互);任务的 verify 以
AssertionError 表达失败。指标统计复用 scripts/run_metrics.compute_metrics,
保证评测报告与 CLI 报告同源。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kyle_agent.core.config import KyleConfig
from kyle_agent.core.permissions.manager import PermissionManager
from kyle_agent.core.runner import AgentRunner
from kyle_agent.core.session.model import Session
from kyle_agent.core.session.store import SessionStore

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from scripts.run_metrics import compute_metrics  # noqa: E402

# 评测预授的工具全集(与 runner._build_registry 的注册清单保持一致)
ALL_TOOL_NAMES: tuple[str, ...] = (
    "read_file", "bash", "write_file", "list_dir", "web_search",
    "note_save", "task_create", "task_update", "task_list", "task_get",
    "spawn_agent", "agent_result",
)


@dataclass
class EvalTask:
    # 任务唯一 id(同时是 cassette 文件名)
    id: str
    # 中文或英文的 agent goal(与真实使用方式一致)
    goal: str
    # 任务分类(read/write/plan/memory/complex)
    category: str
    # 准备工作区初始状态
    setup: Callable[[Path], None]
    # 判定终态;失败直接 raise AssertionError
    verify: Callable[..., None]
    max_steps: int = 8
    # 备注:该任务考察点(报告展示用)
    note: str = ""


@dataclass
class TaskResult:
    task_id: str
    passed: bool
    run_status: str
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


# 在隔离目录中执行一个评测任务
async def run_task(
    task: EvalTask,
    provider: object,
    root: Path,
) -> TaskResult:
    workspace = root / task.id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    sessions_root = root / task.id / "sessions"
    task.setup(workspace)

    store = SessionStore(sessions_root)
    session = Session(
        id=f"sess-eval-{task.id}",
        mode="chat",
        status="active",
        title=task.id,
        created_at="",
        updated_at="",
    )
    store.write_meta(session)

    permission_manager = PermissionManager()
    permission_manager.preapprove(session.id, ALL_TOOL_NAMES)

    config = KyleConfig()
    config.agent.max_steps = task.max_steps
    config.compaction.auto_threshold = 0.0

    collected: list[BaseModel] = []

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    old_cwd = Path.cwd()
    os.chdir(workspace)
    try:
        runner = AgentRunner(
            config,
            provider=provider,  # type: ignore[arg-type]
            permission_manager=permission_manager,
            extra_handlers=[_collect],
            runs_dir=root / task.id / "runs",
        )
        outcome = await runner.run_and_capture(
            task.goal, session=session, store=store
        )
    finally:
        os.chdir(old_cwd)

    events = [_model_dump(e) for e in collected]
    metrics = compute_metrics(events)

    passed = outcome.status == "success"
    error = ""
    if not passed:
        error = f"run status={outcome.status} reason={outcome.reason}"
    if passed:
        try:
            task.verify(
                workspace=workspace,
                outcome=outcome,
                events=events,
                runs_root=store.runs_dir(session.id),
                store=store,
                session=session,
            )
        except AssertionError as exc:
            passed = False
            error = f"verify failed: {exc}"
    return TaskResult(
        task_id=task.id,
        passed=passed,
        run_status=outcome.status,
        error=error,
        metrics=metrics,
    )


# 把事件模型转为 dict(metrics 模块的输入形态)
def _model_dump(event: BaseModel) -> dict[str, Any]:
    return event.model_dump()


# 汇总多个任务结果为整体指标(通过率/LLM 调用/token 合计)
def summarize(results: list[TaskResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.passed)

    def _llm_sum(key: str) -> int:
        return sum(int((r.metrics.get("llm") or {}).get(key, 0) or 0) for r in results)

    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "llm_calls": _llm_sum("calls"),
        "input_tokens": _llm_sum("input_tokens"),
        "output_tokens": _llm_sum("output_tokens"),
    }
