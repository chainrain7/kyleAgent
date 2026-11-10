/* 输入框草稿:hero 卡与告警卡都能"填入而不发送",需要跨组件写 draft;
 * focusNonce 递增触发输入框聚焦(告警卡点击后的老语义)。 */
import { create } from "zustand"

interface ComposerState {
  draft: string
  focusNonce: number
  setDraft: (draft: string) => void
  requestFocus: () => void
}

export const useComposerStore = create<ComposerState>((set) => ({
  draft: "",
  focusNonce: 0,
  setDraft: (draft) => set({ draft }),
  requestFocus: () => set((s) => ({ focusNonce: s.focusNonce + 1 })),
}))

