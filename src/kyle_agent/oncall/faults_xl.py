"""OnCallCopilot 扩容剧本库(端到端效果评测)——40 个新增演练场景。

难度分层(评测分组依据,顺序即分层):
- A 单故障变体(x01–x22,22 条):复用基础注入原语,换服务/幅度/文案;
- B 误导表象(x23–x32,10 条):告警在 A 服务,真根因在链路下游/外部依赖,
  风险经拓扑(gateway→{orders,payments};orders→orders-db;
  payments→{payments-db,risk-svc})传导;
- C 复合故障(x33–x40,8 条):双故障因果链,期望动作为两组件处置并集。

注入约定(与 faults.py 同款):
- 注入原语 = SimEnv 方法(patch_service/append_metrics/append_log/
  append_alert);_spike/_logs 为同款 sugar,本地定义避免循环导入;
- 可自动恢复剧本:fault_id 用自身 id(恢复动作按注册表期望精确清除);
  复合剧本 fault_id 复用"组件剧本"的已注册 id,两个组件各自被对应
  动作清除(顺序无关),双清后由 _apply_recovery 恢复基线;
- 零变更剧本(诊断类):fault_id = 自身 id,终态断言要求标记保留。
manual_triage_minutes 为 runbook 人工排查参考的估算值(非实测),
口径见 faults.py 模块注释与 metrics.md。
"""
from __future__ import annotations

from typing import Any

from kyle_agent.oncall.faults import FaultScenario, _logs, _spike
from kyle_agent.oncall.sim import SimEnv

# ── 难度层 A:单故障变体(x01–x22) ────────────────────────


# X01 payments 内存泄漏(S1 镜像换服务)
def _inject_x01(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-mem-leak",
                    memory_mb=950, status="degraded")
    _spike(e, "payments", 8, memory_mb=950, cpu_pct=70)
    _logs(e, "payments", [
        ("WARN", "memory usage 93% of limit (950/1024MB), growth ~30MB/10m"),
        ("ERROR", "OOMKilled container restart #2 (exit 137)"),
    ])
    e.append_alert("payments", "P1", "payments 内存 93% 持续上涨,逼近容器上限")


# X02 orders 缓慢内存泄漏(低幅长程,区别于 X01 的陡峭曲线)
def _inject_x02(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-mem-leak-slow",
                    memory_mb=780, status="degraded")
    _spike(e, "orders", 8, memory_mb=780, cpu_pct=63)
    _logs(e, "orders", [
        ("WARN", "memory usage 76% of limit (780/1024MB), slow growth ~40MB/h"),
        ("WARN", "gc pause rising: avg 210ms (baseline 12ms)"),
    ])
    e.append_alert("orders", "P2", "orders 内存缓慢上涨,GC 暂停时间抬升")


# X03 gateway 上游连接池耗尽(区别于 S3 的 db 连接池:池在网关侧)
def _inject_x03(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-connpool-exhaust",
                    status="degraded", error_rate=0.22, p99_ms=780)
    _spike(e, "gateway", 7, error_rate=0.22, p99_ms=780)
    _logs(e, "gateway", [
        ("ERROR", "upstream connection pool exhausted (64/64 in use, timeout 3000ms)"),
        ("ERROR", "502 -> /api/orders (pool acquire timeout)"),
    ])
    e.append_alert("gateway", "P1", "gateway 上游连接池耗尽,大量请求排队")


# X04 orders 数据库连接池耗尽(S3 模式换服务)
def _inject_x04(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-connpool-exhaust",
                    status="degraded", error_rate=0.31, p99_ms=890)
    _spike(e, "orders", 7, error_rate=0.31, p99_ms=890)
    _logs(e, "orders", [
        ("ERROR", "db connection pool exhausted (20/20 in use)"),
        ("ERROR", "order create failed: pool acquire timeout after 5000ms"),
    ])
    e.append_alert("orders", "P1", "orders 数据库连接池耗尽,下单失败率上升")


# X05 gateway 线程池死锁(S13 模式换服务)
def _inject_x05(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-thread-stuck",
                    status="degraded", p99_ms=1050, error_rate=0.12)
    _spike(e, "gateway", 6, p99_ms=1050, error_rate=0.12)
    _logs(e, "gateway", [
        ("ERROR", "thread pool exhausted: 128/128 busy (rejecting tasks)"),
        ("WARN", "deadlock suspected: 2 threads waiting on lock L3"),
    ])
    e.append_alert("gateway", "P1", "gateway 线程池耗尽,请求排队超时")


# X06 payments 坏配置崩溃循环(S2 镜像换服务)
def _inject_x06(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-crashloop-badconfig",
                    status="crashloop", replicas=1, error_rate=0.45)
    _spike(e, "payments", 6, error_rate=0.45, qps=85)
    _logs(e, "payments", [
        ("ERROR", 'config validation failed: unknown key "grpc_timeout_ms"'),
        ("ERROR", "CrashLoopBackOff backoff 5m0s (restart #6, exit 1)"),
    ])
    e.append_alert("payments", "P1", "payments CrashLoopBackOff,6 分钟内重启 6 次")


# X07 orders 环境变量缺失崩溃循环(与 X06 同族不同根因特征)
def _inject_x07(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-crashloop-envvar",
                    status="crashloop", replicas=1, error_rate=0.5)
    _spike(e, "orders", 6, error_rate=0.5, qps=80)
    _logs(e, "orders", [
        ("ERROR", "required env var MQ_BROKER_URL is missing at startup"),
        ("ERROR", "CrashLoopBackOff backoff 5m0s (restart #7, exit 1)"),
    ])
    e.append_alert("orders", "P1", "orders CrashLoopBackOff,启动即退出")


