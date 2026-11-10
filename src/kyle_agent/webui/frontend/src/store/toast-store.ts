/* 轻提示:对齐旧版 toast(3.8s 自动消失,info/kind 两态,单条覆盖)。 */
import { create } from "zustand"

interface ToastState {
  text: string | null
  kind: "info" | "error"
  // 展示一条提示;重复调用覆盖前一条
  show: (text: string, kind?: "info" | "error") => void
  hide: () => void
}

export const useToastStore = create<ToastState>((set) => ({
  text: null,
  kind: "error",
  show: (text, kind = "error") => set({ text, kind }),
  hide: () => set({ text: null }),
}))

// 命令式入口(事件处理/非组件上下文直接调用)
export function toast(text: string, kind: "info" | "error" = "error"): void {
  useToastStore.getState().show(text, kind)
}
