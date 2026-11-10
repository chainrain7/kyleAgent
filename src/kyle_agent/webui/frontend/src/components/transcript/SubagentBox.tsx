/* 子代理容器:嵌套归属 spawn_agent / orchestrate_run 的工具卡与最终状态徽章。 */
import type { ReactNode } from "react"
import { GitBranch } from "lucide-react"
import type { SubagentItem } from "@/store/session-store"

function SubagentBox({ item, children }: { item: SubagentItem; children: ReactNode }) {
  // 徽章:running 中性;success 绿;其余红
  const done = item.status !== "running"
  const ok = item.status === "success"
  return (
    <div className="rounded-[10px] border bg-card p-3.5">
      <div className="mb-2 flex items-center gap-2 font-mono text-xs text-muted-foreground">
        <GitBranch className="size-3.5" />
        <span>subagent · {item.description}</span>
        <span
          className={`ml-auto rounded-full border px-2.5 py-px text-[10.5px] ${
            !done
              ? "border-border bg-muted text-muted-foreground"
              : ok
                ? "border-ok/30 text-ok"
                : "border-err/30 text-err"
          }`}
        >
          {item.status} · {item.runId}
        </span>
      </div>
      <div className="flex flex-col gap-1">{children}</div>
    </div>
  )
}

export default SubagentBox
