/* 应用骨架:顶栏 + 侧栏 + 主区(banner/会话流/输入区)+ toast。
 * 全局键盘:审批待决时 y/a/n/d 直接应答(与旧版一致,忽略修饰键)。 */
import { useEffect } from "react"
import ToastHost from "@/components/common/Toast"
import Composer from "@/components/composer/Composer"
import ReportOverlay from "@/components/oncall/ReportOverlay"
import Sidebar from "@/components/sidebar/Sidebar"
import TopBar from "@/components/topbar/TopBar"
import Transcript from "@/components/transcript/Transcript"
import { useAppStore } from "@/store/app-store"
import { useOncallStore } from "@/store/oncall-store"
import { useSessionStore } from "@/store/session-store"
import { wireSessionLifecycle } from "@/ws/session"

// 审批快捷键 → decision 映射(与旧版一致)
const PERM_KEYS: Record<string, string> = {
  y: "allow_once",
  a: "always_allow",
  n: "deny_once",
  d: "always_deny",
}

function App() {
  const conn = useAppStore((s) => s.conn)

  // WS 生命周期绑定:必须且只绑一次(重复订阅会双推事件)
  useEffect(() => wireSessionLifecycle(), [])

  // 值班面板轮询:3s 周期 + 挂载即拉一次(与旧版一致)
  useEffect(() => {
    const poll = () => useOncallStore.getState().poll()
    poll()
    const t = setInterval(poll, 3000)
    return () => clearInterval(t)
  }, [])

  // 全局审批快捷键(document 级,与旧版语义一致)
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const pending = useSessionStore.getState().pendingPermToolUseId
      if (!pending || ev.ctrlKey || ev.metaKey || ev.altKey) return
      const decision = PERM_KEYS[ev.key.toLowerCase()]
      if (decision) {
        ev.preventDefault()
        useSessionStore.getState().respondPermission(pending, decision)
      }
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [])

  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      <TopBar />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="relative flex min-w-0 flex-1 flex-col">
          {conn === "down" && (
            <div className="mx-6 mt-3 flex flex-none items-center gap-2 rounded-xl border border-warn/25 bg-warn/5 px-3.5 py-2 text-[12.5px] text-warn">
              与 daemon 的连接已断开,正在重连…(确认 kyle-core 在运行)
            </div>
          )}
          <Transcript />
          <Composer />
        </main>
      </div>
      <ToastHost />
      <ReportOverlay />
    </div>
  )
}

export default App
