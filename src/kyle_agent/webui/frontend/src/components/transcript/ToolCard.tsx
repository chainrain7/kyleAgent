/* 工具卡:可折叠,展示状态点/参数预览/耗时/输出(错误分类);完成有输出自动展开。 */
import { useEffect, useState } from "react"
import { ChevronRight } from "lucide-react"
import type { ToolItem } from "@/store/session-store"

// 状态点配色:running 蓝(呼吸) / ok 绿 / err 红
function dotClass(state: ToolItem["state"]): string {
  switch (state) {
    case "ok":
      return "bg-ok"
    case "err":
      return "bg-err"
    default:
      return "bg-info animate-breathe"
  }
}

function ToolCard({ item }: { item: ToolItem }) {
  const [open, setOpen] = useState(item.open)
  // 完成且有输出时自动展开(仅一次;之后用户手动折叠不回弹)
  useEffect(() => {
    if (item.output !== null && item.open) setOpen(true)
  }, [item.output, item.open])

  return (
    <div className="group rounded-[10px] border bg-card transition-colors hover:border-border-strong">
      <button
        type="button"
        className="flex w-full items-center gap-2.5 px-3 py-[7.5px] text-left text-[12.5px]"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={`size-1.5 flex-none rounded-full ${dotClass(item.state)}`} />
        <span className="font-mono text-xs font-semibold">{item.name}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-muted-foreground">
          {item.paramsPreview}
        </span>
        <span className="flex items-center gap-2.5">
          <span className="font-tabular font-mono text-[10px] text-muted-foreground/60">
            {item.elapsedMs !== null ? `${item.elapsedMs}ms` : ""}
          </span>
          <ChevronRight
            className={`size-3 text-muted-foreground/60 transition-transform duration-150 ${open ? "rotate-90" : ""}`}
          />
        </span>
      </button>
      {/* 高度过渡展开:grid-rows 0fr→1fr */}
      <div
        className={`grid transition-[grid-template-rows] duration-200 ease-out ${open ? "grid-rows-[1fr]" : "grid-rows-[0fr]"}`}
      >
        <div className="overflow-hidden">
          <div className="border-t px-3.5 pb-3 pt-2.5">
            <div className="mb-1 mt-2.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground/70 first:mt-0">
              参数
            </div>
            <pre className="max-h-[260px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.8px] leading-relaxed text-muted-foreground">
              {item.paramsJson}
            </pre>
            {item.output !== null && (
              <>
                <div className="mb-1 mt-2.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground/70">
                  {item.outputIsError ? "错误" : "输出"}
                </div>
                <pre
                  className={`max-h-[260px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.8px] leading-relaxed ${item.outputIsError ? "text-err" : "text-muted-foreground"}`}
                >
                  {item.output}
                </pre>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default ToolCard
