"""Durable run(支柱 3b):run 级断点快照与崩溃恢复。

机制:AgentLoop 每步结束把 ExecutionContext 快照为 run 目录下的
run_state.json(原子写);run 结束时写入终态。daemon 重启时扫描
sessions 树中 status=="running" 的快照,重建上下文从断点步续跑。

语义:工具执行是 at-least-once——快照点在"工具结果回填之后",崩溃
发生在工具执行中途时该工具会在恢复后重放;幂等工具(read/echo/
内容覆盖写)天然安全,非幂等 bash 命令可能重复执行(文档明示)。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from kyle_agent.core.context import ExecutionContext

SNAPSHOT_FILENAME = "run_state.json"


@dataclass
class RunSnapshot:
    run_id: str
    session_id: str
    goal: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"
    max_steps: int = 20
    system_prompt_override: str | None = None
    ts: str = ""


# 从 ExecutionContext 构建快照
def snapshot_from_context(
    context: ExecutionContext, *, session_id: str
) -> RunSnapshot:
    from datetime import UTC, datetime

    return RunSnapshot(
        run_id=context.run_id,
        session_id=session_id,
        goal=context.goal,
        messages=context.messages,
        step=context.step,
        status=context.status,
        max_steps=context.max_steps,
        system_prompt_override=context.system_prompt_override,
        ts=datetime.now(UTC).isoformat(),
    )


# 原子写快照(先写临时文件再 os.replace,避免半截 JSON)
def save_snapshot(path: Path, snap: RunSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(snap), ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, path)


# 读取快照;文件损坏返回 None(坏快照不阻塞恢复流程)
def load_snapshot(path: Path) -> RunSnapshot | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunSnapshot(**data)
    except (OSError, ValueError, TypeError):
        return None


# 扫描 runs 根目录,返回 status=="running" 的(路径, 快照)列表——待恢复集合
def scan_running(runs_root: Path) -> list[tuple[Path, RunSnapshot]]:
    pending: list[tuple[Path, RunSnapshot]] = []
    if not runs_root.exists():
        return pending
    for state_path in runs_root.rglob(SNAPSHOT_FILENAME):
        snap = load_snapshot(state_path)
        if snap is not None and snap.status == "running":
            pending.append((state_path, snap))
    return pending


# loop 每步调用的持久化协议(durable run 挂钩)
class RunPersister(Protocol):
    # 把当前 ExecutionContext 快照到 run 目录
    async def persist(self, context: ExecutionContext) -> None: ...


# 绑定快照路径与 session 的标准实现:写 run_state.json
class FileRunPersister:
    # path 为 run 目录下的 run_state.json 路径
    def __init__(self, path: Path, session_id: str) -> None:
        self._path = path
        self._session_id = session_id

    # 从 context 生成快照并原子写入
    async def persist(self, context: ExecutionContext) -> None:
        save_snapshot(
            self._path, snapshot_from_context(context, session_id=self._session_id)
        )
