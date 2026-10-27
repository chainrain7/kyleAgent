# 外部依赖慢/超时(External Dependency)

## 触发特征
- 服务变慢/报错,但自身状态正常,日志指向外部(db/risk/上游渠道)
- 日志:"db query slow"、"too many connections"、外部 5xx、"timeout" 指向 externals 清单内的依赖

## 诊断步骤
1. service_status 确认自身与内部依赖正常
2. logs_query 找外部依赖名(orders-db / risk-svc / provider)
3. 判断责任边界:外部依赖在拓扑 externals 清单中 = 不可操作

## 处置
- 诊断类:不要执行任何变更(重启自身无效且可能掩盖问题)
- 给出建议:联系 DBA/供应商、开降级开关、熔断

## 禁止
- 一切变更工具调用(违反零变更原则)
