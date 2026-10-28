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


# 静态资源(style.css / app.js)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


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
        transient = (WebSocketDisconnect, ConnectionResetError, OSError)
        if exc is not None and not isinstance(exc, transient):
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


# ── OnCallCopilot 演练控制 API(操作对象是文件系统模拟环境,不经 daemon) ──

from fastapi import HTTPException
from pydantic import BaseModel


# 注入请求体
class InjectRequest(BaseModel):
    fault_id: str


# 解析 oncall env 目录(KYLE_ONCALL_ENV > 默认)
def _oncall_env_dir() -> Path:
    env = os.environ.get("KYLE_ONCALL_ENV")
    return Path(env).expanduser() if env else Path("~/.kyle/oncall/env").expanduser()


# 值班工作台状态:服务健康 + 告警队列 + 拓扑 + 报告清单 + 剧本列表
@app.get("/api/state")
async def api_state() -> dict[str, object]:
    from kyle_agent.oncall.faults import SCENARIOS
    from kyle_agent.oncall.sim import SimEnv

    env_dir = _oncall_env_dir()
    if not (env_dir / "state.json").exists():
        return {"available": False}
    env = SimEnv(env_dir)
    reports = sorted((env_dir / "reports").glob("*.md")) if (env_dir / "reports").exists() else []
    return {
        "available": True,
        "services": env.read_state(),
        "alerts": env.read_alerts(),
        "topology": {
            name: info.get("depends_on", [])
            for name, info in env.topology().services.items()
        },
        "externals": env.topology().externals,
        "reports": [r.name for r in reports[-10:]],
        "scenarios": [
            {"id": s.id, "severity": s.severity, "summary": s.alert_summary,
             "service": s.alert_service, "recoverable": s.auto_recoverable}
            for s in SCENARIOS
        ],
    }


# 注入剧本(先重置保可复现),返回新告警 id
@app.post("/api/inject")
async def api_inject(req: InjectRequest) -> dict[str, object]:
    from kyle_agent.oncall.faults import BY_ID
    from kyle_agent.oncall.sim import SimEnv

    scenario = BY_ID.get(req.fault_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"unknown fault_id: {req.fault_id}")
    env = SimEnv.reset(_oncall_env_dir())
    scenario.inject(env)
    alert = env.read_alerts()[-1]
    return {"injected": scenario.id, "alert_id": alert["alert_id"],
            "summary": scenario.alert_summary, "service": scenario.alert_service,
            "severity": scenario.severity}


# 重置环境到基线
@app.post("/api/reset")
async def api_reset() -> dict[str, object]:
    from kyle_agent.oncall.sim import SimEnv

    SimEnv.reset(_oncall_env_dir())
    return {"reset": True}


# 读取处置报告原文
@app.get("/api/report/{name}")
async def api_report(name: str) -> dict[str, object]:
    safe = Path(name).name  # 防路径穿越
    if not safe.endswith(".md"):
        safe = f"{safe}.md"
    path = _oncall_env_dir() / "reports" / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="report not found")
    return {"name": path.name, "content": path.read_text(encoding="utf-8")}


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