# X08 payments feature flag 键拼写错误(未崩溃,错误率上升)
def _inject_x08(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-config-typo-flag",
                    status="degraded", error_rate=0.09)
    _spike(e, "payments", 6, error_rate=0.09)
    _logs(e, "payments", [
        ("ERROR", 'unknown feature flag key "paymetns_new_flow" - using default'),
        ("WARN", "flag paymetns_new_flow not found in config registry (typo?)"),
    ])
    e.append_alert("payments", "P2", "payments 错误率升至 9%,疑似发布后配置异常")


# X09 gateway 路由配置漂移致 5xx(发布引入,重启即回滚)
def _inject_x09(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-config-drift-route",
                    status="degraded", error_rate=0.15)
    _spike(e, "gateway", 6, error_rate=0.15)
    _logs(e, "gateway", [
        ("ERROR", "route /api/orders/v2 points to stale upstream pool "
                  "(config drift since v1.9.2)"),
        ("ERROR", "502 -> /api/orders/v2 (bad gateway)"),
    ])
    e.append_alert("gateway", "P1", "gateway /v2 路由 5xx 飙升,疑似配置漂移")


# X10 payments 磁盘写满(S4 同类换服务)
def _inject_x10(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-disk-full",
                    disk_pct=94, status="degraded")
    _spike(e, "payments", 5, disk_pct=94)
    _logs(e, "payments", [
        ("ERROR", "write /var/log/payments/app.log: no space left on device"),
        ("WARN", "disk usage 94% (threshold 90%)"),
    ])
    e.append_alert("payments", "P2", "payments 磁盘 94%,写入受限")


# X11 gateway 磁盘写满(S4 同类换服务,幅度略低)
def _inject_x11(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-disk-full",
                    disk_pct=90, status="degraded")
    _spike(e, "gateway", 5, disk_pct=90)
    _logs(e, "gateway", [
        ("ERROR", "write /var/log/gateway/access.log: no space left on device"),
        ("WARN", "disk usage 90% (threshold 90%)"),
    ])
    e.append_alert("gateway", "P2", "gateway 磁盘 90%,写入受限")


# X12 orders CPU 打满(S5 模式换服务)
def _inject_x12(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-cpu-saturation",
                    cpu_pct=95, qps=950, p99_ms=420, status="degraded")
    _spike(e, "orders", 6, cpu_pct=95, qps=950, p99_ms=420)
    _logs(e, "orders", [
        ("WARN", "cpu 95% sustained 6m (limit 2 cores)"),
        ("WARN", "p99 latency 420ms (baseline 130ms)"),
    ])
    e.append_alert("orders", "P2", "orders CPU 95%,处理延迟上升")


# X13 payments CPU 打满(S5 模式换服务)
def _inject_x13(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-cpu-saturation",
                    cpu_pct=96, qps=780, p99_ms=460, status="degraded")
    _spike(e, "payments", 6, cpu_pct=96, qps=780, p99_ms=460)
    _logs(e, "payments", [
        ("WARN", "cpu 96% sustained 6m (limit 2 cores)"),
        ("WARN", "thread starvation: 150ms queue delay at intake"),
    ])
    e.append_alert("payments", "P2", "payments CPU 96%,处理延迟上升")


# X14 gateway 出口队列积压(S6 模式换服务)
def _inject_x14(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-queue-backlog",
                    queue_lag=45000, status="degraded")
    _spike(e, "gateway", 7, queue_lag=45000)
    _logs(e, "gateway", [
        ("WARN", "egress queue backlog 45000 (workers 8/8 busy)"),
        ("WARN", "flush rate 120/s < ingest rate 260/s"),
    ])
    e.append_alert("gateway", "P2", "gateway 出口队列积压 4.5 万条")


# X15 payments 结算消费积压 8 万(S6 同类换服务换幅度)
def _inject_x15(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-queue-backlog",
                    queue_lag=80000, status="degraded")
    _spike(e, "payments", 7, queue_lag=80000)
    _logs(e, "payments", [
        ("WARN", "consumer lag 80000 msgs (topic payments-settle)"),
        ("WARN", "consume rate 60/s < produce rate 180/s"),
    ])
    e.append_alert("payments", "P2", "payments 结算消费积压 8 万条")


# X16 orders 日志洪水致磁盘 93%(S19 同类换服务)
def _inject_x16(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-log-flood",
                    disk_pct=93, status="degraded")
    _spike(e, "orders", 5, disk_pct=93)
    _logs(e, "orders", [
        ("WARN", "log write rate 18x baseline (debug logging after v2.1.0?)"),
        ("WARN", "disk usage 93% (threshold 90%)"),
    ])
    e.append_alert("orders", "P2", "orders 磁盘 93%,日志写入速率 18 倍")


# X17 payments 遭遇恶意流量(S15 模式换服务,QPS 八倍)
def _inject_x17(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-dos-traffic",
                    status="degraded", qps=4000, cpu_pct=93, p99_ms=520)
    _spike(e, "payments", 5, qps=4000, cpu_pct=93, p99_ms=520)
    _logs(e, "payments", [
        ("WARN", "qps 4000 (baseline 500), 85% from UA 'refund-bot'"),
        ("WARN", "rate-limit threshold exceeded, shedding 25% traffic"),
    ])
    e.append_alert("payments", "P1", "payments 遭遇异常流量,QPS 八倍于基线")


