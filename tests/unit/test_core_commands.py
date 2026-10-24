from __future__ import annotations

import subprocess
import sys
import time

from kyle_agent.cli.commands.core import _pid_alive


# 功能：_pid_alive 对存活进程返回 True
# 设计：起一个 sleep 子进程并立即探测；Windows 上该路径走 Win32 OpenProcess（曾因
#       os.kill(pid,0) 抛 WinError 87 导致 kyle core stop 崩溃），POSIX 走 os.kill 探测
def test_pid_alive_true_for_running_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _pid_alive(proc.pid):
            time.sleep(0.05)
        assert _pid_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


# 功能：_pid_alive 对已退出进程返回 False，且不抛异常
# 设计：起子进程等其退出后探测；覆盖 pid 文件清理依赖的"进程已死"判定分支
def test_pid_alive_false_for_exited_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    time.sleep(0.2)
    assert _pid_alive(proc.pid) is False
