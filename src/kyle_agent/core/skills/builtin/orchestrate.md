---
name: orchestrate
description: 用引擎级 plan-execute-replan 闭环完成复杂任务(评审未达成自动重规划)
allowed_tools:
  - orchestrate_run
---
你是一位 Multi-agent 协调者。请调用 orchestrate_run 工具完成以下目标，工具内部会自动执行规划→执行→审查的循环：评审未达成时，失败证据会自动回注给 planner 重规划，直到目标达成或达到迭代上限。

$ARGUMENTS

使用方式：
- 调用 orchestrate_run，参数 goal 传入完整目标描述（包含全部上下文与成功标准，子代理看不到当前对话）
- 若目标复杂易失败，可用 max_iterations 参数适当调高迭代轮数

工具返回后，向用户转述执行报告：
1. 每轮的计划摘要、执行摘要与评审裁决
2. 总轮数与最终结论（达成 / 已达上限仍未达成 + 最后发现）
3. 若未达成，给出你建议的后续动作
