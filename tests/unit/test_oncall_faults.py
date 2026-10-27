from __future__ import annotations

from pathlib import Path

import pytest

from kyle_agent.oncall.faults import BY_ID, SCENARIOS, validate_registry
from kyle_agent.oncall.sim import SimEnv


# 功能:剧本注册表自身的一致性(id 唯一/动作合法/诊断类零动作/关键词非空)
# 设计:元一致性先于行为——任何剧本定义错误在此即红,不必等注入测试
def test_registry_consistency() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == 20 and len(set(ids)) == 20
    assert validate_registry() == []


# 功能:每个剧本注入后环境呈现故障形态
# 设计:参数化覆盖 20 例;统一断言契约——目标服务带 fault 标记、
# 告警队列新增一条 firing、期望动作数与类别分布正确(修复类必含
# 特征日志可检索);这四点是评测三重断言的环境侧前提
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_inject_creates_fault_shape(scenario, tmp_path: Path) -> None:
    env = SimEnv.reset(tmp_path / "env")
    scenario.inject(env)

    state = env.read_state()
    marked = [s for s, v in state.items() if scenario.id in v.get("faults", [])]
    assert marked, "没有任何服务带上该剧本的 fault 标记"

    alerts = env.read_alerts()
    firing = [a for a in alerts if a["status"] == "firing"]
    assert firing and firing[-1]["summary"] == scenario.alert_summary

    if scenario.auto_recoverable:
        assert scenario.expected_actions, "修复类剧本必须有期望动作"
    # 特征可检索性:root_cause_keywords 至少一个出现在日志或状态文本里
    log_text = "\n".join("\n".join(env.log_tail(svc, 30)) for svc in state)
    assert any(
        kw.lower() in (log_text + str(state)).lower()
        for kw in scenario.root_cause_keywords
    ), "剧本特征未在日志/状态中留下可检索线索"


# 功能:诊断类剧本(6 例)注入后不改变可恢复状态、期望动作为空
# 设计:零变更反例是安全叙事的一半——注册表层面锁死"不该动手时不动手"
def test_diagnosis_only_scenarios() -> None:
    diag = [s for s in SCENARIOS if not s.auto_recoverable]
    assert len(diag) == 6
    assert all(s.expected_actions == [] for s in diag)


# 功能:CLI --list 列出 20 条;未知 id 报错退出 1
# 设计:冒烟 CLI 主路径与错误路径,注入成功路径由下方集成验证
def test_inject_cli_list_and_unknown(tmp_path: Path, capsys) -> None:
    from kyle_agent.oncall.inject import main

    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "gateway-mem-leak" in out and "blackfriday-surge" in out

    assert main(["no-such-fault", "--env-dir", str(tmp_path / "e")]) == 1


# 功能:CLI 注入主路径——默认先 reset,产出告警与环境摘要
# 设计:直接调 main(避免子进程开销),断言输出含 alert id 与剧本 id
def test_inject_cli_main_path(tmp_path: Path, capsys) -> None:
    from kyle_agent.oncall.inject import main

    env_dir = tmp_path / "env"
    code = main(["gateway-mem-leak", "--env-dir", str(env_dir)])
    assert code == 0
    out = capsys.readouterr().out
    assert "gateway-mem-leak" in out and "alert-001" in out
    env = SimEnv(env_dir)
    assert env.read_state()["gateway"]["memory_mb"] == 940
    assert env.read_alerts()[0]["status"] == "firing"


# 功能:--keep 跳过重置,允许叠加故障(复合演练)
# 设计:先注入 S1 再 --keep 注入 S5,两个 fault 标记共存
def test_inject_keep_stacks_faults(tmp_path: Path) -> None:
    from kyle_agent.oncall.inject import main

    env_dir = str(tmp_path / "env")
    main(["gateway-mem-leak", "--env-dir", env_dir])
    main(["gateway-cpu-saturation", "--env-dir", env_dir, "--keep"])
    env = SimEnv(Path(env_dir))
    faults = env.read_state()["gateway"]["faults"]
    assert "gateway-mem-leak" in faults and "gateway-cpu-saturation" in faults
    assert len(env.read_alerts()) == 2


# 功能:BY_ID 与 SCENARIOS 同源
def test_by_id_lookup() -> None:
    assert BY_ID["orders-disk-full"].expected_actions[0]["tool"] == "log_cleanup"
