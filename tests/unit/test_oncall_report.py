from __future__ import annotations

import json
from pathlib import Path

from kyle_agent.oncall.faults import BY_ID
from kyle_agent.oncall.report import generate_report
from kyle_agent.oncall.sim import SimEnv


# 写一份含完整处置轨迹的 events.jsonl
def _write_events(run_dir: Path) -> None:
    events = [
        {"type": "run.started", "run_id": "r1", "goal": "handle alert", "ts": "2026-10-28T10:30:02+00:00"},
        {"type": "tool.call_started", "tool_name": "service_status", "params": {"service": "orders"},
         "tool_use_id": "t0", "ts": "2026-10-28T10:30:05+00:00"},
        {"type": "tool.call_started", "tool_name": "service_restart", "params": {"service": "orders"},
         "tool_use_id": "t1", "ts": "2026-10-28T10:30:31+00:00"},
        {"type": "permission.granted", "tool_use_id": "t1", "decision": "allow_once",
         "ts": "2026-10-28T10:30:31+00:00"},
        {"type": "tool.call_finished", "tool_name": "service_restart", "tool_use_id": "t1",
         "output": "cleared fault(s): orders-config-typo-errors\norders restored",
         "elapsed_ms": 45, "ts": "2026-10-28T10:30:32+00:00"},
        {"type": "tool.call_started", "tool_name": "submit_diagnosis",
         "params": {"root_cause": "发布引入坏配置 retr_interval", "confidence": 0.9,
                    "evidence": "10:25 ERROR unknown config key retr_interval\nerr% 突变点 10:25"},
         "tool_use_id": "t2", "ts": "2026-10-28T10:30:35+00:00"},
        {"type": "run.finished", "run_id": "r1", "status": "success", "steps": 3,
         "ts": "2026-10-28T10:30:36+00:00"},
    ]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )


# 功能:含 submit_diagnosis 的 run 生成报告,含结论/处置/审批/时间线/证据
# 设计:注入 S12 后先执行 restart(终态 resolved),再生成——断言五个
#       章节的关键内容与审批决策字符串;报告文件落在 env/reports/
async def test_generate_report_full_content(tmp_path: Path) -> None:
    from kyle_agent.oncall.tools import build_oncall_tools

    env_dir = tmp_path / "env"
    env = SimEnv.reset(env_dir)
    BY_ID["orders-config-typo-errors"].inject(env)
    tools = {t.name: t for t in build_oncall_tools(env_dir)}
    await tools["service_restart"].invoke({"service": "orders"})

    run_dir = tmp_path / "runs" / "r1"
    _write_events(run_dir)
    path = generate_report(run_dir, env, "r1")

    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "处置报告 · run r1" in text
    assert "坏配置 retr_interval" in text and "0.9" in text
    assert "service_restart orders(allow_once)" in text
    assert "已解决" in text
    assert "## 时间线" in text and "审批:allow_once" in text
    assert "## 证据链" in text and "unknown config key" in text


# 功能:无 submit_diagnosis 的普通 run 返回 None 不落文件
# 设计:events 只有 run.started/finished——非 oncall 会话静默跳过
def test_generate_report_skips_plain_run(tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    run_dir = tmp_path / "runs" / "r2"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "run.started", "run_id": "r2", "ts": "t"}) + "\n"
        + json.dumps({"type": "run.finished", "run_id": "r2", "ts": "t"}) + "\n",
        encoding="utf-8",
    )
    assert generate_report(run_dir, env, "r2") is None
    assert not (tmp_path / "env" / "reports").exists()


# 功能:诊断类 run(零变更)报告标注"未执行变更"
# 设计:submit_diagnosis 有、变更无——报告明确写出零变更与人工跟进
def test_generate_report_diagnosis_only(tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    BY_ID["payments-cert-expired"].inject(env)
    run_dir = tmp_path / "runs" / "r3"
    run_dir.mkdir(parents=True)
    events = [
        {"type": "run.started", "run_id": "r3", "ts": "2026-10-28T11:00:00+00:00"},
        {"type": "tool.call_started", "tool_name": "submit_diagnosis",
         "params": {"root_cause": "证书过期", "confidence": 0.95},
         "tool_use_id": "t9", "ts": "2026-10-28T11:00:20+00:00"},
    ]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
    )
    path = generate_report(run_dir, env, "r3")
    text = path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "未执行变更" in text
    assert "证书过期" in text
