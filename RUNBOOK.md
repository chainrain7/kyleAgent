# 运维手册（RUNBOOK）

## 日常操作

### 启动守护进程

```bash
uv run kyle-core
```

默认监听 `127.0.0.1:7437`，按 `Ctrl+C` 优雅退出。

### 验证连通

```bash
uv run kyle ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
kill $(pgrep -f kyle-core)
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.kyle/config.toml` → `.kyle/config.toml` → `.env` → 系统环境变量**。

### `~/.kyle/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.kyle/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KYLE_CONFIG` | `~/.kyle/config.toml` | 覆盖配置文件路径 |
| `KYLE_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KYLE_PORT` | `7437` | TCP 监听端口 |
| `KYLE_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `KYLE_LOG_FILE` | `~/.kyle/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `KYLE_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```bash
tail -f ~/.kyle/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `kill $(pgrep -f kyle-core)` |
| `core not running` | 未启动守护进程 | `uv run kyle-core` |
| `Address already in use` | 端口被其他进程占用 | `KYLE_PORT=8000 uv run kyle-core` |
| `Config error: KYLE_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `KYLE_PORT` 的值 |

---

## 会话恢复（TUI 断线重连）

TUI 掉线或重启后自动调用 `session.resume`：daemon 从 `~/.kyle/sessions/` 磁盘扫描
最近一个未关闭 session 重新载入内存，返回 `session_id` 与 `last_run_id`；客户端以
`event.subscribe(replay_from_run=last_run_id)` 回放补齐事件。daemon 未重启时运行中的
任务不受影响。显式恢复指定 session：`session.resume {"session_id": "sess-..."}`。

## 运行指标统计

```bash
uv run python scripts/run_metrics.py <events.jsonl 路径或 run 目录> [--compare <另一份>]
```

输出 Markdown 报告：run 状态、LLM 调用与按模型分组的 token 用量、工具成败与耗时分布、
compact 次数；`--compare` 生成前后对比表（模型路由/压缩策略改动的量化依据）。
