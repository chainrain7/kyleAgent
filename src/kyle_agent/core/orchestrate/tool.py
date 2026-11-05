"""orchestrate_run:引擎级 plan-execute-replan 闭环(支柱:Multi-agent 编排)。

把 planner→executor→reviewer 从"纯 prompt 一次性线性流程"升级为引擎级循环:
reviewer 裁决未达成 → 失败证据回注 planner 出 v2 计划 → 再执行 → 再评审,
直到 ACHIEVED 或迭代上限。闭环在本模块内实现,不触碰 core/loop.py 与 core/bus/
事件模型;子角色复用 SpawnAgentTool 前景阻塞模式驱动,不复制 child-runner 机制。

串行不变量(重要):循环内所有子代理调用严格按 planner→executor→reviewer
顺序逐个 await,无任何并行——这是评测 cassette 顺序回放的确定性前提,
任何改动都不允许引入并发 spawn。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from kyle_agent.core.orchestrate.verdict import ReviewVerdict, parse_verdict
from kyle_agent.core.subagent.registry import BackgroundTaskRegistry
from kyle_agent.core.subagent.tool import SpawnAgentTool
from kyle_agent.core.tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from kyle_agent.core.events.bus import EventBus
    from kyle_agent.core.llm.base import LLMProvider
    from kyle_agent.core.permissions.manager import PermissionManager

# 回注 planner 时 executor 汇报的最大保留字符数(防止上下文爆炸)
_EXEC_SUMMARY_LIMIT = 4_000
# 最终 markdown 报告中每段摘录的最大字符数
_REPORT_SNIPPET_LIMIT = 800


class OrchestrateRunParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    goal: str
    # 0 表示使用 config.orchestration.max_iterations 默认值
    max_iterations: int = 0


# 数据结构:单轮循环的产物(计划/执行汇报/裁决/发现)
class _CycleRecord:
    # 保存单轮的四个产物文本,供报告渲染与下一轮回注
    def __init__(self, cycle: int, plan: str, exec_report: str, review: str,
                 verdict: ReviewVerdict, findings: str) -> None:
        self.cycle = cycle
        self.plan = plan
        self.exec_report = exec_report
        self.review = review
        self.verdict = verdict
        self.findings = findings


# 引擎级 plan-execute-replan 循环:reviewer 裁决未达成时自动重规划
class OrchestrateRunTool(BaseTool):
    name = "orchestrate_run"
    description = (
        "Engine-level plan-execute-replan loop (orchestration). "
        "Runs planner→executor→reviewer cycles: if the reviewer verdict is not "
        "ACHIEVED, failure evidence is fed back to the planner for a revised plan "
        "automatically, until the goal is achieved or the iteration cap is hit. "
        "Use this instead of manually chaining spawn_agent calls."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "Complete goal description for the whole mission.",
            },
            "max_iterations": {
                "type": "integer",
                "description": (
                    "Max plan-execute-review cycles. 0 = use server default "
                    "(orchestration.max_iterations, default 3)."
                ),
            },
        },
        "required": ["goal"],
    }
    params_model = OrchestrateRunParams

    # 构造闭环工具;内部组合一个 depth=0 的 SpawnAgentTool 驱动三个子角色
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        runs_dir: Path,
        session_id: str,
        max_iterations: int,
    ) -> None:
        self._max_iterations = max(max_iterations, 1)
        # 复用 SpawnAgentTool(前景阻塞模式)驱动子角色,不复制 child-runner 机制
        self._spawn = SpawnAgentTool(
            provider=provider,
            parent_bus=parent_bus,
            parent_run_id=parent_run_id,
            permission_manager=permission_manager,
            max_steps=max_steps,
            task_registry=task_registry,
            runs_dir=runs_dir,
            session_id=session_id,
            depth=0,
        )

    # 执行 plan-execute-replan 循环,返回结构化 markdown 报告
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = OrchestrateRunParams.model_validate(params)
        total = p.max_iterations if p.max_iterations > 0 else self._max_iterations
        goal = p.goal

        records: list[_CycleRecord] = []
        achieved = False
        for cycle in range(1, total + 1):
            # ── 阶段 1:规划(planner)──
            planner_prompt = self._planner_prompt(goal, records)
            plan = await self._spawn_role(
                "planner", f"规划任务(v{cycle})", planner_prompt
            )

            # ── 阶段 2:执行(executor)──
            exec_prompt = (
                f"原始目标:\n{goal}\n\n请严格按以下执行计划完成目标:\n\n{plan}"
            )
            exec_report = await self._spawn_role(
                "executor", f"执行计划(v{cycle})", exec_prompt
            )

            # ── 阶段 3:审查(reviewer)──
            review_prompt = (
                f"原始目标:\n{goal}\n\n执行汇报:\n{exec_report}\n\n"
                "请核查目标是否真正达成,并按格式在最后一行给出 VERDICT 裁决。"
            )
            review_out = await self._spawn_role(
                "reviewer", f"审查结果(v{cycle})", review_prompt
            )

            verdict, findings = parse_verdict(review_out)
            records.append(_CycleRecord(cycle, plan, exec_report, review_out,
                                        verdict, findings))
            if verdict == ReviewVerdict.ACHIEVED:
                achieved = True
                break

        return ToolResult(content=self._render_report(goal, records, achieved))

    # 派生单个子角色并返回其最终文本输出;子代理失败时抛 RuntimeError 终止循环
    async def _spawn_role(self, role: str, description: str, prompt: str) -> str:
        # 严格串行:每次调用逐个 await,循环内绝无并行 spawn(cassette 顺序回放前提)
        result = await self._spawn.invoke({
            "description": description,
            "prompt": prompt,
            "subagent_type": role,
        })
        if result.is_error:
            raise RuntimeError(
                f"orchestrate_run: sub-agent {role!r} failed: {result.content}"
            )
        return result.content

    # 构造 planner 提示词;存在历史轮次时附加 v(n-1) 计划全文与失败证据
    def _planner_prompt(self, goal: str, records: list[_CycleRecord]) -> str:
        prompt = (
            f"目标:\n{goal}\n\n请制定清晰的有序执行计划,每步包含做什么、"
            "为什么、成功标准。"
        )
        if not records:
            return prompt
        last = records[-1]
        exec_summary = last.exec_report
        if len(exec_summary) > _EXEC_SUMMARY_LIMIT:
            exec_summary = exec_summary[:_EXEC_SUMMARY_LIMIT] + "…(截断)"
        # 失败证据回注:上一轮计划全文 + executor 汇报摘要 + reviewer 发现段落
        prompt += (
            f"\n\n--- 上一计划未达成,依据以下证据修订计划 ---\n\n"
            f"## 上一轮计划(v{last.cycle},全文)\n{last.plan}\n\n"
            f"## 执行汇报摘要\n{exec_summary}\n\n"
            f"## 审查发现\n{last.findings}\n\n"
            "请输出修订后的完整计划 v" + str(last.cycle + 1) +
            "(不要重复已失败的做法,针对上述发现调整)。"
        )
        return prompt

    # 渲染最终结构化 markdown 报告(每轮摘要 + 总轮数 + 最终结论)
    def _render_report(self, goal: str, records: list[_CycleRecord],
                       achieved: bool) -> str:
        lines = ["# orchestrate_run 执行报告", "", f"目标:{goal}", ""]
        for r in records:
            lines += [
                f"## 第 {r.cycle} 轮",
                f"- 计划摘要:{_snippet(r.plan)}",
                f"- 执行摘要:{_snippet(r.exec_report)}",
                f"- 裁决:{r.verdict.value.upper()}",
                f"- 发现:{_snippet(r.findings) or '(无)'}",
                "",
            ]
        lines.append("## 最终结论")
        lines.append(f"- 总轮数:{len(records)}")
        if achieved:
            lines.append(
                f"- 结论:目标已达成(第 {records[-1].cycle} 轮评审通过)。"
            )
        else:
            last = records[-1] if records else None
            lines.append(
                f"- 结论:已达迭代上限({len(records)} 轮)仍未达成。"
            )
            if last is not None:
                lines.append(f"- 最后发现:{_snippet(last.findings)}")
                lines.append(f"- 最后一轮裁决:{last.verdict.value.upper()}")
        return "\n".join(lines)


# 截取文本用于报告展示(超长加省略号)
def _snippet(text: str, limit: int = _REPORT_SNIPPET_LIMIT) -> str:
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + "…(截断)"
    return text
