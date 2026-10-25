"""Web UI 桥接器 E2E:浏览器 WebSocket ↔ 桥接器 ↔ daemon 全链路(真实进程)。

无需外网:mock Anthropic 服务提供确定性 LLM 响应。覆盖:
- WS 上直接发 JSON-RPC(core.ping)拿到响应
- event.subscribe → session.create → send_message 的事件实时推送
- permission.requested 事件到达 + 经 WS 回 permission.respond 的审批往返
- run 期间工具执行事件(kyle-e2e 输出)与 run.finished
- 断开 WS 后桥接器资源清理(再次连接可用)
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

REPO_ROOT = Path(__file__).parent.parent.parent
websockets = pytest.importorskip("websockets")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def webui_stack(tmp_path: Path) -> AsyncGenerator[dict[str, object], None]:
    mock_port, core_port, ui_port = _free_port(), _free_port(), _free_port()
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
        "KYLE_LOG_FILE": str(tmp_path / "core.log"),
        "KYLE_LOG_LEVEL": "WARNING",
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    core_proc = subprocess.Popen(
        [sys.executable, "-m", "kyle_agent.core"], env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 桥接器子进程:独立进程验证真实启动路径(kyle-webui 入口)
    ui_env = os.environ.copy()
    ui_env.update({"KYLE_PORT": str(core_port), "NO_PROXY": "*", "no_proxy": "*"})
    ui_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "kyle_agent.webui.bridge:app",
         "--host", "127.0.0.1", "--port", str(ui_port), "--log-level", "error"],
        env=ui_env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # 等 daemon 与桥接器都就绪
        deadline = time.monotonic() + 20.0
        for port in (core_port, ui_port):
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                try:
                    _r, w = await asyncio.open_connection("127.0.0.1", port)
                    w.close()
                    await w.wait_closed()
                    break
                except (ConnectionRefusedError, OSError):
                    if core_proc.poll() is not None or ui_proc.poll() is not None:
                        pytest.fail("daemon or bridge exited early")
            else:
                pytest.fail(f"service on {port} did not start in 20s")
        yield {"ui_port": ui_port, "tmp": tmp_path}
    finally:
        for proc in (ui_proc, core_proc, mock_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# 功能:WS 全链路——ping、订阅、发消息、审批往返、事件实时推送、run 完成
# 设计:单条 WebSocket 复用命令与事件通道(daemon 协议即如此);
#       permission.requested 到达后经同一 WS 回 allow_once,断言工具真实
#       执行输出(kyle-e2e)与 run.finished,验证桥接器双向中继无死锁
async def test_websocket_relay_end_to_end(webui_stack: dict[str, object]) -> None:
    import websockets.asyncio.client as ws_client

    ui_port = int(webui_stack["ui_port"])
    async with ws_client.connect(f"ws://127.0.0.1:{ui_port}/ws",
                                 max_size=64 * 1024 * 1024) as ws:
        events: list[dict] = []
        # 事件收集器:后台任务持续读
        async def reader() -> None:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("kind") == "event":
                    events.append(msg["event"])

        read_task = asyncio.create_task(reader())

        async def rpc(method: str, params: dict, wait_s: float = 30) -> dict:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": method, "method": method,
                                      "params": params}))
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                if msg.get("id") == method:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result") or {}
                if msg.get("kind") == "event":
                    events.append(msg["event"])
            raise TimeoutError(method)

        pong = await rpc("core.ping", {"client": "webui-e2e"})
        assert pong["server_version"]

        await rpc("event.subscribe", {"topics": ["*"], "scope": "global"})
        created = await rpc("session.create", {"mode": "chat"})
        sid = created["session_id"]

        # send_message 同步 RPC 要等 run 结束;审批在 reader 侧异步完成
        async def approver() -> None:
            while True:
                await asyncio.sleep(0.05)
                for e in events:
                    if e.get("type") == "permission.requested" and not e.get("_done"):
                        e["_done"] = True  # type: ignore[index]
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": f"perm-{e['tool_use_id']}",
                            "method": "permission.respond",
                            "params": {"tool_use_id": e["tool_use_id"],
                                       "decision": "allow_once"},
                        }))
                        return

        approve_task = asyncio.create_task(approver())
        result = await rpc("session.send_message",
                           {"session_id": sid, "content": "run echo"}, wait_s=60)
        await approve_task

        types = [e.get("type") for e in events]
        assert "permission.requested" in types
        assert "tool.call_finished" in types
        finished = next(e for e in events if e.get("type") == "run.finished")
        assert finished["status"] == "success"
        tool_ok = next(e for e in events if e.get("type") == "tool.call_finished")
        assert "kyle-e2e" in str(tool_ok.get("output", ""))
        assert result.get("run_id")

        read_task.cancel()
        try:
            await read_task
        except (asyncio.CancelledError, Exception):
            pass

    # 断开后桥接器应回收;可再次连接(资源清理验证)
    async with ws_client.connect(f"ws://127.0.0.1:{ui_port}/ws") as ws2:
        await ws2.send(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "core.ping",
                                   "params": {}}))
        raw = await asyncio.wait_for(ws2.recv(), timeout=10)
        assert json.loads(raw)["result"]["server_version"]