# X18 orders 营销批任务打穿缓存(S14 变体:诱因是批任务)
def _inject_x18(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-cache-storm-marketing",
                    status="degraded", db_qps=520, cache_hit_pct=15, p99_ms=390)
    _spike(e, "orders", 6, db_qps=520, cache_hit_pct=15, p99_ms=390)
    _logs(e, "orders", [
        ("WARN", "cache hit ratio dropped to 15% (baseline 99%)"),
        ("WARN", "promo batch job 'flash-sale-warmup' scanning full catalog"),
    ])
    e.append_alert("orders", "P2", "orders 缓存命中率骤降,营销批任务打穿缓存")


# X19 payments 缓存失效风暴(S14 同类换服务:批量过期)
def _inject_x19(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-cache-storm",
                    status="degraded", db_qps=610, cache_hit_pct=10, p99_ms=430)
    _spike(e, "payments", 6, db_qps=610, cache_hit_pct=10, p99_ms=430)
    _logs(e, "payments", [
        ("WARN", "mass key eviction: 40% of hot keys expired simultaneously"),
        ("WARN", "db qps 610 (baseline 70)"),
    ])
    e.append_alert("payments", "P2", "payments 缓存失效风暴,数据库 QPS 涨 8 倍")


# X20 gateway TLS 证书过期(S11 同类换服务,需人工)
def _inject_x20(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-cert-expired",
                    status="degraded", error_rate=0.17)
    _spike(e, "gateway", 5, error_rate=0.17)
    _logs(e, "gateway", [
        ("ERROR", "tls: failed to verify certificate: certificate has expired "
                  "(gateway-ingress)"),
        ("ERROR", "handshake failed with external partners (x509)"),
    ])
    e.append_alert("gateway", "P1", "gateway TLS 证书过期,对外握手失败")


