# 内存泄漏(Memory Leak)

## 触发特征
- 指标:memory_mb 持续上涨逼近容器上限(如 94%),CPU 同步升高
- 日志:"memory usage N% of limit"、"OOMKilled"、"exit 137"

## 诊断步骤
1. metrics_query 看 memory_mb 15 分钟趋势(单调上涨 + 突变点对应发布时间→疑新代码泄漏)
2. logs_query 检索 OOMKilled / memory 关键词
3. 区分:突发流量也会抬高内存,但流量回落会跟着回落;泄漏不回落

## 处置
- 立即缓解:service_restart 该服务(滚动重启释放内存)
- 治本:泄漏代码修复(发布回滚或补丁),重启只是争取时间

## 禁止
- 不要 scale_up 掩盖问题(泄漏不会因扩容消失,只会推迟 OOM)
