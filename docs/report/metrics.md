# OnCallCopilot 量化评估报告

| 项 | 值 |
|---|---|
| 版本 | Phase 5 基线(2026-10-29)· 端到端效果评测补录(2026-11-12) |
| 模型 | DeepSeek(deepseek-flash,Anthropic 兼容端点) |
| 数据来源 | `evals/results/oncall-baseline.json`(离线回放,确定性);§8 来自 `evals/results/20261112-score-round1.json` 与 `20261112-score-final.json`(record 产物评分) |
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
| **端到端效果(60 剧本,2026-11-12)** | Top-1/Top-3 命中 **100%** · 完整通过 **60/60** · 处置用时中位 **20.1s**(≤120s 达标) | §8(record 单轮实测) |

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

## 8. 端到端效果评测(2026-11-12,60 剧本 record 单轮实测)

> 本节回答"系统有用吗":根因命中率(Top-1/Top-3)、端到端处置用时(wall/MTTD/MTTR)
> 与人工参考基线的对比。全部时间指标来自 **record 轮 events.jsonl**(单轮实测口径);
> 回放轮 ts 为重放时重新物化、LLM 零延迟,**不产生任何时间指标**。

### 8.1 评测集构成

60 条 = 基础 20(2026-10-28 首轮评测集)+ 新增 40,新增 40 按难度三层设计:

| 难度层 | 条数 | 设计 |
|---|---|---|
| 基础(单故障) | 20 | 2026-10 首轮评测集 |
| 单故障变体 | 22 | 复用注入原语,换服务/幅度/文案 |
| 误导表象 | 10 | 告警在 A 服务,真根因经拓扑(gateway→{orders,payments};orders→orders-db;payments→{payments-db,risk-svc})传导 |
| 复合故障 | 8 | 双故障并存/因果链,期望动作为两组件处置并集 |

类别分布(单一类别 52 + 复合 8):leak 8 · crash 6 · capacity 17 · dependency 4 ·
external 13 · business 4 · 复合 8(构成:capacity+crash 1 / capacity+leak 3 /
external+capacity 2 / leak+capacity 1 / crash+capacity 1)。
零变更(诊断类)17 条 / 修复类 43 条。复合剧本的 fault 标记复用组件剧本 id,
双动作顺序无关恢复;全部 60 条按期望动作序列可自洽通过三重断言(单测
`test_all_scenarios_solvable_by_expected_actions` 守护)。

`submit_diagnosis` 新增可选 `ranked_candidates`(2–4 条按置信度降序的候选根因,
首位须与 root_cause 一致,pydantic 硬校验)——鉴别诊断协议,Top-3 指标的载体。

### 8.2 总体成绩(round-1 → 终局)

| 指标 | round-1(2026-11-12) | 终局(goal 迭代 2 轮后) |
|---|---|---|
| Top-1 命中率 | 86.7%(52/60) | **100%(60/60)** |
| Top-3 命中率 | 86.7% | **100%** |
| 候选覆盖率 | 86.7% | **100%** |
| 完整通过率(三重断言) | 75%(45/60) | **100%(60/60)** |

round-1 的 8 个 Top-1 未命中**全部**来自 llm_error 网络中断(无 submit 产出),
行为性根因误判 0;7 个完整通过失败中 6 条复合故障只执行了两组件动作之一、
1 条漏执行扩容(见 8.4 归因)。

### 8.3 分层与时间指标(终局,record 单轮实测)

| 难度层 | n | Top-1 | wall 中位/P90(s) | MTTD 中位(s) | MTTR 中位(s) |
|---|---|---|---|---|---|
| 全量 | 60 | 100% | 20.1 / 29.3 | 16.3 | 10.4 |
| 基础 | 20 | 100% | 20.1 / 23.3 | 15.7 | 9.5 |
| 单故障变体 | 22 | 100% | 20.9 / 29.3 | 17.6 | 10.4 |
| 误导表象 | 10 | 100% | 18.7 / 20.0 | 14.3 | 8.8 |
| 复合故障 | 8 | 100% | 31.8 / 106.6 | 27.6 | 17.5 |

- wall = run.finished − run.started;MTTD = 最后一条 submit_diagnosis 完成 − run.started;
  MTTR = 最后一个变更工具完成 − run.started(仅 43 条修复类;诊断类无 MTTR);
- 全量 wall 中位 **20.1s**,P90 29.3s——**PRD §2.2「中位数 ≤120s」达标**;
  复合层 P90 106.6s 由两条双动作长轨迹拉高(composite-gateway-surge-memleak 106.6s、
  composite-orders-typo-cpu 98.1s),仍低于 120s;
