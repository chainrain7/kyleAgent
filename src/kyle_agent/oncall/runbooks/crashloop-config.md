# 崩溃循环与坏配置(CrashLoop / Bad Config)

## 触发特征
- 状态:crashloop,副本数下降,错误率高
- 日志:"config validation failed"、"unknown key"、"CrashLoopBackOff"
- 常伴随发布窗口(突变点 = 最近发布时间)

## 诊断步骤
1. logs_query 检索 config / validation / CrashLoopBackOff
2. metrics_query 找错误率突变点,对齐发布时间
3. 区分两类:进程起不来(启动期校验失败)vs 起得来但业务报错(运行期 KeyError)

## 处置
- 两类都以 service_restart 恢复(重启 = 回滚到上一份配置生效)
- 运行期错误需确认发布记录后重启,避免误判为代码 bug

## 禁止
- 不要反复重启掩盖:若重启后复发,升级为回滚发布
