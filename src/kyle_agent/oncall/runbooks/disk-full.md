# 磁盘满与日志洪水(Disk Full / Log Flood)

## 触发特征
- 指标:disk_pct 超阈值(90%+)
- 日志:"no space left on device"、日志写入速率异常(如 debug 未关)

## 诊断步骤
1. metrics_query 看 disk_pct 趋势(缓涨=数据堆积,陡涨=洪水)
2. logs_query 确认写入来源(重复行多 = 调试日志未关)

## 处置
- log_cleanup 该服务(清理/截断日志,释放空间)
- 若为调试日志导致,提醒关闭 debug 开关治本

## 禁止
- 不要 service_restart(不清盘,重启完还是满)
