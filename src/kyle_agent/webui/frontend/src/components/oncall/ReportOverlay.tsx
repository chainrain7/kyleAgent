/* 处置报告浮层:全屏遮罩 + 居中表单,markdown 走 .md-body(复用对话排版)。 */
import { ArrowLeft } from "lucide-react"
import { Markdown } from "@/components/transcript/Markdown"
import { useOncallStore } from "@/store/oncall-store"

function ReportOverlay() {
  const reportName = useOncallStore((s) => s.reportName)
  const reportContent = useOncallStore((s) => s.reportContent)
  const reportLoading = useOncallStore((s) => s.reportLoading)
  const closeReport = useOncallStore((s) => s.closeReport)

  if (!reportName) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-[4px]">
      <div className="flex h-[min(86vh,760px)] w-[min(840px,92vw)] flex-col overflow-hidden rounded-[10px] border border-border-strong bg-card shadow-2xl">
        <div className="flex flex-none items-center gap-3 border-b bg-background/80 px-4 py-3 text-[13px] text-muted-foreground">
          <button
            type="button"
            className="flex items-center gap-1 text-[12.5px] transition-colors hover:text-foreground"
            onClick={closeReport}
          >
            <ArrowLeft className="size-3.5" />
            返回对话
          </button>
          <b className="text-sm font-semibold text-foreground">处置报告</b>
          <span className="font-mono text-[11px] text-muted-foreground/60">{reportName}</span>
        </div>
        <div className="flex-1 overflow-y-auto px-7 py-[22px]">
          <div className="report-body md-body text-[13.5px]">
            {reportLoading && <p className="text-muted-foreground">读取中…</p>}
            {!reportLoading && reportContent !== null && <Markdown>{reportContent}</Markdown>}
          </div>
        </div>
      </div>
    </div>
  )
}

export default ReportOverlay
