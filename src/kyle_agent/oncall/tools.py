"""OnCallCopilot 领域工具层(Phase 2)。

7 个值班工具 + submit_diagnosis,全部读写模拟环境(SimEnv):
- 只读:alerts_list / service_status / logs_query / metrics_query / runbook_search
- 变更(默认 ASK):service_restart / scale_up / log_cleanup
- 结论:submit_diagnosis(结构化诊断提交,评测与报告的准绳)

恢复语义(核心):变更工具执行后按"剧本期望动作"精确清除故障——
fault_id 经 BY_ID 查得其 expected_actions,与本动作(tool+service)
匹配才判定可恢复;传导性 degraded(依赖宕机拖累调用方)在根因服务
恢复后自动解除;服务完全恢复时其 firing 告警转 resolved。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kyle_agent.core.tools.base import BaseTool, ToolResult
from kyle_agent.oncall.sim import SimEnv

_RUNBOOK_DIR = Path(__file__).parent / "runbooks"
_MAX_LOG_LINES = 60
_SCALE_LIMIT = 6


# ── 只读工具 ──────────────────────────────────────────────


class AlertsListParams(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AlertsListTool(BaseTool):
    params_model = AlertsListParams
    name = "alerts_list"
    description = "List active alerts in the on-call queue (firing first)."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 绑定环境目录(惰性打开,注册期不要求 env 已存在)
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 返回告警队列文本(firing 在前)
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        alerts = SimEnv(self._env_dir).read_alerts()
        if not alerts:
            return ToolResult(content="No alerts.")
        ordered = sorted(
            alerts, key=lambda a: (a.get("status") != "firing", a.get("alert_id", ""))
        )
        lines = [
            f"[{a['status']}] {a['alert_id']} {a['severity']} {a['service']}: "
            f"{a['summary']} (ts={a['ts']})"
            for a in ordered
        ]
        return ToolResult(content="\n".join(lines))


class ServiceStatusParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str = ""


class ServiceStatusTool(BaseTool):
    params_model = ServiceStatusParams
    name = "service_status"
    description = (
        "Show current status of services (or one service) plus external "
        "dependencies. Use this first to see the full topology state."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Optional service name."},
        },
    }

    # 绑定环境目录
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 返回状态总览(单服务或全部 + 外部依赖清单)
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ServiceStatusParams.model_validate(params)
        env = SimEnv(self._env_dir)
        state = env.read_state()
        topo = env.topology()
        names = [p.service] if p.service else list(state)
        missing = [n for n in names if n not in state]
        if missing:
            return ToolResult(
                content=f"unknown service(s): {missing}. "
                f"known: {list(state)}; externals: {topo.externals}",
                is_error=True, error_type="schema_error",
            )
        lines = []
        for name in names:
            s = state[name]
            deps = topo.services.get(name, {}).get("depends_on", [])
            lines.append(
                f"{name}: {s['status']} · replicas={s['replicas']} "
                f"· err={s.get('error_rate', 0)} · p99={s.get('p99_ms', 0)}ms "
                f"· qps={s.get('qps', 0)} · mem={s.get('memory_mb', 0)}MB "
                f"· disk={s.get('disk_pct', 0)}% · deps={deps or '-'}"
            )
        lines.append(
            "externals (NOT operable, diagnose via logs/metrics): "
            + ", ".join(topo.externals)
        )
        return ToolResult(content="\n".join(lines))


class LogsQueryParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str
    keyword: str = ""
    level: str = ""
    tail: int = Field(default=50, ge=1, le=200)


class LogsQueryTool(BaseTool):
    params_model = LogsQueryParams
    name = "logs_query"
    description = (
        "Query structured logs of a service. Filter by keyword (case-"
        "insensitive substring) and level (INFO/WARN/ERROR). Returns most "
        "recent matches first, capped at 60 lines."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "keyword": {"type": "string"},
            "level": {"type": "string", "enum": ["INFO", "WARN", "ERROR", "DEBUG"]},
            "tail": {"type": "integer", "description": "Scan last N lines (default 50)."},
        },
        "required": ["service"],
    }

    # 绑定环境目录
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 过滤日志行并返回文本(保持时间顺序)
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = LogsQueryParams.model_validate(params)
        env = SimEnv(self._env_dir)
        state = env.read_state()
        if p.service not in state:
            return ToolResult(
                content=f"unknown service: {p.service}. known: {list(state)}",
                is_error=True, error_type="schema_error",
            )
        raw = env.log_tail(p.service, p.tail)
        hits: list[str] = []
        kw = p.keyword.lower()
        for line in raw:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if p.level and rec.get("level", "").upper() != p.level.upper():
                continue
            msg = str(rec.get("msg", ""))
            if kw and kw not in msg.lower():
                continue
            hits.append(f"{rec.get('ts')} {rec.get('level')} {msg}")
        if not hits:
            return ToolResult(
                content=f"no matching log lines (service={p.service}, "
                f"keyword={p.keyword!r}, level={p.level!r})"
            )
        shown = hits[-_MAX_LOG_LINES:]
        return ToolResult(content="\n".join(shown))


class MetricsQueryParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str
    metric: str = ""
    window_min: int = Field(default=15, ge=1, le=15)


class MetricsQueryTool(BaseTool):
    params_model = MetricsQueryParams
    name = "metrics_query"
    description = (
        "Query time-series metrics of a service (15min window, 1min step). "
        "Returns current/mean/max per metric and the step-change point if "
        "any. Metrics: p99_ms, error_rate, memory_mb, qps, cpu_pct, "
        "disk_pct, queue_lag, db_qps, cache_hit_pct."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "metric": {"type": "string", "description": "Optional single metric."},
            "window_min": {"type": "integer"},
        },
        "required": ["service"],
    }

    # 绑定环境目录
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 计算摘要统计并指出突变点
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MetricsQueryParams.model_validate(params)
        env = SimEnv(self._env_dir)
        state = env.read_state()
        if p.service not in state:
            return ToolResult(
                content=f"unknown service: {p.service}. known: {list(state)}",
                is_error=True, error_type="schema_error",
            )
        rows = env.metrics_tail(p.service, p.window_min)
        if not rows:
            return ToolResult(content="no metrics rows")
        cols = [p.metric] if p.metric else list(rows[0])
        lines = [f"{p.service} last {len(rows)}min:"]
        for col in cols:
            vals = [r[col] for r in rows if col in r]
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            # 突变点:后半均值偏离前半均值 > 50% 时给出提示
            half = max(1, len(vals) // 2)
            head, tail = vals[:half], vals[half:]
            hm, tm = sum(head) / len(head), sum(tail) / len(tail)
            shift = ""
            if hm > 0 and abs(tm - hm) / hm > 0.5:
                shift = "  <-- step change detected"
            lines.append(
                f"  {col}: current={vals[-1]} mean={mean:.4g} "
                f"max={max(vals)}{shift}"
            )
        return ToolResult(content="\n".join(lines))


class RunbookSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1)


class RunbookSearchTool(BaseTool):
    params_model = RunbookSearchParams
    name = "runbook_search"
    description = (
        "Search the on-call runbook knowledge base for handling procedures. "
        "Returns the most relevant runbook sections (trigger signature, "
        "diagnosis steps, action, and prohibitions)."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    # 绑定环境目录(runbook 只读包内,不需 env;保持工具构造统一)
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 关键词打分检索 runbook(库规模 <20 篇,全文扫描即可,无需 FTS 索引)
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = RunbookSearchParams.model_validate(params)
        terms = [w for w in re.split(r"[\s,，]+", p.query.lower()) if w]
        scored: list[tuple[float, str]] = []
        for path in sorted(_RUNBOOK_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            lower = text.lower()
            title = path.stem.replace("-", " ")
            score = 0.0
            for term in terms:
                score += lower.count(term) * 1.0
                if term in title:
                    score += 5.0
            if score > 0:
                scored.append((score, text.strip()))
        if not scored:
            available = [p2.stem for p2 in _RUNBOOK_DIR.glob("*.md")]
            return ToolResult(
                content=f"no matching runbook. available: {available}"
            )
        scored.sort(key=lambda x: -x[0])
        top = scored[:3]
        return ToolResult(
            content="\n\n---\n\n".join(t for _, t in top)
        )


# ── 变更工具(默认 ASK) ───────────────────────────────────


# 判定 fault 是否可由该动作清除(fault 的期望动作含同 tool+service)
def _fault_recoverable_by(fault_id: str, tool: str, service: str) -> bool:
    from kyle_agent.oncall.faults import BY_ID

    scenario = BY_ID.get(fault_id)
    if scenario is None:
        return False
    return any(
        a["tool"] == tool and a["params"].get("service") == service
        for a in scenario.expected_actions
    )


# 应用恢复:清除匹配故障→恢复基线值→追加恢复段;传导 degraded 一并解除;
# 完全恢复的服务告警转 resolved。返回恢复摘要行列表
def _apply_recovery(env: SimEnv, tool: str, service: str) -> list[str]:
    state = env.read_state()
    baseline = SimEnv.baseline_state()
    notes: list[str] = []

    # 1) 本服务:清除可由该动作恢复的故障
    entry = state[service]
    kept_faults = [
        f for f in entry.get("faults", [])
        if not _fault_recoverable_by(f, tool, service)
    ]
    cleared = [f for f in entry.get("faults", []) if f not in kept_faults]
    if cleared:
        entry["faults"] = kept_faults
        notes.append(f"cleared fault(s): {', '.join(cleared)}")
        if not kept_faults and service in baseline:
            healthy = dict(baseline[service])
            healthy["replicas"] = entry.get("replicas", healthy["replicas"])
            if tool == "scale_up":
                healthy["replicas"] = entry.get("replicas", healthy["replicas"])
            state[service] = healthy
            notes.append(f"{service} restored to healthy baseline")
    env.write_state(state)

    # 2) 传导解除:自身无故障但状态非 running 的服务,若其依赖已恢复则复位
    topo = env.topology()
    state = env.read_state()
    for name, info in state.items():
        if name == service or info.get("faults"):
            continue
        if info.get("status") == "running":
            continue
        deps = topo.services.get(name, {}).get("depends_on", [])
        dep_sick = any(
            d in state and state[d].get("status") != "running" for d in deps
        )
        if not dep_sick:
            healthy = dict(baseline.get(name, info))
            healthy["faults"] = []
            state[name] = healthy
            notes.append(f"{name} cascade-recovered (dependency healthy)")
    env.write_state(state)

    # 3) 完全恢复的服务:其 firing 告警转 resolved
    alerts = env.read_alerts()
    for a in alerts:
        if a.get("status") != "firing":
            continue
        svc = a.get("service", "")
        if svc in state and not state[svc].get("faults") \
                and state[svc].get("status") == "running":
            a["status"] = "resolved"
            notes.append(f"alert {a['alert_id']} resolved")
    env.write_alerts(alerts)

    # 4) 追加恢复痕迹(指标+日志),便于复盘
    env.append_metrics(service, [{}])
    env.append_log(service, "INFO", f"recovery applied by oncall-copilot: {tool}")
    return notes


class ServiceRestartParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str


class ServiceRestartTool(BaseTool):
    params_model = ServiceRestartParams
    name = "service_restart"
    description = (
        "Rolling-restart a service (requires approval). Clears crash/leak/"
        "dependency faults on that service and restores healthy baseline."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"service": {"type": "string"}},
        "required": ["service"],
    }

    # 绑定环境目录
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 重启:down/crashloop/degraded 恢复;已 running 提示幂等
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ServiceRestartParams.model_validate(params)
        env = SimEnv(self._env_dir)
        state = env.read_state()
        if p.service not in state:
            return ToolResult(
                content=f"unknown service: {p.service}",
                is_error=True, error_type="schema_error",
            )
        entry = state[p.service]
        has_faults = bool(entry.get("faults"))
        if entry.get("status") == "running" and not has_faults:
            return ToolResult(
                content=f"{p.service} already running and healthy — "
                f"restart skipped (idempotent)"
            )
        notes = _apply_recovery(env, "service_restart", p.service)
        env.append_log(p.service, "INFO", "rolling restart completed (3/3 replicas)")
        return ToolResult(
            content=f"service_restart {p.service}: done\n" + "\n".join(notes)
        )


class ScaleUpParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str
    delta: int = Field(default=1, ge=1, le=4)


class ScaleUpTool(BaseTool):
    params_model = ScaleUpParams
    name = "scale_up"
    description = (
        "Scale out a service by delta replicas (requires approval). "
        "Relieves capacity-type faults. Replica limit is 6."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "delta": {"type": "integer", "description": "Replicas to add (default 1)."},
        },
        "required": ["service"],
    }

    # 绑定环境目录
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 扩容:副本+delta(上限 6);容量类故障随之清除
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ScaleUpParams.model_validate(params)
        env = SimEnv(self._env_dir)
        state = env.read_state()
        if p.service not in state:
            return ToolResult(
                content=f"unknown service: {p.service}",
                is_error=True, error_type="schema_error",
            )
        entry = state[p.service]
        new_replicas = min(_SCALE_LIMIT, entry["replicas"] + p.delta)
        if new_replicas == entry["replicas"]:
            return ToolResult(
                content=f"{p.service} already at replica limit {_SCALE_LIMIT}",
                is_error=True, error_type="runtime_error",
            )
        entry["replicas"] = new_replicas
        env.write_state(state)
        notes = _apply_recovery(env, "scale_up", p.service)
        env.append_log(p.service, "INFO", f"scaled out to {new_replicas} replicas")
        return ToolResult(
            content=f"scale_up {p.service}: replicas -> {new_replicas}\n"
            + "\n".join(notes)
        )


class LogCleanupParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str


class LogCleanupTool(BaseTool):
    params_model = LogCleanupParams
    name = "log_cleanup"
    description = (
        "Truncate aged logs of a service to free disk (requires approval). "
        "Clears disk-full type faults. Keeps the most recent 50 lines."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"service": {"type": "string"}},
        "required": ["service"],
    }

    # 绑定环境目录
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 清日志:保留最近 50 行,磁盘类故障清除
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = LogCleanupParams.model_validate(params)
        env = SimEnv(self._env_dir)
        state = env.read_state()
        if p.service not in state:
            return ToolResult(
                content=f"unknown service: {p.service}",
                is_error=True, error_type="schema_error",
            )
        log_path = env.dir / "logs" / f"{p.service}.log"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        kept = lines[-50:]
        freed = max(0, len(lines) - len(kept))
        log_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        notes = _apply_recovery(env, "log_cleanup", p.service)
        env.append_log(p.service, "INFO", f"log cleanup: rotated {freed} aged lines")
        return ToolResult(
            content=f"log_cleanup {p.service}: freed {freed} lines\n"
            + "\n".join(notes)
        )


# ── 诊断结论提交 ──────────────────────────────────────────


class SubmitDiagnosisParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    root_cause: str = Field(min_length=1)
    recommended_actions: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str = ""


class SubmitDiagnosisTool(BaseTool):
    params_model = SubmitDiagnosisParams
    name = "submit_diagnosis"
    description = (
        "Submit the structured diagnosis conclusion. Call exactly once at "
        "the end: root_cause (one sentence), recommended_actions (list of "
        "{tool, params}; empty if the fault needs human/external action), "
        "confidence (0-1), evidence (key log/metric lines you relied on)."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string"},
            "recommended_actions": {
                "type": "array",
                "items": {"type": "object"},
            },
            "confidence": {"type": "number"},
            "evidence": {"type": "string"},
        },
        "required": ["root_cause"],
    }

    # 占位构造(与其他领域工具统一由 build_oncall_tools 创建)
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir

    # 校验并确认收到结论(评测从事件流读取参数)
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SubmitDiagnosisParams.model_validate(params)
        actions = json.dumps(p.recommended_actions, ensure_ascii=False)
        return ToolResult(
            content=(
                f"diagnosis recorded.\nroot_cause: {p.root_cause}\n"
                f"recommended_actions: {actions}\n"
                f"confidence: {p.confidence}"
            )
        )


# 构建全部领域工具(注册入口,runner 在 [oncall] enabled 时调用)
def build_oncall_tools(env_dir: Path) -> list[BaseTool]:
    return [
        AlertsListTool(env_dir),
        ServiceStatusTool(env_dir),
        LogsQueryTool(env_dir),
        MetricsQueryTool(env_dir),
        RunbookSearchTool(env_dir),
        ServiceRestartTool(env_dir),
        ScaleUpTool(env_dir),
        LogCleanupTool(env_dir),
        SubmitDiagnosisTool(env_dir),
    ]
