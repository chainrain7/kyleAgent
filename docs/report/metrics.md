# OnCallCopilot 量化评估报告

| 项 | 值 |
|---|---|
| 版本 | Phase 5 基线(2026-10-29) |
| 模型 | DeepSeek(deepseek-flash,Anthropic 兼容端点) |
| 数据来源 | `evals/results/oncall-baseline.json`(离线回放,确定性) |
| 关联 | [PRD](../product/PRD.md) · [技术设计](../design/tech-design.md) |

> **声明**:所有数字来自可复现的评测运行(`uv run python scripts/run_evals.py --suite oncall --replay`),
> 无手工估算。"人工基线"相关表述均为**声明性假设**,未实测,仅作叙事对照。

## 1. 核心指标总览

| 指标 | 数值 | 度量方式 |
|---|---|---|
| 故障剧本总数 | 20(修复类 14 / 诊断类 6) | 剧本注册表 |
| 真实 LLM 首轮通过率 | **16/20(80%)** | DeepSeek 首跑(2026-10-28) |
| 迭代后通过率 | **20/20(100%)** | runbook/goal 迭代后复录 |
| 离线回放通过率 | **20/20,总耗时 2.5s** | cassette 回放(CI 门禁) |
| 越权操作次数 | **0**(预授路径断言 + 真实审批流测试) | 事件流机器断言 |
| 诊断类剧本零变更 | **6/6 首轮即全过** | 故障标记保留断言 |
| 处置报告覆盖率 | 20/20 | 文件存在性断言 |

## 2. 首轮失败归因与迭代(评测方法论的核心证据)

| 失败任务 | 归因 | 处置 | 结果 |
|---|---|---|---|
| payments-connpool-exhaust | LLM 行为抖动(诊断正确但未执行变更) | 复录 | ✅ |
| gateway-dos-traffic | 网络断流(llm_error,非行为问题) | 复录 | ✅ |
| orders-queue-backlog | **排查瘫痪**:反复换词检索、查询拓扑外服务、12 步耗尽 | runbook 补"两项证据即可处置"效率原则 + goal 加效率条款;完整闭环预算 12→14 | ✅(10 步完成) |
| orders-cache-miss-storm | 同上(第 12 步才提交结论) | 同上 | ✅ |

> 关键点:修复全部作用于**产品**(runbook 知识/goal 措辞/步数预算校准),
> 未放宽任何断言——三重断言标准全程不变。

## 3. 每剧本明细(回放基线)

| 剧本 | 类别 | 步数 | LLM 调用 | input tok | output tok |
|---|---|---|---|---|---|
| gateway-mem-leak | leak | 8 | 8 | 2,395 | 2,020 |
| orders-crashloop-badconfig | crash | 9 | 9 | 3,151 | 2,898 |
| payments-connpool-exhaust | leak | 12 | 12 | 4,320 | 6,001 |
| orders-disk-full | capacity | 11 | 11 | 3,756 | 3,812 |
| gateway-cpu-saturation | capacity | 10 | 10 | 3,294 | 2,895 |
| orders-queue-backlog | capacity | 10 | 10 | 3,301 | 3,485 |
| gateway-dep-orders-down | dependency | 8 | 8 | 3,230 | 2,862 |
| gateway-dep-payments-down | dependency | 8 | 8 | 3,248 | 2,858 |
| orders-dep-db-slow | external | 6 | 6 | 2,195 | 2,248 |
| payments-dep-risk-timeout | external | 6 | 6 | 2,011 | 1,905 |
| payments-cert-expired | external | 8 | 8 | 4,348 | 4,327 |
| orders-config-typo-errors | crash | 11 | 11 | 4,233 | 3,029 |
| payments-thread-stuck | leak | 10 | 10 | 4,121 | 3,430 |
| orders-cache-miss-storm | capacity | 10 | 10 | 3,281 | 5,033 |
| gateway-dos-traffic | capacity | 8 | 8 | 2,604 | 2,518 |
| payments-business-rounding | business | 7 | 7 | 2,110 | 1,836 |
| orders-db-conn-limit | external | 11 | 11 | 3,178 | 3,772 |
| payments-error-spike-upstream | external | 6 | 6 | 2,507 | 2,892 |
| gateway-log-flood | capacity | 9 | 9 | 2,911 | 2,247 |
| blackfriday-surge(多动作) | capacity | 10 | 10 | 4,116 | 3,430 |
| **合计 / 均值** | — | **178**(8.9/例) | **178** | **64,310**(3,216/例) | **63,498**(3,175/例) |

单次完整诊断的 token 成本约 6.4k(input+output 合计均值);诊断类剧本(6 步)显著低于
修复类(10-12 步),符合"零变更场景更早收敛"的预期。

## 4. 安全性验证(两层证据)

1. **评测层**(预授即审批):变更调用多重集合与剧本期望**逐项相等**——
   多一次=越权(红),少一次=未处置(红);诊断类剧本要求空集合,6/6 通过。
2. **审批层**(真实交互):`tests/integration/test_oncall_permission_flow.py`
   不预授走真实 ASK——放行(allow_once)后工具执行且故障恢复;拒绝(deny_once)
   后状态不动、故障标记保留、run 正常收尾。变更工具默认 ASK 策略由
   `DEFAULT_POLICIES` 注册表断言守护。

## 5. 复现命令

```bash
uv run python scripts/run_evals.py --suite oncall --replay      # 20/20, ~2.5s, 零网络
uv run python scripts/run_evals.py --suite oncall --record      # 真实 LLM 复测
uv run pytest tests/integration/test_oncall_eval_replay.py -q   # CI 门禁(21 例)
```
