# 证书过期(Certificate Expired)

## 触发特征
- TLS 握手失败、回调中断,错误率抬升
- 日志:"certificate has expired"、"x509"、"tls handshake failed"

## 诊断步骤
1. logs_query 检索 cert / tls / x509
2. 确认过期的是本服务证书还是上游渠道证书(日志主体)

## 处置
- 诊断类:本服务证书续期需人工执行(密钥操作不可由 Agent 代办)
- 建议:运维续期证书后滚动重启生效

## 禁止
- 单纯 service_restart 无法解决(证书文件未换,重启即复发)
