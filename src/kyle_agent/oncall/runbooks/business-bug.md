# 业务数据差错(Business Logic Bug)

## 触发特征
- 无明显性能异常,少量业务结果错误(金额/状态/数量)
- 日志:mismatch / discrepancy / 计算类错误,量小而精确

## 诊断步骤
1. logs_query 检索 mismatch / 计算 / 金额
2. 确认错误模式是否与最近发布相关

## 处置
- 诊断类:业务代码 bug 只能回滚或热修版本
- 建议:回滚发布 + 数据订正方案评估

## 禁止
- 严禁 service_restart/scale_up(重启会冲掉现场证据且不解决代码问题)
