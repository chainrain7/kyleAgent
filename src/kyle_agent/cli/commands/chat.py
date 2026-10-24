from __future__ import annotations

import asyncio
import sys
from typing import Any

from kyle_agent.core.config import KyleConfig
from kyle_agent.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending_permission_id: str | None = None

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            print(f"[permission] {tool_name}  {param_preview}")
            print("  y=allow once  a=always allow  n=deny once  d=always deny")
            self.pending_permission_id = tool_use_id
        elif t in ("permission.granted", "permission.denied"):
            # 审批已有结果（含超时自动拒绝）时清除挂起状态，避免下一条聊天消息被误读为决策
            self.pending_permission_id = None
        elif t == "run.finished":
            # run 结束（含失败/取消）后不可能再有审批响应，兜底清除挂起状态
            self.pending_permission_id = None
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            self.pending_permission_id = None
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            self.pending_permission_id = None
            print("session closed.")


# 在线程池中读取 stdin，避免阻塞 socket event loop
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


# 异步核心：创建 chat session，循环读取用户输入并发送到 daemon；权限请求时优先处理审批
async def _chat_async(config: KyleConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())

    def _loop_done(task: asyncio.Task[None]) -> None:
        # 事件循环异常退出（如 daemon 中途死亡）时给出提示；正常 cancel 不打印
        if not task.cancelled() and task.exception() is not None:
            print("\n[connection lost]", file=sys.stderr)

    loop_task.add_done_callback(_loop_done)
    # 正在执行的 send_message RPC；后台运行以保持 stdin 可读，否则权限审批会死锁到超时
    send_task: asyncio.Task[dict[str, Any]] | None = None

    # 打印后台 send_message 的失败（如 session busy），成功时无需输出
    def _send_done(task: asyncio.Task[dict[str, Any]]) -> None:
        if not task.cancelled() and task.exception() is not None:
            print(f"error: {task.exception()}", file=sys.stderr)

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": "global",
            },
        )
        created = await client.send_command("session.create", {"mode": "chat"})
        session_id = str(created["session_id"])
        print(f"[session: {session_id}]")

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break
            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending_permission_id:
                decision = _DECISION_MAP.get(content.lower())
                if decision is None:
                    print("  enter y (allow once), a (always allow), "
                          "n (deny once), d (always deny)")
                    continue
                tool_use_id = printer.pending_permission_id
                printer.pending_permission_id = None
                await client.send_command(
                    "permission.respond",
                    {"tool_use_id": tool_use_id, "decision": decision},
                )
                continue

            # 上一条消息的 run 还在执行时拒绝新消息（daemon 侧也会以 session busy 拒绝）
            if send_task is not None and not send_task.done():
                print("  [agent is still working on the previous message]")
                continue

            send_task = asyncio.create_task(
                client.send_command(
                    "session.send_message",
                    {"session_id": session_id, "content": content},
                )
            )
            send_task.add_done_callback(_send_done)

        # 输入通道已关闭：主动拒绝仍未答复的审批，daemon 不必等到审批超时
        if printer.pending_permission_id:
            try:
                await client.send_command(
                    "permission.respond",
                    {
                        "tool_use_id": printer.pending_permission_id,
                        "decision": "deny_once",
                    },
                )
            except IpcError:
                pass
            printer.pending_permission_id = None

        # 等待仍在执行的 run 结束后再关闭 session，避免 session busy 错误
        if send_task is not None and not send_task.done():
            try:
                await send_task
            except IpcError:
                pass  # 已由 _send_done 回调打印，这里不重复
        await client.send_command("session.close", {"session_id": session_id})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    return 0


# 执行 kyle chat 命令
def cmd_chat(config: KyleConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
