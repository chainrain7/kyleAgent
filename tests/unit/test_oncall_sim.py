from __future__ import annotations

from pathlib import Path

from kyle_agent.oncall.sim import SimEnv


# 功能:reset 从包内模板重建环境并物化时间戳
# 设计:重建后三服务/三外部拓扑齐全;csv 首列不再是 T-i 占位而是 HH:MM 真值;
#       日志 ts 同样物化——物化是回放确定性与真实感的共同前提
def test_reset_materializes_timestamps(tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    topo = env.topology()
    assert set(topo.services) == {"gateway", "orders", "payments"}
    assert topo.externals == ["orders-db", "payments-db", "risk-svc"]
    assert topo.dependents_of("payments") == ["gateway"]
    assert topo.dependents_of("orders") == ["gateway"]

    rows = env.metrics_tail("gateway", 15)
    assert len(rows) == 15
    assert rows[-1]["qps"] > 0
    lines = env.log_tail("gateway", 10)
    assert len(lines) == 6
    assert "T-" not in lines[0]  # 占位已被物化


# 功能:state 读写与 patch_service 的故障标记
# 设计:patch 只改给定字段并追加 fault_id;未给字段不丢(state 整体保留)
def test_patch_service_adds_fault_marker(tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    env.patch_service("payments", fault_id="f1", status="degraded", error_rate=0.31)
    state = env.read_state()
    assert state["payments"]["status"] == "degraded"
    assert state["payments"]["error_rate"] == 0.31
    assert state["payments"]["faults"] == ["f1"]
    assert state["payments"]["replicas"] == 3  # 未动字段保留
    # 重复 patch 同一 fault 不重复入列
    env.patch_service("payments", fault_id="f1", p99_ms=800)
    assert env.read_state()["payments"]["faults"] == ["f1"]


# 功能:指标追加继承最近一行,告警编号自增
# 设计:append_metrics 缺省字段继承(剧本只写变化列);append_alert 依次
#       alert-001/002——注入与评测都依赖这两个约定
def test_append_metrics_and_alerts(tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    env.append_metrics("payments", [{"error_rate": 0.31}, {"error_rate": 0.34}])
    tail = env.metrics_tail("payments", 3)
    assert tail[-1]["error_rate"] == 0.34
    assert tail[-1]["qps"] == tail[0]["qps"]  # 未写列继承基线

    a1 = env.append_alert("payments", "P1", "err 31%")
    a2 = env.append_alert("gateway", "P2", "cpu 97%")
    assert (a1, a2) == ("alert-001", "alert-002")
    alerts = env.read_alerts()
    assert alerts[0]["status"] == "firing" and alerts[1]["service"] == "gateway"


# 功能:append_log 追加结构化 JSON 行
# 设计:故障特征行经此入口写入,评测断言在 log_tail 中检索关键词
def test_append_log_json_line(tmp_path: Path) -> None:
    import json

    env = SimEnv.reset(tmp_path / "env")
    env.append_log("orders", "ERROR", 'unknown config key "retr_interval"')
    last = json.loads(env.log_tail("orders", 1)[0])
    assert last["level"] == "ERROR"
    assert "retr_interval" in last["msg"]
    assert ":" in last["ts"]  # 已物化钟点


# 功能:重复 reset 幂等——脏环境重建后回到基线
# 设计:注入过的环境再 reset,状态/告警/指标行数全部回到基线值
def test_reset_idempotent_after_pollution(tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    env.patch_service("gateway", fault_id="x", status="degraded")
    env.append_alert("gateway", "P1", "polluted")
    env2 = SimEnv.reset(tmp_path / "env")
    assert env2.read_state()["gateway"]["status"] == "running"
    assert env2.read_alerts() == []
    assert len(env2.metrics_tail("gateway", 20)) == 15
