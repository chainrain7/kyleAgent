from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kyle_agent.core.bus.envelope import HandlerError
from kyle_agent.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from kyle_agent.core.durable import scan_running
from kyle_agent.core.events.bus import EventBus
from kyle_agent.core.runs import new_run_id
from kyle_agent.core.session.model import Session, SessionMode
from kyle_agent.core.session.store import SessionStore
from kyle_agent.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from kyle_agent.core.llm.base import LLMProvider
    from kyle_agent.core.llm.router import ModelRouter
    from kyle_agent.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线、可选 LLM provider 与模型路由器
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._router = router
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()

    # 创建新 session 并写入 meta.json
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            self._store.append_message(sid, "user", content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40]

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.write_meta(session)

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    # allowed_tools=None 表示不限制；空列表表示不允许任何工具
                    tool_whitelist = skill.allowed_tools
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            await runner.run_and_capture(
                goal,
                run_id=run_id,
                session=session,
                store=self._store,
                system_prompt_override=system_prompt_override,
                tool_whitelist=tool_whitelist,
            )

            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            return run_id

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from kyle_agent.core.bus.commands import SessionCompactResult
            from kyle_agent.core.compact.compactor import Compactor
            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
# 手动 /compact 保持全量替换;增量保留轮数仅自动压缩按配置启用
            compactor = Compactor(self._bus, session_dir, sid, router=self._router)
            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            self._store.write_compacted(sid, [
                {"role": "user", "content": result.summary_text},
                {"role": "assistant", "content": "Understood, I'll continue from this summary."},
            ])
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 恢复 session：显式 id 或取磁盘上最近活跃的 session，重新载入内存索引
    # 场景：TUI 重启/断线重连后接回同一会话，daemon 未重启时运行中任务不受影响
    async def resume(self, sid: str = "") -> Session:
        if sid:
            found: Session = self._store.read_meta(sid)  # 不存在时抛 FileNotFoundError
        else:
            latest = self._latest_active_session()
            if latest is None:
                raise HandlerError(SESSION_NOT_FOUND, "no active session to resume")
            found = latest
        if found.id not in self._sessions:
            self._sessions[found.id] = found
            self._locks.setdefault(found.id, asyncio.Lock())
        await self._bus.publish(
            SessionResumedEvent(session_id=found.id, ts=_now())
        )
        return found

    # 扫描存储目录，返回最近更新的未关闭 session；无候选返回 None
    def _latest_active_session(self) -> Session | None:
        best: Session | None = None
        root = self._store.root
        if not root.exists():
            return None
        import json as _json

        for meta_path in root.glob("*/meta.json"):
            try:
                data = _json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("status") == "closed":
                continue
            candidate = Session.from_dict(data)
            if best is None or candidate.updated_at > best.updated_at:
                best = candidate
        return best

    # daemon 启动时恢复运行中的 run:扫描快照 → 后台任务从断点续跑
    # 工具执行为 at-least-once(崩溃点在工具执行中途会重放该工具)
    async def recover_pending_runs(self) -> int:
        # 扫描所有 session 的 runs 目录,找 status=="running" 的快照
        pending = scan_running(self._store.root)
        count = 0
        for _path, snap in pending:
            session = self._sessions.get(snap.session_id)
            if session is None:
                try:
                    session = self._store.read_meta(snap.session_id)
                except (OSError, ValueError):
                    continue
                if session.status == "closed":
                    continue
                self._sessions[session.id] = session
                self._locks.setdefault(session.id, asyncio.Lock())
            runner = self._runner_factory()
            asyncio.create_task(
                runner.run_and_capture(
                    snap.goal,
                    run_id=snap.run_id,
                    session=session,
                    store=self._store,
                    resume=snap,
                )
            )
            count += 1
        if count:
            import logging

            logging.getLogger(__name__).info(
                "durable: recovered %d pending run(s)", count
            )
        return count

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
