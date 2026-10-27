# 依赖服务宕机(Dependency Down)

## 触发特征
- 告警在调用方(如 gateway 5xx/504 飙升),但根因常在下游
- 日志:"upstream X unreachable / timeout / refused"
- 关键信号:下游服务 qps 归零或状态 down

## 诊断步骤
1. 不要被告警服务带偏:先 service_status 看全拓扑
2. 找出状态 down / qps=0 的服务——那才是根因
3. logs_query 在告警服务里确认 upstream 指向

## 处置
- service_restart 宕机的下游服务(不是告警方!)
- 下游恢复后,调用方的传导性 degraded 自动解除

## 禁止
- 不要重启/扩容告警方(它没问题,重启反而扩大影响面)
