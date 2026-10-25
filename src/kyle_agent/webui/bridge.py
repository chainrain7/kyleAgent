"""Web UI 桥接器(WS↔TCP)。

浏览器无法直连 kyle-core 的 TCP NDJSON 协议;本模块在两者之间做纯中继:

- 静态资源:serve webui/static/(单页前端,无构建步骤)
- WebSocket `/ws`:每个浏览器连接对应一条到 daemon 的 TCP 连接,
  浏览器发来的每条文本消息 = 一条 JSON-RPC 命令(补 \\n 转发);
  daemon 推回的响应/事件信封原样转发给浏览器。
  桥接层不解析、不改写协议——协议契约仍由 bus/ 模型单源生成。

启动:uv run --extra webui kyle-webui(默认 127.0.0.1:8080;
daemon 地址取 KYLE_HOST/KYLE_PORT 或 --core-* 参数)。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_LINE_BYTES = 64 * 1024 * 1024  # 与 socket_server/socket_client 一致

app = FastAPI(title="kyleAgent Web UI", docs_url=None, redoc_url=None)


# 返回 daemon 地址(环境变量优先,回落默认)
def _core_address() -> tuple[str, int]:
    host = os.environ.get("KYLE_HOST", "127.0.0.1")
    port = int(os.environ.get("KYLE_PORT", "7437"))
    return host, port


# 首页(单页前端入口)
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# WebSocket ↔ daemon TCP 双向中继;任一侧断开即收尾另一侧
@app.websocket("/ws")
async def ws_relay(ws: WebSocket) -> None:
    await ws.accept()
    host, port = _core_address()
    try:
        reader, writer = await asyncio.open_connection(host, port, limit=_MAX_LINE_BYTES)
    except (ConnectionRefusedError, OSError):
        # daemon 不在线:告知前端后关闭,前端进入重连循环
        await ws.close(code=1013, reason=f"core not reachable at {host}:{port}")
        return

    async def daemon_to_ws() -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            await ws.send_text(line.decode("utf-8", errors="replace").rstrip("\n"))

    async def ws_to_daemon() -> None:
        while True:
            message = await ws.receive_text()
            writer.write(message.encode("utf-8") + b"\n")
            await writer.drain()

    d2w = asyncio.create_task(daemon_to_ws())
    w2d = asyncio.create_task(ws_to_daemon())
    done, pending = await asyncio.wait(
        {d2w, w2d}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc is not None and not isinstance(exc, (WebSocketDisconnect, ConnectionResetError, OSError)):
            logger.warning("ws relay ended with error: %r", exc)
    for task in pending:
        try:
            await task
        except (WebSocketDisconnect, asyncio.CancelledError, ConnectionResetError, OSError):
            pass
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionResetError, OSError):
        pass
    # 对侧未主动断开时补一刀(1011:上游故障)
    try:
        await ws.close(code=1011)
    except Exception:
        pass


# CLI 入口:uvicorn 起服务
def main() -> None:
    parser = argparse.ArgumentParser(prog="kyle-webui", description="kyleAgent Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="桥接器监听地址")
    parser.add_argument("--port", type=int, default=8080, help="桥接器监听端口")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logger.info(
        "kyle-webui on http://%s:%d (core: %s:%s)",
        args.host, args.port,
        os.environ.get("KYLE_HOST", "127.0.0.1"), os.environ.get("KYLE_PORT", "7437"),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
