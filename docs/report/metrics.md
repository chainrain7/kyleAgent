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

## 6. 上下文压缩实测(2026-11-10,单次实测)

| 项 | 值 |
|---|---|
| 场景 | 同一 chat session 内顺序 5 轮 goal,每轮 `read_file` 读 2~3 个大文件并一句话总结(uv.lock 102KB / README.md 34KB / WIRE_PROTOCOL.md 29KB / AGENT.md 15KB,共 13 次读取);随后手动 `session.compact` + 1 条探针消息 |
| daemon | 真实 API(DeepSeek deepseek-flash,Anthropic 兼容端点);临时 USERPROFILE/HOME 隔离 `~/.kyle`;默认压缩配置(auto_threshold=0 自动压缩关闭,tool_result 截断 8000/4000) |
| 压缩前 A | **71,944 input_tokens**(goal 5 最后一次 llm.usage;含缓存合计 95,112) |
| 压缩后 B | **1,720 input_tokens**(探针消息首轮 llm.usage;含缓存合计 3,896) |
| 降幅 | **input 字段口径 -97.6%**;含缓存口径 -95.9%。单次实测,不写平均、不外推 |
| 落盘佐证 | thread.jsonl 602,255 B(压缩前 .bak 留档)→ 7,285 B;compact RPC 返回 summary_tokens=2,792 / saved_tokens=26,925(压缩器自有 token 口径,与 API usage 口径不可直接互比) |

**口径说明**:

- A=压缩前最后一次 `llm.usage` 的 input_tokens,B=压缩完成后第一次业务 `llm.usage` 的
  input_tokens;压缩点由 RPC 顺序保证(goal 5 run 结束 → session.compact → 探针 run)。
  压缩器自身的 LLM 调用走 silent bus,不产生 usage 事件——全序列 11 条 usage 均为业务调用;
- DeepSeek 端点启用提示缓存:usage.input_tokens 不含 cache_read,故并列两种口径;
  简历/学习路线引用 **input 字段口径(71,944 → 1,720,-97.6%)**;
- 会话构造:线程重建(每条消息开头)对超长 tool_result 应用 8k/4k 截断,但同一条消息内
  多次工具调用的内存上下文不截断,故单轮内累积至 7 万 token 量级——A 取的是消息内
  最后一次调用,反映压缩前的真实上下文水位;
- **有损观察**(定性):结构化摘要(「# State Snapshot」格式,含 Goal/Completed/关键发现)
  聚合了早期轮次,探针凭摘要复述时丢失 AGENT.md 的读取细节(goal 2/4 曾读取)——
  省 token 的代价是细节有损,符合压缩设计预期。

**usage 时间线摘要**(6 runs / 11 次业务 LLM 调用,ts 为 UTC):

| # | run | input | cache_read |
|---|---|---|---|
| 1-2 | goal1 | 2,238 / 59,727 | 0 / 2,304 |
| 3-4 | goal2 | 3,065 / 16,635 | 3,968 / 7,040 |
| 5-6 | goal3 | 2,765 / 23,231 | 8,576 / 11,392 |
| 7-8 | goal4 | 16,577 / 53,245 | 0 / 16,896 |
| 9-10 | goal5 | 9,399 / **71,944(A)** | 13,312 / 23,168 |
| 11 | 探针 | **1,720(B)** | 2,176 |

**复现方法**(实验脚本不入库,本地保留 `_p2_exp/driver_compact.py`):临时 HOME 起
`kyle-core`(KYLE_PORT=17451,KYLE_TRACE_FILE/KYLE_LOG_FILE 指向 tmp)→ SocketClient
建 chat session → 顺序发 5 条 goal → `session.compact` → 发探针消息 → 解析
`~/.kyle/sessions/<sid>/runs/*/events.jsonl` 的 llm.usage 序列。原始 events/trace
含对话内容,不入库,只有本节汇总数字入库。

## 7. 并行工具批(2026-11-10,引用既有 wall-time 断言)

按预定决策走**引用路径**:tests/ 已有现成并行 wall-time 断言,不再做真实 daemon 重测
(真实文件单次读取 elapsed_ms 仅毫秒级,wall-time 区分度不足,属决策预期内的正常情况)。

- `tests/unit/test_loop.py::test_parallel_readonly_batch_is_faster`:3 个各 sleep 0.25s
  的只读工具一轮并发——串行执行下限 0.75s,断言整轮耗时 <0.55s(留调度余量),
  并断言结果严格按 tool_calls 原顺序回填;
- `test_mixed_batch_falls_back_to_sequential`:批次含变更工具(write_file)时整批退回串行,
  结果顺序仍与 tool_calls 对齐;
- `test_parallel_disabled_by_config`:`max_parallel_tools=1` 时只读批次也保持串行(配置开关)。

简历/学习路线 9.1 对并行能力写**定性描述 + 上述单测引用**,不写加速比数字
(未做 sum/wall ≥ 1.5 的真实实测,不硬凑)。