# X21 orders 税费计算 bug(S16 同类换服务,零变更)
def _inject_x21(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-business-tax-calc")
    _logs(e, "orders", [
        ("ERROR", "tax calculation mismatch: applied rate 0.13 expected 0.06 "
                  "(order 66201)"),
        ("WARN", "214 orders overcharged in region south since 09:40"),
    ])
    e.append_alert("orders", "P2", "orders 税费计算差错,客诉上报")


# X22 payments 对账批处理金额 bug(业务逻辑缺陷,零变更)
def _inject_x22(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-settlement-batch-bug")
    _logs(e, "payments", [
        ("ERROR", "settlement reconciliation drift: -3120.50 across 44 orders "
                  "(batch #1207)"),
        ("WARN", "batch job settlement-nightly produced inconsistent totals"),
    ])
    e.append_alert("payments", "P2", "payments 夜间对账批处理金额不符")


# ── 难度层 B:误导表象(x23–x32) ──────────────────────────


# X23 gateway 间歇 5xx,真根因 orders 反复重启抖动(传导定位)
def _inject_x23(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="gateway-dep-orders-flapping",
                    status="degraded", qps=60)
    e.patch_service("gateway", status="degraded", error_rate=0.14)
    _spike(e, "orders", 6, qps=60)
    _spike(e, "gateway", 6, error_rate=0.14)
    _logs(e, "orders", [
        ("WARN", "liveness probe failed, restarting container (restart #4 in 10m)"),
        ("WARN", "readiness gate flapping: ready 40s then not-ready"),
    ])
    _logs(e, "gateway", [
        ("ERROR", "upstream orders intermittent connection reset (flapping)"),
        ("ERROR", "502 Bad Gateway -> /api/orders (retry 2 of 3)"),
    ])
    e.append_alert("gateway", "P1", "gateway 间歇性 5xx,orders 上游反复重连")


# X24 orders 5xx,真根因 orders-db 死锁(外部数据库,零变更)
def _inject_x24(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-5xx-db-deadlock",
                    status="degraded", error_rate=0.25, p99_ms=900)
    _spike(e, "orders", 6, error_rate=0.25, p99_ms=900)
    _logs(e, "orders", [
        ("ERROR", "orders-db: deadlock detected on table order_items "
                  "(victim tx 88121)"),
        ("ERROR", "insert failed: deadlock retry exhausted"),
    ])
    e.append_alert("orders", "P1", "orders 5xx 飙升,写入失败率 25%")


# X25 orders 写入失败,真根因 orders-db 磁盘写满(外部,零变更)
def _inject_x25(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-errors-db-diskfull",
                    status="degraded", error_rate=0.18)
    _spike(e, "orders", 6, error_rate=0.18)
    _logs(e, "orders", [
        ("ERROR", "insert failed: orders-db reports no space left on device"),
        ("WARN", "orders-db disk 99% (from upstream error payload)"),
    ])
    e.append_alert("orders", "P2", "orders 写入失败率上升,批量请求被拒")


# X26 gateway p99 抬升,真根因 risk-svc 延迟经 payments 传导(两级链路)
def _inject_x26(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="gateway-p99-risk-svc-latency",
                    status="degraded", p99_ms=850, error_rate=0.05)
    e.patch_service("gateway", status="degraded", p99_ms=610)
    _spike(e, "payments", 7, p99_ms=850, error_rate=0.05)
    _spike(e, "gateway", 6, p99_ms=610)
    _logs(e, "payments", [
        ("ERROR", "risk-svc call latency 1800ms (40% of calls)"),
        ("WARN", "payment flow slowed by risk check stage"),
    ])
    _logs(e, "gateway", [
        ("WARN", "upstream payments p99 850ms -> end-to-end p99 610ms"),
    ])
    e.append_alert("gateway", "P2", "gateway 全链路 p99 抬升,支付段为主要延迟源")


# X27 payments 延迟尖刺,真根因 payments-db 新上线慢 SQL(外部,零变更)
def _inject_x27(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-latency-db-slowquery",
                    status="degraded", p99_ms=980)
    _spike(e, "payments", 7, p99_ms=980)
    _logs(e, "payments", [
        ("WARN", "db query slow (>2400ms): SELECT * FROM payments WHERE ... "
                  "(since migration v2.3)"),
        ("WARN", "payments-db slow query count rising since deploy"),
    ])
    e.append_alert("payments", "P2", "payments 延迟尖刺,p99 达 980ms")


# X28 payments 超时,真根因 payments-db 磁盘 IO 饱和(外部,零变更)
def _inject_x28(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-timeout-db-io",
                    status="degraded", error_rate=0.14, p99_ms=1200)
    _spike(e, "payments", 6, error_rate=0.14, p99_ms=1200)
    _logs(e, "payments", [
        ("ERROR", "payment commit timeout after 3000ms (db iowait 45%)"),
        ("WARN", "payments-db disk io utilization 98%"),
    ])
    e.append_alert("payments", "P1", "payments 大面积超时,p99 达 1.2 秒")


# X29 payments 失败率预警,真根因证书 7 天后过期(监控预警,零变更)
def _inject_x29(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-cert-renew-window",
                    status="degraded", error_rate=0.04)
    _spike(e, "payments", 5, error_rate=0.04)
    _logs(e, "payments", [
        ("WARN", "certificate payments-channel.example.com expires in 7 days"),
        ("WARN", "monitor cert-expiry-warning: renewal window open"),
    ])
    e.append_alert("payments", "P2", "payments 渠道证书 7 天后过期(预警)")


# X30 orders 写入失败,真根因上游字段 schema 变更(业务对齐问题,零变更)
def _inject_x30(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-schema-mismatch",
                    status="degraded", error_rate=0.21)
    _spike(e, "orders", 6, error_rate=0.21)
    _logs(e, "orders", [
        ("ERROR", "schema validation failed: field 'user_phone' changed type "
                  "(string -> object) in upstream payload"),
        ("ERROR", "order create rejected: schema mismatch (42 rejects in 10m)"),
    ])
    e.append_alert("orders", "P2", "orders 写入失败率上升,请求被批量拒绝")


# X31 gateway 延迟,真根因 payments 线程池吃紧但未宕(依赖劣化,可重启)
def _inject_x31(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="gateway-dep-payments-slow",
                    status="degraded", p99_ms=1150, error_rate=0.08)
    e.patch_service("gateway", status="degraded", p99_ms=720)
    _spike(e, "payments", 6, p99_ms=1150, error_rate=0.08)
    _spike(e, "gateway", 6, p99_ms=720)
    _logs(e, "payments", [
        ("ERROR", "thread pool 188/200 busy (pool starvation, service up)"),
        ("WARN", "queue delay 900ms at worker intake"),
    ])
    _logs(e, "gateway", [
        ("WARN", "upstream payments latency elevated (service not down)"),
    ])
    e.append_alert("gateway", "P2", "gateway 延迟上升,payments 段耗时异常")


# X32 payments 失败率上升,真根因 risk-svc 侧证书被吊销(外部,零变更)
def _inject_x32(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-risk-cert-chain",
                    status="degraded", error_rate=0.19)
    _spike(e, "payments", 6, error_rate=0.19)
    _logs(e, "payments", [
        ("ERROR", "risk-svc handshake failed: certificate revoked (OCSP check)"),
        ("WARN", "risk check failing for 19% of transactions"),
    ])
    e.append_alert("payments", "P1", "payments 交易失败率上升,风控调用异常")


# ── 难度层 C:复合故障(x33–x40) ──────────────────────────


# X33 orders 磁盘满→进程反复崩溃(因果链,清盘+重启双动作)
def _inject_x33(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-disk-full",
                    disk_pct=96, status="degraded")
    e.patch_service("orders", fault_id="orders-crashloop-badconfig",
                    status="crashloop", replicas=1, error_rate=0.38)
    _spike(e, "orders", 6, disk_pct=96, error_rate=0.38)
    _logs(e, "orders", [
        ("ERROR", "write /var/log/orders/app.log: no space left on device"),
        ("ERROR", "process terminated: worker unable to write tmp state (exit 1)"),
        ("WARN", "CrashLoopBackOff backoff 5m0s (restart #3)"),
    ])
    e.append_alert("orders", "P1", "orders 磁盘写满且进程反复崩溃")


# X34 大促流量叠加网关内存泄漏(双因,扩容+重启双动作)
def _inject_x34(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-mem-leak",
                    memory_mb=945, status="degraded", qps=1600, cpu_pct=90,
                    p99_ms=410)
    _spike(e, "gateway", 6, qps=1600, cpu_pct=90, memory_mb=945, p99_ms=410)
    _logs(e, "gateway", [
        ("WARN", "qps 1600 (3x baseline), promo traffic pattern detected"),
        ("WARN", "memory usage 92% of limit (945/1024MB)"),
        ("ERROR", "OOMKilled container restart #1 (exit 137)"),
    ])
    e.append_alert("gateway", "P1", "大促流量叠加网关内存持续上涨,p99 与内存双高")


# X35 orders-db 慢查询拖慢消费致积压(止血扩容,根因在外部)
def _inject_x35(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-queue-backlog",
                    queue_lag=130000, status="degraded", p99_ms=870)
    _spike(e, "orders", 7, queue_lag=130000, p99_ms=870)
    _logs(e, "orders", [
        ("WARN", "db query slow (>2000ms): SELECT ... FOR UPDATE (orders-db)"),
        ("WARN", "consumer lag 130000 rising (consume 45/s < produce 200/s)"),
    ])
    e.append_alert("orders", "P2", "orders 消费积压 13 万条,处理延迟上升")


# X36 双告警:payments 证书过期(不可处置)+ orders 磁盘满(可处置)
def _inject_x36(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-cert-expired",
                    status="degraded", error_rate=0.15)
    e.patch_service("orders", fault_id="orders-disk-full",
                    disk_pct=95, status="degraded")
    _spike(e, "payments", 5, error_rate=0.15)
    _spike(e, "orders", 5, disk_pct=95)
    _logs(e, "payments", [
        ("ERROR", "tls: failed to verify certificate: certificate has expired"),
        ("ERROR", "channel callback handshake failed (x509)"),
    ])
    _logs(e, "orders", [
        ("ERROR", "write /var/log/orders/app.log: no space left on device"),
        ("WARN", "disk usage 95% (threshold 90%)"),
    ])
    e.append_alert("payments", "P1", "payments TLS 握手失败,渠道回调中断")
    e.append_alert("orders", "P1", "orders 磁盘使用 95%,写入受限")


# X37 payments 线程池卡死叠加异常流量(重启+扩容双动作)
def _inject_x37(e: SimEnv) -> None:
    e.patch_service("payments", fault_id="payments-thread-stuck",
                    status="degraded", p99_ms=1150, error_rate=0.13,
                    qps=4200, cpu_pct=93)
    _spike(e, "payments", 6, p99_ms=1150, error_rate=0.13, qps=4200, cpu_pct=93)
    _logs(e, "payments", [
        ("ERROR", "thread pool exhausted: 200/200 busy (rejecting tasks)"),
        ("WARN", "qps 4200 (baseline 500), 80% from UA 'refund-bot'"),
    ])
    e.append_alert("payments", "P1", "payments 线程池耗尽叠加异常流量")


# X38 orders 配置键错误叠加 CPU 飙升(重启+扩容双动作)
def _inject_x38(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-config-typo-errors",
                    status="degraded", error_rate=0.09, cpu_pct=95, qps=800)
    _spike(e, "orders", 6, error_rate=0.09, cpu_pct=95, qps=800)
    _logs(e, "orders", [
        ("ERROR", 'unknown config key "retr_interval" - using default'),
        ("ERROR", "order create failed: KeyError 'retr_interval'"),
        ("WARN", "cpu 95% sustained 6m (limit 2 cores)"),
    ])
    e.append_alert("orders", "P2", "orders 错误率上升叠加 CPU 飙升")


# X39 gateway 日志洪水叠加连接池泄漏(清盘+重启双动作)
def _inject_x39(e: SimEnv) -> None:
    e.patch_service("gateway", fault_id="gateway-log-flood",
                    disk_pct=92, status="degraded")
    e.patch_service("gateway", fault_id="gateway-connpool-exhaust",
                    error_rate=0.16, p99_ms=800)
    _spike(e, "gateway", 6, disk_pct=92, error_rate=0.16, p99_ms=800)
    _logs(e, "gateway", [
        ("WARN", "log write rate 20x baseline (debug logging enabled?)"),
        ("ERROR", "upstream connection pool exhausted (64/64 in use)"),
        ("WARN", "disk usage 92% (threshold 90%)"),
    ])
    e.append_alert("gateway", "P2", "gateway 日志写入速率异常叠加连接池获取失败")


# X40 orders 消费积压叠加内存缓慢泄漏(扩容+重启双动作)
def _inject_x40(e: SimEnv) -> None:
    e.patch_service("orders", fault_id="orders-queue-backlog",
                    queue_lag=110000, status="degraded")
    e.patch_service("orders", fault_id="orders-mem-leak-slow",
                    memory_mb=800, cpu_pct=66)
    _spike(e, "orders", 7, queue_lag=110000, memory_mb=800, cpu_pct=66)
    _logs(e, "orders", [
        ("WARN", "consumer lag 110000 msgs (partition orders-0)"),
        ("WARN", "memory usage 78% of limit and climbing (~40MB/h)"),
    ])
    e.append_alert("orders", "P2", "orders 消费积压叠加内存持续上涨")


# ── 扩容剧本注册表(由 faults.py 装配进全局注册表) ─────────
# 顺序即难度层:A 变体(x01–x22)→ B 误导(x23–x32)→ C 复合(x33–x40);
# manual_triage_minutes 为估算参考值(口径见模块注释),取值按任务书 §4 终值。

SCENARIOS_XL: list[FaultScenario] = [
    # ── A 单故障变体 ──
    FaultScenario(
        id="payments-mem-leak", category="leak",
        alert_service="payments", severity="P1",
        alert_summary="payments 内存 93% 持续上涨,逼近容器上限",
        inject=_inject_x01,
        root_cause_keywords=["memory", "leak", "内存泄漏", "oom"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        manual_triage_minutes=35,
        note="S1 镜像:基础查证→单动作重启",
    ),
    FaultScenario(
        id="orders-mem-leak-slow", category="leak",
        alert_service="orders", severity="P2",
        alert_summary="orders 内存缓慢上涨,GC 暂停时间抬升",
        inject=_inject_x02,
        root_cause_keywords=["memory", "leak", "内存泄漏", "缓慢"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        manual_triage_minutes=30,
        note="低幅长程泄漏,GC 特征定位",
    ),
    FaultScenario(
        id="gateway-connpool-exhaust", category="leak",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 上游连接池耗尽,大量请求排队",
        inject=_inject_x03,
        root_cause_keywords=["conn", "pool", "连接池"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "gateway"}}],
        manual_triage_minutes=30,
        note="网关侧连接池,S3 的对照变体",
    ),
    FaultScenario(
        id="orders-connpool-exhaust", category="leak",
        alert_service="orders", severity="P1",
        alert_summary="orders 数据库连接池耗尽,下单失败率上升",
        inject=_inject_x04,
        root_cause_keywords=["conn", "pool", "连接池"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        manual_triage_minutes=30,
        note="S3 同类换服务",
    ),
    FaultScenario(
        id="gateway-thread-stuck", category="leak",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 线程池耗尽,请求排队超时",
        inject=_inject_x05,
        root_cause_keywords=["thread", "线程", "deadlock", "死锁", "stuck"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "gateway"}}],
        manual_triage_minutes=30,
        note="S13 同类换服务",
    ),
    FaultScenario(
        id="payments-crashloop-badconfig", category="crash",
        alert_service="payments", severity="P1",
        alert_summary="payments CrashLoopBackOff,6 分钟内重启 6 次",
        inject=_inject_x06,
        root_cause_keywords=["config", "配置", "bad", "crashloop"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        manual_triage_minutes=20,
        note="S2 镜像:坏配置崩溃循环",
    ),
    FaultScenario(
        id="orders-crashloop-envvar", category="crash",
        alert_service="orders", severity="P1",
        alert_summary="orders CrashLoopBackOff,启动即退出",
        inject=_inject_x07,
        root_cause_keywords=["env", "环境变量", "missing", "crashloop", "缺失"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        manual_triage_minutes=20,
        note="与 X06 同族不同特征:环境变量缺失",
    ),
    FaultScenario(
        id="payments-config-typo-flag", category="crash",
        alert_service="payments", severity="P2",
        alert_summary="payments 错误率升至 9%,疑似发布后配置异常",
        inject=_inject_x08,
        root_cause_keywords=["config", "flag", "配置", "typo", "拼写"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        manual_triage_minutes=15,
        note="S12 同类:flag 键拼写错误,进程未崩",
    ),
    FaultScenario(
        id="gateway-config-drift-route", category="crash",
        alert_service="gateway", severity="P1",
        alert_summary="gateway /v2 路由 5xx 飙升,疑似配置漂移",
        inject=_inject_x09,
        root_cause_keywords=["config", "route", "路由", "drift", "漂移"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "gateway"}}],
        manual_triage_minutes=25,
        note="配置漂移类,重启回滚",
    ),
    FaultScenario(
        id="payments-disk-full", category="capacity",
        alert_service="payments", severity="P2",
        alert_summary="payments 磁盘 94%,写入受限",
        inject=_inject_x10,
        root_cause_keywords=["disk", "磁盘", "log", "空间"],
        expected_actions=[{"tool": "log_cleanup", "params": {"service": "payments"}}],
        manual_triage_minutes=20,
        note="S4 同类换服务:清日志",
    ),
    FaultScenario(
        id="gateway-disk-full", category="capacity",
        alert_service="gateway", severity="P2",
        alert_summary="gateway 磁盘 90%,写入受限",
        inject=_inject_x11,
        root_cause_keywords=["disk", "磁盘", "log", "空间"],
        expected_actions=[{"tool": "log_cleanup", "params": {"service": "gateway"}}],
        manual_triage_minutes=20,
        note="S4 同类换服务",
    ),
    FaultScenario(
        id="orders-cpu-saturation", category="capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders CPU 95%,处理延迟上升",
        inject=_inject_x12,
        root_cause_keywords=["cpu", "traffic", "流量", "qps", "容量"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "orders", "delta": 1}}],
        manual_triage_minutes=20,
        note="S5 同类换服务:扩容",
    ),
    FaultScenario(
        id="payments-cpu-saturation", category="capacity",
        alert_service="payments", severity="P2",
        alert_summary="payments CPU 96%,处理延迟上升",
        inject=_inject_x13,
        root_cause_keywords=["cpu", "traffic", "流量", "qps", "容量"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "payments", "delta": 1}}],
        manual_triage_minutes=20,
        note="S5 同类换服务",
    ),
    FaultScenario(
        id="gateway-queue-backlog", category="capacity",
        alert_service="gateway", severity="P2",
        alert_summary="gateway 出口队列积压 4.5 万条",
        inject=_inject_x14,
        root_cause_keywords=["lag", "backlog", "queue", "积压", "队列"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "gateway", "delta": 1}}],
        manual_triage_minutes=25,
        note="S6 同类换服务:出口队列",
    ),
    FaultScenario(
        id="payments-queue-backlog", category="capacity",
        alert_service="payments", severity="P2",
        alert_summary="payments 结算消费积压 8 万条",
        inject=_inject_x15,
        root_cause_keywords=["lag", "backlog", "consumer", "积压", "消费"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "payments", "delta": 1}}],
        manual_triage_minutes=25,
        note="S6 同类换服务换幅度",
    ),
    FaultScenario(
        id="orders-log-flood", category="capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 磁盘 93%,日志写入速率 18 倍",
        inject=_inject_x16,
        root_cause_keywords=["log", "日志", "disk", "磁盘", "debug"],
        expected_actions=[{"tool": "log_cleanup", "params": {"service": "orders"}}],
        manual_triage_minutes=20,
        note="S19 同类换服务",
    ),
    FaultScenario(
        id="payments-dos-traffic", category="capacity",
        alert_service="payments", severity="P1",
        alert_summary="payments 遭遇异常流量,QPS 八倍于基线",
        inject=_inject_x17,
        root_cause_keywords=["dos", "traffic", "流量", "bot", "恶意", "attack"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "payments", "delta": 2}}],
        manual_triage_minutes=30,
        note="S15 同类换服务:+2 扩容缓解",
    ),
    FaultScenario(
        id="orders-cache-storm-marketing", category="capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 缓存命中率骤降,营销批任务打穿缓存",
        inject=_inject_x18,
        root_cause_keywords=["cache", "缓存", "miss", "营销", "promo"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "orders", "delta": 1}}],
        manual_triage_minutes=25,
        note="S14 变体:诱因是营销批任务",
    ),
    FaultScenario(
        id="payments-cache-storm", category="capacity",
        alert_service="payments", severity="P2",
        alert_summary="payments 缓存失效风暴,数据库 QPS 涨 8 倍",
        inject=_inject_x19,
        root_cause_keywords=["cache", "缓存", "miss", "失效", "evict"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "payments", "delta": 1}}],
        manual_triage_minutes=25,
        note="S14 同类换服务:批量过期",
    ),
    FaultScenario(
        id="gateway-cert-expired", category="external",
        alert_service="gateway", severity="P1",
        alert_summary="gateway TLS 证书过期,对外握手失败",
        inject=_inject_x20,
        root_cause_keywords=["cert", "证书", "tls", "expired", "过期"],
        note="S11 同类换服务:需人工续期,零变更",
        auto_recoverable=False,
        manual_triage_minutes=10,
    ),
    FaultScenario(
        id="orders-business-tax-calc", category="business",
        alert_service="orders", severity="P2",
        alert_summary="orders 税费计算差错,客诉上报",
        inject=_inject_x21,
        root_cause_keywords=["tax", "税", "计算", "bug", "业务"],
        note="S16 同类换服务:业务代码 bug,零变更",
        auto_recoverable=False,
        manual_triage_minutes=40,
    ),
    FaultScenario(
        id="payments-settlement-batch-bug", category="business",
        alert_service="payments", severity="P2",
        alert_summary="payments 夜间对账批处理金额不符",
        inject=_inject_x22,
        root_cause_keywords=["settlement", "对账", "批处理", "金额", "bug"],
        note="批处理对账逻辑缺陷,零变更",
        auto_recoverable=False,
        manual_triage_minutes=40,
    ),
    # ── B 误导表象 ──
    FaultScenario(
        id="gateway-dep-orders-flapping", category="dependency",
        alert_service="gateway", severity="P1",
        alert_summary="gateway 间歇性 5xx,orders 上游反复重连",
        inject=_inject_x23,
        root_cause_keywords=["orders", "flapping", "重启", "抖动", "upstream"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "orders"}}],
        manual_triage_minutes=40,
        note="表象在 gateway,根因是 orders 重启抖动(未宕)",
    ),
    FaultScenario(
        id="orders-5xx-db-deadlock", category="external",
        alert_service="orders", severity="P1",
        alert_summary="orders 5xx 飙升,写入失败率 25%",
        inject=_inject_x24,
        root_cause_keywords=["orders-db", "deadlock", "死锁", "数据库", "db"],
        note="根因在 orders-db 死锁,DBA 处理,零变更",
        auto_recoverable=False,
        manual_triage_minutes=40,
    ),
    FaultScenario(
        id="orders-errors-db-diskfull", category="external",
        alert_service="orders", severity="P2",
        alert_summary="orders 写入失败率上升,批量请求被拒",
        inject=_inject_x25,
        root_cause_keywords=["orders-db", "disk", "磁盘", "满", "full"],
        note="磁盘满在外部库,本服务清盘无效,零变更",
        auto_recoverable=False,
        manual_triage_minutes=35,
    ),
    FaultScenario(
        id="gateway-p99-risk-svc-latency", category="external",
        alert_service="gateway", severity="P2",
        alert_summary="gateway 全链路 p99 抬升,支付段为主要延迟源",
        inject=_inject_x26,
        root_cause_keywords=["risk", "风控", "risk-svc", "延迟", "latency"],
        note="两级传导:gateway←payments←risk-svc,零变更",
        auto_recoverable=False,
        manual_triage_minutes=35,
    ),
    FaultScenario(
        id="payments-latency-db-slowquery", category="external",
        alert_service="payments", severity="P2",
        alert_summary="payments 延迟尖刺,p99 达 980ms",
        inject=_inject_x27,
        root_cause_keywords=["payments-db", "slow", "慢查询", "sql", "db"],
        note="新上线慢 SQL 在外部库,零变更",
        auto_recoverable=False,
        manual_triage_minutes=35,
    ),
    FaultScenario(
        id="payments-timeout-db-io", category="external",
        alert_service="payments", severity="P1",
        alert_summary="payments 大面积超时,p99 达 1.2 秒",
        inject=_inject_x28,
        root_cause_keywords=["payments-db", "io", "iowait", "磁盘", "饱和"],
        note="IO 饱和在外部库,零变更",
        auto_recoverable=False,
        manual_triage_minutes=30,
    ),
    FaultScenario(
        id="payments-cert-renew-window", category="external",
        alert_service="payments", severity="P2",
        alert_summary="payments 渠道证书 7 天后过期(预警)",
        inject=_inject_x29,
        root_cause_keywords=["cert", "证书", "expire", "过期", "renew", "续期"],
        note="预防性预警:尚未故障,安排续期窗口,零变更",
        auto_recoverable=False,
        manual_triage_minutes=15,
    ),
    FaultScenario(
        id="orders-schema-mismatch", category="business",
        alert_service="orders", severity="P2",
        alert_summary="orders 写入失败率上升,请求被批量拒绝",
        inject=_inject_x30,
        root_cause_keywords=["schema", "字段", "上游", "变更", "mismatch"],
        note="上游接口契约变更,需协同对齐,零变更",
        auto_recoverable=False,
        manual_triage_minutes=40,
    ),
    FaultScenario(
        id="gateway-dep-payments-slow", category="dependency",
        alert_service="gateway", severity="P2",
        alert_summary="gateway 延迟上升,payments 段耗时异常",
        inject=_inject_x31,
        root_cause_keywords=["payments", "thread", "线程", "池", "upstream"],
        expected_actions=[{"tool": "service_restart", "params": {"service": "payments"}}],
        manual_triage_minutes=40,
        note="依赖劣化(未宕):restart payments 恢复",
    ),
    FaultScenario(
        id="payments-risk-cert-chain", category="external",
        alert_service="payments", severity="P1",
        alert_summary="payments 交易失败率上升,风控调用异常",
        inject=_inject_x32,
        root_cause_keywords=["cert", "证书", "revoked", "吊销", "risk"],
        note="证书被吊销在 risk-svc 侧,零变更",
        auto_recoverable=False,
        manual_triage_minutes=35,
    ),
    # ── C 复合故障 ──
    FaultScenario(
        id="composite-orders-disk-crashloop", category="capacity+crash",
        alert_service="orders", severity="P1",
        alert_summary="orders 磁盘写满且进程反复崩溃",
        inject=_inject_x33,
        root_cause_keywords=["disk", "磁盘", "crash", "崩溃", "空间"],
        expected_actions=[
            {"tool": "log_cleanup", "params": {"service": "orders"}},
            {"tool": "service_restart", "params": {"service": "orders"}},
        ],
        manual_triage_minutes=40,
        note="因果链:磁盘满→进程崩;清盘+重启双动作",
    ),
    FaultScenario(
        id="composite-gateway-surge-memleak", category="capacity+leak",
        alert_service="gateway", severity="P1",
        alert_summary="大促流量叠加网关内存持续上涨,p99 与内存双高",
        inject=_inject_x34,
        root_cause_keywords=["memory", "内存", "leak", "泄漏", "traffic", "流量"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "gateway", "delta": 1}},
            {"tool": "service_restart", "params": {"service": "gateway"}},
        ],
        manual_triage_minutes=50,
        note="双因并行:扩容扛流量+重启止泄漏",
    ),
    FaultScenario(
        id="composite-orders-db-slow-backlog", category="external+capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 消费积压 13 万条,处理延迟上升",
        inject=_inject_x35,
        root_cause_keywords=["orders-db", "slow", "慢", "积压", "lag"],
        expected_actions=[{"tool": "scale_up", "params": {"service": "orders", "delta": 1}}],
        manual_triage_minutes=40,
        note="止血扩容在本服务,根因在外部库",
    ),
    FaultScenario(
        id="composite-payments-cert-orders-disk", category="external+capacity",
        alert_service="payments", severity="P1",
        alert_summary="payments TLS 握手失败与 orders 磁盘 95% 双告警",
        inject=_inject_x36,
        root_cause_keywords=["disk", "磁盘", "log", "空间"],
        expected_actions=[{"tool": "log_cleanup", "params": {"service": "orders"}}],
        manual_triage_minutes=50,
        note="根因主=可处置项(orders 清盘),证书项进候选待人工",
    ),
    FaultScenario(
        id="composite-payments-thread-dos", category="leak+capacity",
        alert_service="payments", severity="P1",
        alert_summary="payments 线程池耗尽叠加异常流量",
        inject=_inject_x37,
        root_cause_keywords=["thread", "线程", "dos", "流量", "恶意"],
        expected_actions=[
            {"tool": "service_restart", "params": {"service": "payments"}},
            {"tool": "scale_up", "params": {"service": "payments", "delta": 1}},
        ],
        manual_triage_minutes=50,
        note="线程卡死+流量冲击:重启+扩容双动作",
    ),
    FaultScenario(
        id="composite-orders-typo-cpu", category="crash+capacity",
        alert_service="orders", severity="P2",
        alert_summary="orders 错误率上升叠加 CPU 飙升",
        inject=_inject_x38,
        root_cause_keywords=["config", "配置", "typo", "cpu", "容量"],
        expected_actions=[
            {"tool": "service_restart", "params": {"service": "orders"}},
            {"tool": "scale_up", "params": {"service": "orders", "delta": 1}},
        ],
        manual_triage_minutes=35,
        note="配置错+CPU 高:重启+扩容双动作",
    ),
    FaultScenario(
        id="composite-gateway-logflood-connpool", category="capacity+leak",
        alert_service="gateway", severity="P2",
        alert_summary="gateway 日志写入速率异常叠加连接池获取失败",
        inject=_inject_x39,
        root_cause_keywords=["log", "日志", "conn", "pool", "连接池"],
        expected_actions=[
            {"tool": "log_cleanup", "params": {"service": "gateway"}},
            {"tool": "service_restart", "params": {"service": "gateway"}},
        ],
        manual_triage_minutes=40,
        note="日志洪水+连接池泄漏:清盘+重启双动作",
    ),
    FaultScenario(
        id="composite-orders-backlog-memleak", category="capacity+leak",
        alert_service="orders", severity="P2",
        alert_summary="orders 消费积压叠加内存持续上涨",
        inject=_inject_x40,
        root_cause_keywords=["lag", "积压", "mem", "内存", "leak"],
        expected_actions=[
            {"tool": "scale_up", "params": {"service": "orders", "delta": 1}},
            {"tool": "service_restart", "params": {"service": "orders"}},
        ],
        manual_triage_minutes=40,
        note="积压+泄漏:扩容+重启双动作",
    ),
]

# 供类型检查的显式运行时引用(Any:仅确保导入面完整)
_RUNTIME_REF: list[Any] = [SimEnv]
