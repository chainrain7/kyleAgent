# 连接池/线程池耗尽(Pool Exhaust)

## 触发特征
- 大量请求超时,但服务状态可能只是 degraded
- 日志:"ConnectionPool exhausted"、"thread pool exhausted"、"acquire timeout"

## 诊断步骤
1. logs_query 检索 exhausted / pool / acquire
2. 区分连接池(db 连接泄漏)与线程池(死锁/慢任务堆积):看 busy 比例与 lock 等待
3. metrics 确认 p99 抬升但 qps 未涨(非容量问题)

## 处置
- service_restart 该服务(释放泄漏的连接/线程)
- 治本:修复泄漏点;临时可调大池上限

## 禁止
- scale_up 无效(新副本同样会泄漏)
