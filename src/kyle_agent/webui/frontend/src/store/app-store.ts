/* 全局应用状态(zustand):连接状态与主题。
 * 事件流/会话/工具卡等运行时状态在 P2 按事件语义拆分进各自的 store。 */
import { create } from "zustand"
import { initialTheme, toggleTheme, type Theme } from "@/lib/theme"

// 连接状态机:connecting(订阅中)→ ready / busy(run 执行中)→ down(断线)
export type ConnState = "connecting" | "ready" | "busy" | "down"

interface AppState {
  conn: ConnState
  // 顶栏连接文案:connecting / subscribing / ready / running / reconnecting
  connLabel: string
  theme: Theme
  setConn: (conn: ConnState, label?: string) => void
  switchTheme: () => void
}

// 全局 store:连接指示与主题由顶栏消费
export const useAppStore = create<AppState>((set) => ({
  conn: "connecting",
  connLabel: "connecting",
  theme: initialTheme(),
  setConn: (conn, label) => set({ conn, connLabel: label ?? conn }),
  switchTheme: () => {
    const next = toggleTheme()
    set({ theme: next })
  },
}))
