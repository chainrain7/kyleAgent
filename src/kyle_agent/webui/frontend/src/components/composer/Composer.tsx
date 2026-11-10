/* 输入区:自动增高(≤180px)、Enter 发送 / Shift+Enter 换行、
 * 断线禁用发送;审批快捷键在 App 全局处理。 */
import { useEffect, useRef } from "react"
import { ArrowUp } from "lucide-react"
import { useAppStore } from "@/store/app-store"
import { useComposerStore } from "@/store/composer-store"
import { useSessionStore } from "@/store/session-store"

function Composer() {
  const draft = useComposerStore((s) => s.draft)
  const setDraft = useComposerStore((s) => s.setDraft)
  const focusNonce = useComposerStore((s) => s.focusNonce)
  const sendMessage = useSessionStore((s) => s.sendMessage)
  const conn = useAppStore((s) => s.conn)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // 外部请求聚焦(告警卡点击填入诊断指令后)
  useEffect(() => {
    if (focusNonce > 0) inputRef.current?.focus()
  }, [focusNonce])

  // 自动增高:重置后取 scrollHeight,上限 180px(与旧版一致)
  const autoResize = () => {
    const el = inputRef.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }

  // 发送:清空草稿并复位高度
  const doSend = () => {
    if (!draft.trim()) return
    sendMessage(draft)
    setDraft("")
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (el) el.style.height = "auto"
    })
  }

  // 与旧版一致:仅断线/未就绪时禁用;草稿为空由 sendMessage 兜底 no-op
  const disabled = conn === "down" || conn === "connecting"

  return (
    <div className="flex-none px-6 pb-3.5 pt-2">
      <div className="mx-auto flex max-w-[740px] items-end gap-2 rounded-lg border bg-card px-4 py-2 transition-all duration-200 focus-within:border-foreground/25 focus-within:ring-2 focus-within:ring-ring/20">
        <textarea
          ref={inputRef}
          rows={1}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value)
            autoResize()
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault()
              doSend()
            }
          }}
          placeholder="给 OnCallCopilot 一个目标,或点击左侧告警发起诊断…"
          className="max-h-[180px] w-full flex-1 resize-none bg-transparent py-1.5 text-[14.5px] leading-relaxed outline-none placeholder:text-muted-foreground/50"
        />
        <button
          type="button"
          title="发送"
          disabled={disabled}
          onClick={doSend}
          className="grid size-[34px] flex-none place-items-center rounded-lg bg-primary text-primary-foreground transition-opacity duration-200 hover:opacity-90 active:scale-95 disabled:opacity-20"
        >
          <ArrowUp className="size-4" strokeWidth={2.2} />
        </button>
      </div>
      <div className="mx-auto mt-2 flex max-w-[740px] items-center justify-center gap-1 text-[11.5px] text-muted-foreground/60">
        <kbd className="kbd">Enter</kbd> 发送 ·<kbd className="kbd">⇧ Enter</kbd> 换行 · 审批时按
        <kbd className="kbd">y</kbd>
        <kbd className="kbd">a</kbd>
        <kbd className="kbd">n</kbd>
        <kbd className="kbd">d</kbd>
      </div>
    </div>
  )
}

export default Composer
