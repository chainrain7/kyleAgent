"""全链路离线 E2E：真实 daemon + 本地 mock LLM + 真实 IPC。

不访问外网：scripts/mock_anthropic_server.py 提供确定性的 Anthropic 兼容
SSE 流。覆盖此前只有真实 API 才能测到的路径：流式解析（含 thinking block）、
bash 工具执行、permission.requested → permission.respond 的跨进程审批往返、
session 状态机、compact、事件回放与多连接广播、协议错误路径。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from kyle_agent.core.transport.socket_client import IpcError, SocketClient

REPO_ROOT = Path(__file__).parent.parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def mock_stack(tmp_path: Path) -> AsyncGenerator[dict[str, object], None]:
    # 启动 mock LLM 与 daemon（~ 指向 tmp，隔离用户真实 ~/.kyle）
    mock_port = _free_port()
    core_port = _free_port()
    mock_proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "scripts" / "mock_anthropic_server.py"),
         str(mock_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    env = os.environ.copy()
    env.update({
        "KYLE_PORT": str(core_port),
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{mock_port}",
        "ANTHROPIC_API_KEY": "sk-mock-offline",
        "USERPROFILE": str(tmp_path),
        "HOME": str(tmp_path),
        "KYLE_TRACE_FILE": str(tmp_path / "trace.jsonl"),
        "KYLE_LOG_FILE": str(tmp_path / "core.log"),
        "KYLE_LOG_LEVEL": "INFO",
        # httpx 会读取 Windows 注册表系统代理（如 Clash）并把 localhost 请求也
        # 转发过去导致 mock 不可达；NO_PROXY=* 强制全部直连
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    core_proc = subprocess.Popen(
        [sys.executable, "-m", "kyle_agent.core"], env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            try:
                _r, w = await asyncio.open_connection("127.0.0.1", core_port)
                w.close()
                await w.wait_closed()
                break
            except (ConnectionRefusedError, OSError):
                if core_proc.poll() is not None:
                    pytest.fail(f"daemon exited early: {core_proc.returncode}")
        else:
            pytest.fail("daemon did not start in 20s")
        yield {"core_port": core_port, "tmp": tmp_path, "env": env}
    finally:
        core_proc.terminate()
        try:
            core_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            core_proc.kill()
        mock_proc.terminate()


def _client(port: int) -> SocketClient:
    return SocketClient("127.0.0.1", port)


# 功能：完整 happy path——LLM 请求 bash → IPC 审批 allow_once → 工具真实执行 → end_turn → compact → 回放
# 设计：mock LLM 按对话形状返回确定性响应；审批在事件回调内用 create_task 异步回复
#       （不能内联 await，否则读循环被 dispatch 阻塞造成自死锁）；断言覆盖事件序列、
#       thread 历史、compact 重写、events.jsonl 与 trace 落盘
async def test_full_stack_permission_allow_and_compact(mock_stack: dict[str, object]) -> None:
    port = int(mock_stack["core_port"])
    tmp = Path(str(mock_stack["tmp"]))

    client = _client(port)
    await client.connect()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)
        if event.get("type") == "permission.requested":
            # 必须异步回复：读循环正在 await 本回调，内联 send_command 会死锁
            asyncio.create_task(client.send_command("permission.respond", {
                "tool_use_id": event["tool_use_id"], "decision": "allow_once",
            }))

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())
    await client.send_command("event.subscribe", {
        "topics": ["*"], "scope": "global",
    })
    pong = await client.send_command("core.ping", {"client": "e2e"})
    assert pong["server_version"]

    created = await client.send_command("session.create", {"mode": "chat", "title": "e2e"})
    sid = created["session_id"]

    t0 = time.monotonic()
    send_result = await client.send_command(
        "session.send_message", {"session_id": sid, "content": "run echo"}
    )
    run_id = str(send_result["run_id"])
    assert time.monotonic() - t0 < 60

    types = [e["type"] for e in events]
    assert "permission.requested" in types
    assert "tool.call_started" in types
    assert "run.finished" in types
    finished = next(e for e in events if e["type"] == "run.finished")
    assert finished["status"] == "success", finished
    # bash 真实执行（Windows cmd 的 echo 同样输出该串）
    tool_ok = next(e for e in events if e["type"] == "tool.call_finished")
    assert "kyle-e2e" in str(tool_ok.get("output", ""))
    # thinking block 被保留进 assistant 消息
    history = await client.send_command("session.get_history", {"session_id": sid})
    roles = [m["role"] for m in history["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    first_assistant_blocks = [b["type"] for b in history["messages"][1]["content"]]
    assert "thinking" in first_assistant_blocks and "tool_use" in first_assistant_blocks
    tool_result_block = history["messages"][2]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert "kyle-e2e" in str(tool_result_block["content"])

    # 手动 compact：mock 返回固定摘要，thread 被整体替换
    compact = await client.send_command("session.compact", {"session_id": sid})
    assert compact["summary_tokens"] > 0
    history2 = await client.send_command("session.get_history", {"session_id": sid})
    assert history2["messages"][0]["content"] == (
        "MOCK COMPACT SUMMARY: goal done, files verified."
    )
    assert len(history2["messages"]) == 2

    # events.jsonl 与 trace 落盘且为合法 JSONL
    events_file = tmp / ".kyle" / "sessions" / sid / "runs" / run_id / "events.jsonl"
    lines = [ln for ln in events_file.read_text(encoding="utf-8").splitlines() if ln]
    assert json.loads(lines[0])["type"] == "run.started"
    trace_file = tmp / "trace.jsonl"
    trace_lines = [ln for ln in trace_file.read_text(encoding="utf-8").splitlines() if ln]
    assert any(json.loads(ln)["layer"] == "llm" for ln in trace_lines)

    await client.send_command("session.close", {"session_id": sid})
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    await client.close()


# 功能：审批拒绝路径——deny_once 后工具不执行，错误结果回填，run 仍正常结束
# 设计：与 happy path 同构，审批回调回 deny_once；断言 tool.call_failed 的
#       error_class=permission_denied 且事件流无 tool.call_finished
async def test_full_stack_permission_deny(mock_stack: dict[str, object]) -> None:
    port = int(mock_stack["core_port"])
    client = _client(port)
    await client.connect()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)
        if event.get("type") == "permission.requested":
            asyncio.create_task(client.send_command("permission.respond", {
                "tool_use_id": event["tool_use_id"], "decision": "deny_once",
            }))

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())
    await client.send_command("event.subscribe", {"topics": ["*"], "scope": "global"})
    created = await client.send_command("session.create", {"mode": "chat"})
    sid = created["session_id"]

    await client.send_command(
        "session.send_message", {"session_id": sid, "content": "run echo"}
    )

    types = [e["type"] for e in events]
    failed = [e for e in events if e["type"] == "tool.call_failed"]
    assert failed and failed[0]["error_class"] == "permission_denied"
    assert "tool.call_finished" not in types
    finished = next(e for e in events if e["type"] == "run.finished")
    assert finished["status"] == "success"

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    await client.close()


# 功能：事件回放与多连接广播——第二个客户端既能收到历史回放也能收到实时事件
# 设计：连接 A 跑一个 run；连接 B 以 replay_from_run 订阅断言 replayed_count>0；
#       随后 A 再跑一个 run，断言 B 收到新事件（广播生效）
async def test_replay_and_broadcast(mock_stack: dict[str, object]) -> None:
    port = int(mock_stack["core_port"])

    a = _client(port)
    await a.connect()
    # 读循环必须先于任何 send_command 启动，否则响应无人消费、future 永不 resolve
    loop_a = asyncio.create_task(a.run_event_loop())
    created = await a.send_command("session.create", {"mode": "chat"})
    sid = created["session_id"]

    async def approve(event: dict) -> None:
        if event.get("type") == "permission.requested":
            asyncio.create_task(a.send_command("permission.respond", {
                "tool_use_id": event["tool_use_id"], "decision": "allow_once",
            }))

    a.on_event(approve)
    await a.send_command("event.subscribe", {"topics": ["*"], "scope": "global"})
    run1 = await a.send_command(
        "session.send_message", {"session_id": sid, "content": "run echo"}
    )
    run_id = str(run1["run_id"])

    b = _client(port)
    await b.connect()
    b_events: list[dict] = []

    async def collect(event: dict) -> None:
        b_events.append(event)

    b.on_event(collect)
    loop_b = asyncio.create_task(b.run_event_loop())
    sub = await b.send_command("event.subscribe", {
        "topics": ["*"], "scope": "global", "replay_from_run": run_id,
    })
    assert sub["replayed_count"] > 0
    assert any(e["type"] == "run.started" for e in b_events)

    # A 再触发一次 run，B 应实时收到广播
    before = len(b_events)
    await a.send_command(
        "session.send_message", {"session_id": sid, "content": "run echo again"}
    )
    assert len(b_events) > before

    for task, c in ((loop_a, a), (loop_b, b)):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await c.close()


# 功能：协议错误路径——坏 JSON、未知方法、非法参数、不存在的 session 均返回结构化错误
# 设计：用原始 socket 直发非法帧，逐一断言 JSON-RPC 错误码；
#       permission.respond 未知 id 不崩溃（返回正常结果，仅记日志）
async def test_protocol_error_paths(mock_stack: dict[str, object]) -> None:
    port = int(mock_stack["core_port"])

    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def roundtrip(raw: bytes) -> dict:
        writer.write(raw + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        return json.loads(line)

    # 坏 JSON → PARSE_ERROR (-32700)
    resp = await roundtrip(b"{not json")
    assert resp["error"]["code"] == -32700
    # 未知方法 → METHOD_NOT_FOUND (-32601)
    resp = await roundtrip(json.dumps(
        {"jsonrpc": "2.0", "id": "2", "method": "no.such.method", "params": {}}
    ).encode())
    assert resp["error"]["code"] == -32601
    # 非法参数（mode 必须是字符串）→ INVALID_REQUEST (-32602)
    resp = await roundtrip(json.dumps(
        {"jsonrpc": "2.0", "id": "3", "method": "session.create",
         "params": {"mode": 123}}
    ).encode())
    assert resp["error"]["code"] == -32602
    # 不存在的 session → SESSION_NOT_FOUND (-32010)
    client = _client(port)
    await client.connect()
    # send_command 的响应由读循环消费，必须先启动否则 future 永不 resolve
    loop_task = asyncio.create_task(client.run_event_loop())
    with pytest.raises(IpcError) as exc:
        await client.send_command(
            "session.send_message", {"session_id": "sess-missing", "content": "x"}
        )
    assert exc.value.code == -32010
    # permission.respond 未知 id：不崩溃，返回 ok 结果（仅记 warning 日志）
    resp = await client.send_command("permission.respond", {
        "tool_use_id": "bogus", "decision": "allow_once",
    })
    assert resp.get("ok") is True
    writer.close()
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    await client.close()
