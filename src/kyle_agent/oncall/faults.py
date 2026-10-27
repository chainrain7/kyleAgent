"""OnCallCopilot 故障剧本库(Phase 1)——20 个演练场景的定义与注入。

每个剧本 = 一次可复现的"生产事故":注入函数改写模拟环境
(状态/指标尾段/特征日志/告警),并携带评测所需的期望
(root_cause_keywords 任一命中即定位正确;expected_actions 与
终态一一对应;auto_recoverable=False 的诊断类剧本期望零变更)。

清单与技术设计 §4 一一对应,改动需同步文档。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kyle_agent.oncall.sim import SimEnv

# 变更工具合法名(期望动作的取值域,与 Phase 2 工具层对齐)
_MUTATION_TOOLS = frozenset({"service_restart", "scale_up", "log_cleanup"})


class FaultScenario:
    """一个故障演练剧本的完整定义(注入动作 + 评测期望)。"""

    # 绑定剧本元数据与注入函数
    def __init__(
        self,
        id: str,
        category: str,
        alert_service: str,
        severity: str,
        alert_summary: str,
        inject: Callable[[SimEnv], None],
        root_cause_keywords: list[str],
        expected_actions: list[dict[str, Any]] | None = None,
        auto_recoverable: bool = True,
        note: str = "",
    ) -> None:
        self.id = id
        self.category = category
        self.alert_service = alert_service
        self.severity = severity
        self.alert_summary = alert_summary
        self.inject = inject
        self.root_cause_keywords = root_cause_keywords
        self.expected_actions = expected_actions or []
        self.auto_recoverable = auto_recoverable
        self.note = note


# ── 注入辅助 ──────────────────────────────────────────────


# 追加若干分钟异常指标段(仅写变化列,其余继承基线)
def _spike(env: SimEnv, svc: str, n: int, **cols: Any) -> None:
    env.append_metrics(svc, [dict(cols) for _ in range(n)])


# 追加特征日志行
def _logs(env: SimEnv, svc: str, lines: list[tuple[str, str]]) -> None:
    for level, msg in lines:
        env.append_log(svc, level, msg)


# ── 剧本注入函数(具名定义) ───────────────────────────────


# S1 gateway 内存泄漏
def _inject_s1(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-mem-leak",
                    memory_mb=940, status="degraded")
    _spike(e, "gateway", 8, memory_mb=940, cpu_pct=71)
    _logs(e, "gateway", [
        ("WARN", "memory usage 94% of limit (940/1024MB), OOM risk"),
        ("ERROR", "OOMKilled container restart #1 (exit 137)"),
    ])
    e.append_alert("gateway", "P1", "gateway 内存 92% 持续上涨,逼近容器上限")


# S2 orders 坏配置崩溃循环
def _inject_s2(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-crashloop-badconfig",
                    status="crashloop", replicas=1, error_rate=0.42)
    _spike(e, "orders", 6, error_rate=0.42, qps=90)
    _logs(e, "orders", [
        ("ERROR", 'config validation failed: unknown key "max_retries"'),
        ("ERROR", "CrashLoopBackOff backoff 5m0s (restart #5, exit 1)"),
    ])
    e.append_alert("orders", "P1", "orders CrashLoopBackOff,5 分钟内重启 5 次")


# S3 payments 连接池耗尽
def _inject_s3(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-connpool-exhaust",
                    status="degraded", error_rate=0.28, p99_ms=920)
    _spike(e, "payments", 7, error_rate=0.28, p99_ms=920)
    _logs(e, "payments", [
        ("ERROR", "ConnectionPool exhausted acquiring connection (timeout 5000ms)"),
        ("ERROR", "payment failed: db connection acquire timeout"),
    ])
    e.append_alert("payments", "P1", "payments 大量请求超时,连接池获取失败")


# S4 orders 磁盘写满
def _inject_s4(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-disk-full",
                    disk_pct=96, status="degraded")
    _spike(e, "orders", 5, disk_pct=96)
    _logs(e, "orders", [
        ("ERROR", "write /var/log/orders/app.log: no space left on device"),
        ("WARN", "disk usage 96% (threshold 90%)"),
    ])
    e.append_alert("orders", "P2", "orders 磁盘使用 96%,写入受限")


# S5 gateway CPU 打满
def _inject_s5(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-cpu-saturation",
                    cpu_pct=97, qps=1500, p99_ms=480, status="degraded")
    _spike(e, "gateway", 6, cpu_pct=97, qps=1500, p99_ms=480)
    _logs(e, "gateway", [
        ("WARN", "cpu 97% sustained 6m (limit 2 cores)"),
        ("WARN", "p99 latency 480ms (baseline 120ms), queue building"),
    ])
    e.append_alert("gateway", "P2", "gateway CPU 97%,p99 延迟翻 4 倍")


# S6 orders 消费积压
def _inject_s6(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-queue-backlog",
                    queue_lag=120000, status="degraded")
    _spike(e, "orders", 7, queue_lag=120000)
    _logs(e, "orders", [
        ("WARN", "consumer lag 120000 msgs (partition orders-0)"),
        ("WARN", "consume rate 80/s < produce rate 210/s"),
    ])
    e.append_alert("orders", "P2", "orders 消费积压 12 万条且持续增长")


# S7 gateway 5xx(根因 orders 宕机)
def _inject_s7(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="gateway-dep-orders-down",
                    status="down", replicas=0)
    e.patch_service("gateway", status="degraded", error_rate=0.18)
    _spike(e, "orders", 6, qps=0)
    _spike(e, "gateway", 6, error_rate=0.18)
    _logs(e, "gateway", [
        ("ERROR", "upstream orders unreachable (dial tcp refused)"),
        ("ERROR", "502 Bad Gateway -> /api/orders (upstream down)"),
    ])
    _logs(e, "orders", [("ERROR", "service stopped unexpectedly (signal 9)")])
    e.append_alert("gateway", "P1", "gateway 5xx 飙升至 18%,下游不可达")


# S8 gateway 超时(根因 payments 宕机)
def _inject_s8(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="gateway-dep-payments-down",
                    status="down", replicas=0)
    e.patch_service("gateway", status="degraded", error_rate=0.12, p99_ms=810)
    _spike(e, "payments", 6, qps=0)
    _spike(e, "gateway", 6, error_rate=0.12, p99_ms=810)
    _logs(e, "gateway", [
        ("ERROR", "upstream payments timeout after 3000ms"),
        ("ERROR", "504 Gateway Timeout -> /api/pay"),
    ])
    _logs(e, "payments", [("ERROR", "killed by OOM (exit 137)")])
    e.append_alert("gateway", "P1", "gateway 下游调用大面积超时,支付链路受阻")


# S9 orders 变慢(外部 orders-db 慢查询)
def _inject_s9(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-dep-db-slow",
                    p99_ms=890, status="degraded")
    _spike(e, "orders", 8, p99_ms=890)
    _logs(e, "orders", [
        ("WARN", "db query slow (>2000ms): SELECT * FROM orders WHERE ..."),
        ("WARN", "orders-db replication lag 45s"),
    ])
    e.append_alert("orders", "P2", "orders 全量请求变慢,p99 达 890ms")


# S10 payments 风控超时(外部 risk-svc)
def _inject_s10(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-dep-risk-timeout",
                    status="degraded", error_rate=0.09, p99_ms=780)
    _spike(e, "payments", 7, error_rate=0.09, p99_ms=780)
    _logs(e, "payments", [
        ("ERROR", "risk-svc call timeout after 2000ms (40% of calls)"),
        ("WARN", "circuit breaker half-open on risk-svc"),
    ])
    e.append_alert("payments", "P2", "payments 风控调用超时率 40%,支付成功率下降")


# S11 payments 证书过期
def _inject_s11(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-cert-expired",
                    status="degraded", error_rate=0.15)
    _spike(e, "payments", 5, error_rate=0.15)
    _logs(e, "payments", [
        ("ERROR", "tls: failed to verify certificate: certificate has expired"),
        ("ERROR", "channel callback handshake failed (x509)"),
    ])
    e.append_alert("payments", "P1", "payments TLS 握手失败,渠道回调中断")


# S12 orders 配置键拼写错误
def _inject_s12(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-config-typo-errors",
                    status="degraded", error_rate=0.08)
    _spike(e, "orders", 6, error_rate=0.08)
    _logs(e, "orders", [
        ("ERROR", 'unknown config key "retr_interval" - using default'),
        ("ERROR", "order create failed: KeyError 'retr_interval'"),
    ])
    e.append_alert("orders", "P1", "orders 下单接口 500 尖峰,错误率 8%")


# S13 payments 线程池耗尽
def _inject_s13(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-thread-stuck",
                    status="degraded", p99_ms=1100, error_rate=0.11)
    _spike(e, "payments", 6, p99_ms=1100, error_rate=0.11)
    _logs(e, "payments", [
        ("ERROR", "thread pool exhausted: 200/200 busy, rejecting task"),
        ("WARN", "deadlock suspected: 3 threads waiting on lock L7"),
    ])
    e.append_alert("payments", "P1", "payments 线程池耗尽,请求大量排队")


# S14 orders 缓存失效风暴
def _inject_s14(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-cache-miss-storm",
                    status="degraded", db_qps=480, p99_ms=410)
    _spike(e, "orders", 6, db_qps=480, cache_hit_pct=12, p99_ms=410)
    _logs(e, "orders", [
        ("WARN", "cache hit ratio dropped to 12% (baseline 99%)"),
        ("WARN", "db qps 480 (baseline 60), replica lag rising"),
    ])
    e.append_alert("orders", "P2", "orders 数据库 QPS 涨 8 倍,疑似缓存失效")


# S15 gateway 恶意流量
def _inject_s15(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-dos-traffic",
                    status="degraded", qps=5000, cpu_pct=95, p99_ms=390)
    _spike(e, "gateway", 5, qps=5000, cpu_pct=95, p99_ms=390)
    _logs(e, "gateway", [
        ("WARN", "qps 5000 (baseline 500), 90% from UA 'scraper-bot'"),
        ("WARN", "rate-limit threshold exceeded, shedding 30% traffic"),
    ])
    e.append_alert("gateway", "P1", "gateway 异常流量,QPS 十倍于基线")


# S16 payments 金额计算差错(业务 bug)
def _inject_s16(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-business-rounding")
    _logs(e, "payments", [
        ("ERROR", "rounding mismatch: expected 10.00 got 10.004 (order 88190)"),
        ("WARN", "3 complaints in 10m about amount discrepancy"),
    ])
    e.append_alert("payments", "P2", "payments 少量订单金额计算差错,客诉上报")


# S17 orders 数据库连接上限(外部)
def _inject_s17(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-db-conn-limit",
                    status="degraded", error_rate=0.22)
    _spike(e, "orders", 6, error_rate=0.22)
    _logs(e, "orders", [
        ("ERROR", "orders-db: too many connections (limit 100, in use 100)"),
        ("ERROR", "insert failed: connection refused by orders-db"),
    ])
    e.append_alert("orders", "P1", "orders 报数据库连接数达上限,写入失败")


# S18 payments 上游渠道 5xx 传导
def _inject_s18(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-error-spike-upstream",
                    status="degraded", error_rate=0.06)
    _spike(e, "payments", 6, error_rate=0.06)
    _logs(e, "payments", [
        ("ERROR", "upstream provider returned 503 (service unavailable)"),
        ("WARN", "channel alipay-x error rate 60% from 10:25"),
    ])
    e.append_alert("payments", "P2", "payments 错误率 6%,上游渠道 5xx 传导")


# S19 gateway 日志洪水
def _inject_s19(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-log-flood",
                    disk_pct=91, status="degraded")
    _spike(e, "gateway", 5, disk_pct=91)
    _logs(e, "gateway", [
        ("WARN", "log write rate 20x baseline (debug logging enabled?)"),
        ("WARN", "disk usage 91% (threshold 90%)"),
    ])
    e.append_alert("gateway", "P2", "gateway 磁盘 91%,日志写入速率 20 倍")


# S20 大促全链路流量(多动作)
def _inject_s20(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="blackfriday-surge",
                    status="degraded", qps=1500, p99_ms=380, cpu_pct=88)
    e.patch_service("orders", status="degraded", qps=900, p99_ms=260, cpu_pct=82)
    _spike(e, "gateway", 6, qps=1500, p99_ms=380, cpu_pct=88)
    _spike(e, "orders", 6, qps=900, p99_ms=260, cpu_pct=82, queue_lag=9000)
    _logs(e, "gateway", [
        ("WARN", "qps 1500 (3x baseline), promo traffic pattern detected"),
        ("WARN", "p99 380ms, upstream orders latency elevated"),
    ])
    _logs(e, "orders", [
        ("WARN", "qps 900 (3x baseline), consumer lag 9000 rising"),
    ])
    e.append_alert("gateway", "P1", "全链路 p99 抬升,QPS 三倍于基线(大促流量)")


# ── 剧本注册表(评测与注入 CLI 共用) ──────────────────────

SCENARIOS: list[FaultScenario] = [
    FaultScenario(
        id="gateway-mem-leak", category="leak",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 内存 92% 持续上涨,逼近容器上限",
        inject=_inject_s1,
        root_cause_keywords=["memory", "leak", "内存泄漏", "oom"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "gateway"}}],
        note="基础查证→单动作重启",
    ),
    FaultScenario(
        id="orders-crashloop-badconfig", category="crash",
        alert_service="orders", severity="P1",
        alert_summary="orders CrashLoopBackOff,5 分钟内重启 5 次",
        inject=_inject_s2,
        root_cause_keywords=["config", "配置", "bad", "crashloop"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        note="发布引入坏配置,回滚即重启",
    ),
    FaultScenario(
        id="payments-connpool-exhaust", category="leak",
        alert_service="payments", severity="P1",
        alert_summary="payments 大量请求超时,连接池获取失败",
        inject=_inject_s3,
        root_cause_keywords=["conn", "pool", "连接池"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        note="日志特征定位连接泄漏",
    ),
    FaultScenario(
        id="orders-disk-full", category="capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 磁盘使用 96%,写入受限",
        inject=_inject_s4,
        root_cause_keywords=["disk", "磁盘", "log", "空间"],
        expected_actions=[{"tool": "log_cleanup", "params": {"service": "orders"}}],
        note="非重启类变更:清日志",
    ),
    FaultScenario(
        id="gateway-cpu-saturation", category="capacity",
        alert_service="gateway", severity="P2",
        alert_summary="gateway CPU 97%,p99 延迟翻 4 倍",
        inject=_inject_s5,
        root_cause_keywords=["cpu", "traffic", "流量", "qps", "容量"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "gateway", "delta": 1}},
        ],
        note="容量→扩容",
    ),
    FaultScenario(
        id="orders-queue-backlog", category="capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 消费积压 12 万条且持续增长",
        inject=_inject_s6,
        root_cause_keywords=["lag", "积压", "消费", "consumer", "backlog"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "orders", "delta": 1}},
        ],
        note="指标趋势推理",
    ),
    FaultScenario(
        id="gateway-dep-orders-down", category="dependency",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 5xx 飙升至 18%,下游不可达",
        inject=_inject_s7,
        root_cause_keywords=["orders", "down", "宕机", "upstream", "依赖"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        note="表象在 gateway,根因在 orders——考察定位准确性",
    ),
    FaultScenario(
        id="gateway-dep-payments-down", category="dependency",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 下游调用大面积超时,支付链路受阻",
        inject=_inject_s8,
        root_cause_keywords=["payments", "down", "宕机", "upstream", "依赖"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        note="S7 镜像:根因在 payments",
    ),
    FaultScenario(
        id="orders-dep-db-slow", category="external",
        alert_service="orders", severity="P2",
        alert_summary="orders 全量请求变慢,p99 达 890ms",
        inject=_inject_s9,
        root_cause_keywords=["orders-db", "slow", "慢查询", "外部", "database", "db"],
        note="外部依赖不可操作——诊断并建议 DBA,要求零变更",
        auto_recoverable=False,
    ),
    FaultScenario(
        id="payments-dep-risk-timeout", category="external",
        alert_service="payments", severity="P2",
        alert_summary="payments 风控调用超时率 40%,支付成功率下降",
        inject=_inject_s10,
        root_cause_keywords=["risk", "风控", "外部", "upstream", "timeout"],
        note="外部依赖——建议降级开关,要求零变更",
        auto_recoverable=False,
    ),
    FaultScenario(
        id="payments-cert-expired", category="external",
        alert_service="payments", severity="P1",
        alert_summary="payments TLS 握手失败,渠道回调中断",
        inject=_inject_s11,
        root_cause_keywords=["cert", "证书", "tls", "expired", "过期"],
        note="需人工续期,要求零变更",
        auto_recoverable=False,
    ),
    FaultScenario(
        id="orders-config-typo-errors", category="crash",
        alert_service="orders", severity="P1",
        alert_summary="orders 下单接口 500 尖峰,错误率 8%",
        inject=_inject_s12,
        root_cause_keywords=["config", "配置", "typo", "retr_interval", "key"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        note="与 S2 区分:进程未崩,仅配置键拼写错误",
    ),
    FaultScenario(
        id="payments-thread-stuck", category="leak",
        alert_service="payments", severity="P1",
        alert_summary="payments 线程池耗尽,请求大量排队",
        inject=_inject_s13,
        root_cause_keywords=["thread", "线程", "deadlock", "死锁", "stuck"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        note="同类不同特征(对比 S3 连接池)",
    ),
    FaultScenario(
        id="orders-cache-miss-storm", category="capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 数据库 QPS 涨 8 倍,疑似缓存失效",
        inject=_inject_s14,
        root_cause_keywords=["cache", "缓存", "miss"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "orders", "delta": 1}},
        ],
        note="中间层推理:缓存失效风暴",
    ),
    FaultScenario(
        id="gateway-dos-traffic", category="capacity",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 异常流量,QPS 十倍于基线",
        inject=_inject_s15,
        root_cause_keywords=["dos", "traffic", "流量", "bot", "恶意", "attack"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "gateway", "delta": 2}},
        ],
        note="缓解类动作(扩容扛住,治本靠封禁)",
    ),
    FaultScenario(
        id="payments-business-rounding", category="business",
        alert_service="payments", severity="P2",
        alert_summary="payments 少量订单金额计算差错,客诉上报",
        inject=_inject_s16,
        root_cause_keywords=["round", "金额", "计算", "bug", "业务"],
        note="业务代码 bug——建议回滚版本,严禁重启掩盖,要求零变更",
        auto_recoverable=False,
    ),
    FaultScenario(
        id="orders-db-conn-limit", category="external",
        alert_service="orders", severity="P1",
        alert_summary="orders 报数据库连接数达上限,写入失败",
        inject=_inject_s17,
        root_cause_keywords=["orders-db", "conn", "连接", "limit", "上限", "外部"],
        note="外部数据库限制——建议 DBA 提额,要求零变更",
        auto_recoverable=False,
    ),
    FaultScenario(
        id="payments-error-spike-upstream", category="external",
        alert_service="payments", severity="P2",
        alert_summary="payments 错误率 6%,上游渠道 5xx 传导",
        inject=_inject_s18,
        root_cause_keywords=["upstream", "上游", "渠道", "provider", "503", "传导"],
        note="传导定位:责任在上游供应商,要求零变更",
        auto_recoverable=False,
    ),
    FaultScenario(
        id="gateway-log-flood", category="capacity",
        alert_service="gateway", severity="P2",
        alert_summary="gateway 磁盘 91%,日志写入速率 20 倍",
        inject=_inject_s19,
        root_cause_keywords=["log", "日志", "disk", "磁盘", "debug"],
        expected_actions=[{"tool": "log_cleanup", "params": {"service": "gateway"}}],
        note="S4 同类不同服务",
    ),
    FaultScenario(
        id="blackfriday-surge", category="capacity",
        alert_service="gateway", severity="P1",
        alert_summary="全链路 p99 抬升,QPS 三倍于基线(大促流量)",
        inject=_inject_s20,
        root_cause_keywords=["traffic", "流量", "promo", "大促", "surge", "qps"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "gateway", "delta": 1}},
            {"tool": "scale_up", "params": {"service": "orders", "delta": 1}},
        ],
        note="多动作编排:入口与订单双扩容",
    ),
]

# id → 剧本 查找表
BY_ID: dict[str, FaultScenario] = {s.id: s for s in SCENARIOS}


# 校验剧本注册表自身一致性(id 动作合法/诊断类零动作/关键词非空)
def validate_registry() -> list[str]:
    problems: list[str] = []
    ids = [s.id for s in SCENARIOS]
    if len(ids) != len(set(ids)):
        problems.append("duplicate scenario ids")
    for s in SCENARIOS:
        for action in s.expected_actions:
            if action["tool"] not in _MUTATION_TOOLS:
                problems.append(f"{s.id}: unknown tool {action['tool']}")
        if not s.auto_recoverable and s.expected_actions:
            problems.append(f"{s.id}: 诊断类剧本不应有期望动作")
        if not s.root_cause_keywords:
            problems.append(f"{s.id}: 缺少 root_cause_keywords")
    return problems
