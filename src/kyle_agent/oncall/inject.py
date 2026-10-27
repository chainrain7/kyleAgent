"""故障注入 CLI(Phase 1)。

    uv run python -m kyle_agent.oncall.inject --list
    uv run python -m kyle_agent.oncall.inject <fault_id> [--env-dir X] [--keep]
    uv run python -m kyle_agent.oncall.inject --reset [--env-dir X]

注入前默认 reset 环境到基线(--keep 跳过),保证单剧本演示的可复现性;
env 目录默认 ~/.kyle/oncall/env,可用 KYLE_ONCALL_ENV 覆盖。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from kyle_agent.oncall.faults import BY_ID, SCENARIOS
from kyle_agent.oncall.sim import SimEnv


# 解析 env 目录:--env-dir > KYLE_ONCALL_ENV > ~/.kyle/oncall/env
def _env_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("KYLE_ONCALL_ENV")
    if env:
        return Path(env).expanduser()
    return Path("~/.kyle/oncall/env").expanduser()


# CLI 入口:list / reset / inject
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kyle-oncall-inject", description="OnCallCopilot 故障注入"
    )
    parser.add_argument("fault_id", nargs="?", help="剧本 id(--list 查看)")
    parser.add_argument("--list", action="store_true", help="列出全部剧本")
    parser.add_argument("--reset", action="store_true", help="重置环境到基线")
    parser.add_argument("--keep", action="store_true", help="注入前不重置(叠加故障)")
    parser.add_argument("--env-dir", help="环境目录(默认 ~/.kyle/oncall/env)")
    args = parser.parse_args(argv)

    if args.list:
        for s in SCENARIOS:
            marker = " ✓可自动修复" if s.auto_recoverable else " ○诊断类(零变更)"
            print(f"{s.id:32s} [{s.severity}] {s.alert_summary}{marker}")
        return 0

    env_path = _env_dir(args.env_dir)

    if args.reset:
        SimEnv.reset(env_path)
        print(f"env reset -> {env_path}")
        return 0

    if not args.fault_id:
        parser.print_help()
        return 1

    scenario = BY_ID.get(args.fault_id)
    if scenario is None:
        print(f"error: unknown fault_id {args.fault_id!r}(--list 查看)", file=sys.stderr)
        return 1

    env = SimEnv.reset(env_path) if not args.keep else SimEnv(env_path)
    scenario.inject(env)
    alert = env.read_alerts()[-1]
    print(f"injected: {scenario.id} [{scenario.severity}]")
    print(f"  alert : {alert['alert_id']} {scenario.alert_summary}")
    print(f"  env   : {env_path}")
    if not scenario.auto_recoverable:
        print("  note  : 诊断类剧本——期望零变更执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
