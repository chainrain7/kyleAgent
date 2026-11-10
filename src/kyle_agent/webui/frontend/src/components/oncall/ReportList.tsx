/* 处置报告清单:点击打开浮层;新报告出现由 store 轻提示。 */
import { FileCheck } from "lucide-react"
import { useOncallStore } from "@/store/oncall-store"

function ReportList() {
  const reports = useOncallStore((s) => s.reports)
  const openReport = useOncallStore((s) => s.openReport)

  return (
    <section className="flex flex-col gap-0.5">
      <div className="sec-label">处置报告</div>
      {!reports.length ? (
        <div className="px-2 py-1 text-xs text-muted-foreground/70">
          尚无报告 · 完成一次诊断后生成
        </div>
      ) : (
        <div>
          {reports
            .slice()
            .reverse()
            .map((name) => (
              <button
                key={name}
                type="button"
                className="flex w-full items-center gap-[7px] rounded-lg px-2 py-1.5 font-mono text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                onClick={() => openReport(name)}
              >
                <FileCheck className="size-3 flex-none text-ok/70" />
                <span className="truncate">{name}</span>
              </button>
            ))}
        </div>
      )}
    </section>
  )
}

export default ReportList
