/* 故障演练:注入下拉菜单(剧本清单来自 /api/state)+ 一键重置。 */
import { useState } from "react"
import { RotateCcw, Zap } from "lucide-react"
import { useOncallStore } from "@/store/oncall-store"

function DrillPanel() {
  const scenarios = useOncallStore((s) => s.scenarios)
  const inject = useOncallStore((s) => s.inject)
  const resetEnv = useOncallStore((s) => s.resetEnv)
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <div className="relative">
      <div className="flex gap-2">
        <button
          type="button"
          className="flex h-9 flex-1 items-center justify-center gap-[7px] rounded-lg border bg-secondary text-[13px] font-semibold transition-colors hover:border-border-strong hover:bg-accent"
          onClick={() => setMenuOpen((v) => !v)}
        >
          <Zap className="size-3.5" />
          故障演练 ▾
        </button>
        <button
          type="button"
          title="重置环境"
          aria-label="重置环境"
          className="grid h-9 w-9 flex-none place-items-center rounded-lg border bg-secondary text-muted-foreground transition-colors hover:border-border-strong hover:bg-accent"
          onClick={() => resetEnv()}
        >
          <RotateCcw className="size-3.5" />
        </button>
      </div>
      {menuOpen && (
        <>
          {/* 点击菜单外关闭 */}
          <div className="fixed inset-0 z-30" onClick={() => setMenuOpen(false)} />
          <div className="absolute left-0 top-[42px] z-40 max-h-[340px] w-[280px] overflow-y-auto rounded-xl border border-border-strong bg-popover p-1.5 shadow-lg">
            <div className="sec-label px-2.5 pb-1 pt-1.5">选择故障剧本注入</div>
            {scenarios.map((s) => (
              <button
                key={s.id}
                type="button"
                className="block w-full rounded-lg px-2.5 py-[7px] text-left text-[12.5px] leading-snug text-foreground/85 transition-colors hover:bg-accent"
                onClick={() => {
                  setMenuOpen(false)
                  inject(s.id)
                }}
              >
                <span
                  className={`mr-1.5 font-mono text-[10px] ${
                    s.severity === "P1" ? "text-err" : s.severity === "P2" ? "text-warn" : "text-muted-foreground"
                  }`}
                >
                  {s.severity}
                </span>
                <span>{s.summary}</span>
                <span className="mt-0.5 block font-mono text-[10.5px] text-muted-foreground/60">
                  {s.id}
                  {s.recoverable ? "" : " · 诊断类"}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

export default DrillPanel
