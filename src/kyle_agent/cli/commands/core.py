from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from kyle_agent.core.config import KyleConfig

_PID_FILE = Path.home() / ".kyle" / "kyle-core.pid"


# 判断进程是否存活；Windows 上 os.kill(pid, 0) 不是探测（会抛 WinError 87 或直接杀进程），
# 必须用 Win32 API 查询退出码
def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: KyleConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except ValueError:
        _PID_FILE.unlink(missing_ok=True)
        return None
    if _pid_alive(pid):
        return pid
    _PID_FILE.unlink(missing_ok=True)
    return None


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: KyleConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: KyleConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    proc = subprocess.Popen(
        [sys.executable, "-m", "kyle_agent.core"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 停止 daemon；POSIX 发 SIGTERM 走优雅关闭，Windows 上 os.kill 等价 TerminateProcess（强制终止）
def cmd_core_stop(config: KyleConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # 竞态：进程刚好已退出
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
