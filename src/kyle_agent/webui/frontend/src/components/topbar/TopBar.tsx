/* 顶栏:品牌、环境徽章(P3 接 /api/state)、主题切换、上下文水位、
 * cache 命中率、连接状态。 */
import { Moon, Sun } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useAppStore } from "@/store/app-store"
import { useOncallStore } from "@/store/oncall-store"
import { useSessionStore } from "@/store/session-store"

// 环境状态徽章:平稳绿点 / 活跃告警或服务异常红点呼吸
function EnvChip() {
  const available = useOncallStore((s) => s.available)
  const services = useOncallStore((s) => s.services)
  const alerts = useOncallStore((s) => s.alerts)
  if (!available) return null
  const names = Object.keys(services)
  const down = names.filter((n) => services[n] && services[n].status !== "running")
  const firing = alerts.filter((a) => a.status === "firing")
  const alerting = firing.length > 0 || down.length > 0
  const text = firing.length > 0
    ? `${firing.length} 条活跃告警`
    : down.length > 0
      ? `${down.length} 个服务异常`
      : "环境平稳"
  return (
    <span
      className={`inline-flex items-center gap-[7px] rounded-full border px-[11px] py-[3.5px] text-xs font-medium transition-colors duration-300 ${
        alerting ? "border-err/35 text-err" : "border-border bg-card text-muted-foreground"
      }`}
    >
      <span
        className={`size-1.5 rounded-full ${alerting ? "bg-err animate-breathe" : "bg-ok"}`}
      />
      <span>{text}</span>
    </span>
  )
}

// 连接点配色:ready 绿 / busy 蓝(呼吸) / down 红 / connecting 琥珀(呼吸)
function connDotClass(conn: string): string {
  switch (conn) {
    case "ready":
      return "bg-ok"
    case "busy":
      return "bg-info animate-breathe"
    case "down":
      return "bg-err"
    default:
      return "bg-warn animate-breathe"
  }
}

function TopBar() {
  const conn = useAppStore((s) => s.conn)
  const connLabel = useAppStore((s) => s.connLabel)
  const theme = useAppStore((s) => s.theme)
  const switchTheme = useAppStore((s) => s.switchTheme)
  const contextPct = useSessionStore((s) => s.contextPct)
  const cacheHitRate = useSessionStore((s) => s.cacheHitRate)

  return (
    <header className="z-10 flex h-[54px] flex-none items-center justify-between border-b bg-background/80 px-5 backdrop-blur-xl">
      <div className="flex items-center gap-2.5">
        <img src="/favicon.svg" alt="" className="h-6 w-6 rounded-[7px]" />
        <span className="text-[14.5px] font-semibold tracking-tight">OnCallCopilot</span>
        <span className="mx-1 h-4 w-px bg-border-strong" />
        <EnvChip />
      </div>
      <div className="flex items-center gap-3.5">
        <Button
          variant="outline"
          size="icon"
          className="size-[30px] rounded-lg"
          title="切换亮/暗色"
          aria-label="切换亮色/暗色主题"
          onClick={switchTheme}
        >
          {theme === "dark" ? <Sun className="size-[15px]" /> : <Moon className="size-[15px]" />}
        </Button>
        {contextPct !== null && (
          <div className="flex items-center gap-2" title="上下文水位">
            <div className="h-[3px] w-[84px] overflow-hidden rounded-full bg-muted">
              <div
                className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                  contextPct >= 85 ? "bg-err" : contextPct >= 60 ? "bg-warn" : "bg-foreground"
                }`}
                style={{ width: `${contextPct}%` }}
              />
            </div>
            <span className="font-tabular font-mono text-[11px] text-muted-foreground/70">
              ctx {contextPct}%
            </span>
          </div>
        )}
        {cacheHitRate !== null && (
          <span
            title="prompt 缓存命中率"
            className={`rounded-full border px-2.5 py-0.5 font-tabular text-[11.5px] font-medium ${
              cacheHitRate >= 0.6 ? "border-ok/25 text-ok" : "border-border bg-card text-muted-foreground"
            }`}
          >
            cache {(cacheHitRate * 100).toFixed(0)}%
          </span>
        )}
        <div className="flex items-center gap-[7px]">
          <span className={`size-[7px] rounded-full ${connDotClass(conn)}`} />
          <span className="min-w-[66px] text-xs text-muted-foreground">{connLabel}</span>
        </div>
      </div>
    </header>
  )
}

export default TopBar
