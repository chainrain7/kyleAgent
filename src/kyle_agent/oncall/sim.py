"""OnCallCopilot 模拟生产环境内核(Phase 1)。

SimEnv 封装一个纯文件的"微型生产环境"(技术设计 §2):
拓扑(services.yaml)+ 动态状态(state.json)+ 时序指标(metrics/*.csv)
+ 结构化日志(logs/*.log)+ 告警队列(alerts.json)。

确定性设计:
- baseline/ 模板随包分发,reset 原样拷贝并"物化"时间戳(以当前时刻为
  t0 重写 ts 列),窗口永远是"最近 15 分钟";
- 一切注入/恢复都是文件追加或整文件重写,无随机、无网络、无进程。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

_BASELINE_DIR = Path(__file__).parent / "baseline"
_METRIC_COLUMNS = [
    "ts", "p99_ms", "error_rate", "memory_mb", "qps", "cpu_pct",
    "disk_pct", "queue_lag", "db_qps", "cache_hit_pct",
]


# 返回 HH:MM 形式的当前时刻
def _clock_now() -> str:
    return datetime.now().strftime("%H:%M")


# 返回当前 UTC 时间 ISO 串(告警/日志绝对时间)
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Topology:
    # 服务名 → 部署信息(副本数/依赖)
    services: dict[str, dict[str, Any]]
    # 不可操作的外部依赖名列表
    externals: list[str] = field(default_factory=list)

    # 返回依赖指定服务的服务列表(影响面推导)
    def dependents_of(self, service: str) -> list[str]:
        return [
            name for name, info in self.services.items()
            if service in info.get("depends_on", [])
        ]


# 解析 YAML 子集拓扑文件(仅 services/externals 两键,避免引入 pyyaml)
def _parse_topology(text: str) -> Topology:
    services: dict[str, dict[str, Any]] = {}
    externals: list[str] = []
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if line.startswith("services:"):
            current = "services"
        elif line.startswith("externals:"):
            current = "externals"
        elif current == "services" and line.startswith("  ") and ":" in stripped:
            name, _, rest = stripped.partition(":")
            info: dict[str, Any] = {"replicas": 2, "depends_on": []}
            services[name.strip()] = info
            rest = rest.strip()
            if rest.startswith("{") and rest.endswith("}"):
                for part in rest[1:-1].split(","):
                    k, _, v = part.partition(":")
                    k, v = k.strip(), v.strip()
                    if k == "depends_on":
                        info["depends_on"] = [
                            d.strip() for d in v.strip("[]").split(",") if d.strip()
                        ]
                    elif k == "replicas":
                        info["replicas"] = int(v)
        elif current == "externals" and stripped.startswith("- "):
            externals.append(stripped[2:].strip())
    return Topology(services=services, externals=externals)


class SimEnv:
    # 绑定一个环境目录;目录必须已经过 reset 物化
    def __init__(self, env_dir: Path) -> None:
        self.dir = env_dir

    # ── 生命周期 ──────────────────────────────────────────────

    # 从包内 baseline 模板重建环境并物化时间戳,返回绑定实例
    @classmethod
    def reset(cls, env_dir: Path) -> SimEnv:
        if env_dir.exists():
            shutil.rmtree(env_dir)
        shutil.copytree(_BASELINE_DIR, env_dir)
        env = cls(env_dir)
        env._materialize_timestamps()
        logger.info("sim env reset at %s", env_dir)
        return env

    # 把模板中的相对时间占位(T-14 … T-0)替换为以当前时刻为终点的真实钟点
    def _materialize_timestamps(self) -> None:
        t0 = datetime.now()
        offsets = {f"T-{i}": (t0 - timedelta(minutes=i)).strftime("%H:%M")
                   for i in range(0, 15)}
        for csv_path in (self.dir / "metrics").glob("*.csv"):
            rows = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
            for row in rows:
                if row and row[0] in offsets:
                    row[0] = offsets[row[0]]
            csv_path.write_text(
                "\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8"
            )
        for log_path in (self.dir / "logs").glob("*.log"):
            lines: list[str] = []
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                record = json.loads(line)
                token = str(record.get("ts", ""))
                if token in offsets:
                    record["ts"] = offsets[token]
                elif token == "NOW":
                    record["ts"] = t0.strftime("%H:%M:%S")
                lines.append(json.dumps(record, ensure_ascii=False))
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── 拓扑与状态 ────────────────────────────────────────────

    # 读取服务拓扑(services + externals + 依赖)
    def topology(self) -> Topology:
        return _parse_topology(
            (self.dir / "services.yaml").read_text(encoding="utf-8")
        )

    # 读取全部服务动态状态
    def read_state(self) -> dict[str, dict[str, Any]]:
        data = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        return cast("dict[str, dict[str, Any]]", data)

    # 整体写回服务状态
    def write_state(self, state: dict[str, dict[str, Any]]) -> None:
        (self.dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # 修改单个服务的部分字段;fault_id 提供时追加故障标记
    def patch_service(
        self, service: str, fault_id: str | None = None, **fields: Any
    ) -> None:
        state = self.read_state()
        entry = state[service]
        entry.update(fields)
        if fault_id is not None and fault_id not in entry.get("faults", []):
            entry.setdefault("faults", []).append(fault_id)
        self.write_state(state)

    # ── 日志 ──────────────────────────────────────────────────

    # 追加一条结构化日志(ts 缺省为当前钟点)
    def append_log(
        self, service: str, level: str, msg: str, ts: str | None = None
    ) -> None:
        record = {"ts": ts or datetime.now().strftime("%H:%M:%S"),
                  "level": level, "msg": msg}
        with (self.dir / "logs" / f"{service}.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 读取服务日志尾部的原始行
    def log_tail(self, service: str, n: int = 50) -> list[str]:
        path = self.dir / "logs" / f"{service}.log"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[-n:]

    # ── 指标 ──────────────────────────────────────────────────

    # 追加若干分钟指标行(字段缺省继承该服务最近一行)
    def append_metrics(self, service: str, minutes: list[dict[str, Any]]) -> None:
        path = self.dir / "metrics" / f"{service}.csv"
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
        base = dict(rows[-1]) if rows else {c: "0" for c in _METRIC_COLUMNS}
        base["ts"] = _clock_now()
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=_METRIC_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        for patch in minutes:
            row = {**base, **{k: str(v) for k, v in patch.items()}}
            writer.writerow(row)
        path.write_text(out.getvalue(), encoding="utf-8")

    # 读取指标尾部长度为 n 的数值行(ts 之外全部转 float)
    def metrics_tail(self, service: str, n: int = 15) -> list[dict[str, float]]:
        path = self.dir / "metrics" / f"{service}.csv"
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))[-n:]
        parsed: list[dict[str, float]] = []
        for row in rows:
            item = {k: float(v) for k, v in row.items() if k != "ts"}
            parsed.append(item)
        return parsed

    # ── 告警 ──────────────────────────────────────────────────

    # 读取告警队列
    def read_alerts(self) -> list[dict[str, Any]]:
        data = json.loads((self.dir / "alerts.json").read_text(encoding="utf-8"))
        return cast("list[dict[str, Any]]", data)

    # 写回告警队列
    def write_alerts(self, alerts: list[dict[str, Any]]) -> None:
        (self.dir / "alerts.json").write_text(
            json.dumps(alerts, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # 新增一条 firing 告警,返回其 alert_id(编号自增)
    def append_alert(self, service: str, severity: str, summary: str) -> str:
        alerts = self.read_alerts()
        seq = len(alerts) + 1
        alert_id = f"alert-{seq:03d}"
        alerts.append({
            "alert_id": alert_id, "service": service, "severity": severity,
            "summary": summary, "ts": _now_iso(), "status": "firing",
        })
        self.write_alerts(alerts)
        return alert_id
