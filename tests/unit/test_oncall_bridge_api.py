from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from kyle_agent.oncall.faults import BY_ID  # noqa: E402
from kyle_agent.oncall.sim import SimEnv  # noqa: E402
from kyle_agent.webui.bridge import app  # noqa: E402


# 构造指向临时 env 的 ASGI 客户端
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:

    monkeypatch.setenv("KYLE_ONCALL_ENV", str(tmp_path / "env"))
    SimEnv.reset(tmp_path / "env")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# 功能:/api/state 返回服务/告警/拓扑/剧本与报告清单
# 设计:注入后告警 firing、拓扑含依赖、20 剧本可下发到前端演练面板
async def test_api_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(tmp_path, monkeypatch) as c:
        BY_ID["gateway-mem-leak"].inject(SimEnv(tmp_path / "env"))
        r = await c.get("/api/state")
        data = r.json()
        assert data["available"] is True
        assert data["services"]["gateway"]["status"] == "degraded"
        assert data["alerts"][0]["status"] == "firing"
        assert data["topology"]["gateway"] == ["orders", "payments"]
        assert len(data["scenarios"]) == 20
        assert data["reports"] == []


# 功能:注入/重置/报告读取全链路
# 设计:POST inject 先重置再注入(重复注入不叠加);reset 后告警清空;
#       报告 404 与路径穿越防护(../ 归约为文件名)
async def test_api_inject_reset_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(tmp_path, monkeypatch) as c:
        r = await c.post("/api/inject", json={"fault_id": "orders-disk-full"})
        assert r.json()["injected"] == "orders-disk-full"

        bad = await c.post("/api/inject", json={"fault_id": "no-such"})
        assert bad.status_code == 404

        # 生成一份报告再读
        reports = tmp_path / "env" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "run-1.md").write_text("# 处置报告 · run run-1", encoding="utf-8")
        rr = await c.get("/api/report/run-1")
        assert "处置报告" in rr.json()["content"]
        missing = await c.get("/api/report/nope")
        assert missing.status_code == 404
        traversal = await c.get("/api/report/..%2Fstate")
        assert traversal.status_code == 404

        await c.post("/api/reset")
        st = (await c.get("/api/state")).json()
        assert st["alerts"] == []
        assert st["services"]["gateway"]["status"] == "running"


# 功能:env 不存在时 available=False,前端降级为普通控制台
async def test_api_state_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KYLE_ONCALL_ENV", str(tmp_path / "nowhere"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        data = (await c.get("/api/state")).json()
        assert data == {"available": False}