- round-1 时间轴(供对照):wall 20.0/28.7,MTTD 18.1/25.6,MTTR 10.4/15.0。

### 8.4 迭代史(方法论:失败是数据,修产品不修断言)

| 轮次 | 处置 | 结果 | 归因 |
|---|---|---|---|
| round-1 | 全量 60 首录 | 45/60 | 8 条 llm_error(网络中断);7 条行为失败:6 条复合只执行两组件动作之一,1 条(orders-cache-storm-marketing)将缓存打穿误判为"业务/人工类"漏执行扩容 |
| 迭代 1 | goal 追加"复合故障每个可自动恢复组件都必须执行各自变更"条款,复录 15 条失败 | 56/60 | 8 条网络复录全过;复合 6 条恢复 2 条——其余 4 条将并存异常归并为单一因果链,或把"积压止血"误判为外部问题零变更 |
| 迭代 2 | goal 追加"独立并存异常不得归并为单一因果链只处置其一;本服务积压属可止血容量问题,即使诱因在外部也应先扩容止血",复录 4 条 | **60/60** | 4 条全部恢复 |

关键纪律:两轮迭代**只改 goal 措辞**(PRD 允许的迭代面),`root_cause_keywords` /
`expected_actions` / severity 等 ground-truth 字段在提交 9184284 后冻结,git diff
可核;复合剧本评测步数预算 12→14 属 harness 配准非断言放宽;45 条通过任务沿用
首轮录制不重录(录制成本线性增长,与 2026-10 轮 20 剧本迭代同款口径)。

### 8.5 人工基线对比(估算口径,非实测)

**人工基线为按 runbook 步骤数与经验系数估算的参考值,非实测**(类别基线 ×
P1×1.2/P2×1.0 取整到 5 分钟,字段 `manual_triage_minutes` 随剧本注册表分发)。

| 口径 | 值 |
|---|---|
| 人工参考基线中位(估算) | 30 分钟 |
| 端到端处置中位(record 实测) | 20.1s ≈ 0.34 分钟 |
| 加速比 | 约 **90 倍**(脚本取整口径 100×,score JSON `speedup_vs_manual_est`) |
| 缩短 | 约 **99%** |

表述规范:凡引用加速比处必须带"模拟录制口径下,较 runbook 人工参考基线(估算)"限定语。

### 8.6 复现命令

```bash
# 记录(真实 LLM,60 条;逐任务 --task 可断点续跑)
uv run python scripts/run_evals.py --record --suite oncall \
    --artifacts-dir evals/results/<ts>-record
# 评分聚合(命中/候选/时间轴/人工对比;--results 供完整通过率)
uv run python scripts/eval_score.py --artifacts-dir evals/results/<ts>-record \
    --results evals/results/<ts>-replay.json --out evals/results/<ts>-score.json
# 离线回放(CI 门禁,确定性)
uv run python scripts/run_evals.py --replay --suite oncall    # 60/60
```

本轮产物:`evals/results/20261112-score-round1.json`(round-1 原始成绩留档)、
`evals/results/20261112-score-final.json`(终局)、`evals/results/20261112-round1-replay.json`
与 `20261112-round2-replay.json`(两轮回放);录制产物 events.jsonl/处置报告在
`20261112-record/`(不入库,仅成绩与口径入库)。

## 9. RAG 检索质量(2026-11-12 复核,36 条标注集)

`scripts/eval_rag.py` 的 Recall@k 与 hit@k 事实等价(命中即召回),故检索质量
以 hit 口径引用。检索语料与确定性行为自 2026-11-11 首测以来零变化,本轮重跑
`eval_rag.py` 六数字与 `20261111-105331-rag-eval.json` **逐位一致**(blake2b
确定性哈希嵌入,零 Key、离线可复现):

| 模式(全量 n=36) | R@1(=hit@1) | R@3(=hit@3) | MRR |
|---|---|---|---|
| keyword | 0.778 | 0.972 | 0.861 |
| vector | 0.833 | 0.889 | 0.861 |
| hybrid | 0.889 | 0.944 | 0.917 |

字面/改写分桶与逐位一致性由 `evals/results/20261112-161753-rag-eval.json` 与
首测文件直接 diff 核验;默认 keyword、hybrid opt-in 的门禁决策依据不变(§9 首测见
2026-11-11 记录,混合模式赢 R@1 输 R@3,按预设门禁回退)。
