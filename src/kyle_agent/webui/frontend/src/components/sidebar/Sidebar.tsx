/* 侧栏:新会话、故障演练、告警队列、服务健康、处置报告、会话状态与操作、
 * daemon/session 信息脚注(OnCall 区块随 /api/state available 显隐)。 */
import { Plus, Scissors, X } from "lucide-react"
import AlertQueue from "@/components/oncall/AlertQueue"
import DrillPanel from "@/components/oncall/DrillPanel"
import ReportList from "@/components/oncall/ReportList"
import ServiceHealth from "@/components/oncall/ServiceHealth"
import { useOncallStore } from "@/store/oncall-store"
import { useSessionStore } from "@/store/session-store"

// 会话状态胶囊:running 蓝(呼吸)/ waiting 绿点 / 无会话 中性灰
function StatePill() {
  const running = useSessionStore((s) => s.running)
  const sessionId = useSessionStore((s) => s.sessionId)
  const label = running ? "running" : sessionId ? "waiting_for_input" : "—"
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 pl-2 font-mono text-[11px] font-medium ${
        running ? "border-info/30 text-info" : "border-border bg-muted text-muted-foreground"
      }`}
    >
      <span
        className={`size-1.5 rounded-full ${
          running ? "bg-info animate-breathe" : sessionId ? "bg-ok" : "bg-muted-foreground/40"
        }`}
      />
      <span>{label}</span>
    </span>
  )
}

function Sidebar() {
  const sessionId = useSessionStore((s) => s.sessionId)
  const running = useSessionStore((s) => s.running)
  const newSession = useSessionStore((s) => s.newSession)
  const compactSession = useSessionStore((s) => s.compactSession)
  const closeSession = useSessionStore((s) => s.closeSession)
  const oncallAvailable = useOncallStore((s) => s.available)
  const daemonHost = location.hostname === "" ? "127.0.0.1" : location.hostname

  return (
    <aside className="flex w-[256px] flex-none flex-col gap-[18px] overflow-y-auto border-r bg-background px-3 pb-2.5 pt-3.5">
      <button
        type="button"
        onClick={() => newSession()}
        className="flex h-9 items-center justify-center gap-[7px] rounded-[10px] bg-primary text-[13px] font-semibold text-primary-foreground transition-all hover:opacity-90 active:scale-[0.985]"
      >
        <Plus className="size-3.5" strokeWidth={2.2} />
        新会话
      </button>

      {/* OnCallCopilot 值班面板:演练环境可用时展示 */}
      {oncallAvailable && (
        <>
          <DrillPanel />
          <AlertQueue />
          <ServiceHealth />
          <ReportList />
        </>
      )}

      <section className="flex flex-col gap-0.5">
        <div className="sec-label">会话</div>
        <div className="flex items-center justify-between rounded-lg px-2 py-1 text-[12.5px] text-muted-foreground">
          <span>状态</span>
          <StatePill />
        </div>
        <div className="mt-1 flex flex-col gap-0.5">
          <button
            type="button"
            disabled={!sessionId || running}
            onClick={() => compactSession()}
            className="flex items-center gap-2 rounded-lg px-2 py-[7px] text-[12.5px] text-foreground/80 transition-colors hover:bg-accent disabled:opacity-40"
          >
            <Scissors className="size-3.5 opacity-75" />
            压缩上下文
          </button>
          <button
            type="button"
            disabled={!sessionId}
            onClick={() => closeSession()}
            className="flex items-center gap-2 rounded-lg px-2 py-[7px] text-[12.5px] text-foreground/80 transition-colors hover:bg-err/10 hover:text-err disabled:opacity-40"
          >
            <X className="size-3.5 opacity-75" />
            关闭会话
          </button>
        </div>
      </section>

      <footer className="mt-auto flex flex-col gap-[3px] border-t px-2 pb-1 pt-2.5 text-[11px] text-muted-foreground/50">
        <div className="flex items-center justify-between gap-2">
          <span>daemon</span>
          <span className="font-mono text-[10.5px]">{daemonHost}:7437</span>
        </div>
        <div className="flex items-center justify-between gap-2">
          <span>session</span>
          <span className="max-w-[150px] truncate font-mono text-[10.5px] font-semibold">
            {sessionId || "—"}
          </span>
        </div>
      </footer>
    </aside>
  )
}

export default Sidebar
