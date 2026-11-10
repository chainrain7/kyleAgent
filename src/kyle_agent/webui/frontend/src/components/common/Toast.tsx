/* 轻提示宿主:固定顶部居中,3.8s 自动消失(info=中性,error=强调边框)。 */
import { useEffect } from "react"
import { useToastStore } from "@/store/toast-store"

function ToastHost() {
  const text = useToastStore((s) => s.text)
  const kind = useToastStore((s) => s.kind)
  const hide = useToastStore((s) => s.hide)

  useEffect(() => {
    if (!text) return
    const t = setTimeout(hide, 3800)
    return () => clearTimeout(t)
  }, [text, hide])

  if (!text) return null
  return (
    <div
      className={`fixed left-1/2 top-[66px] z-60 max-w-[68vw] -translate-x-1/2 rounded-xl border px-4 py-2 text-[13px] shadow-lg ${
        kind === "info"
          ? "border-border-strong bg-accent text-foreground"
          : "border-err/30 bg-card text-foreground"
      }`}
      role="status"
    >
      {text}
    </div>
  )
}

export default ToastHost
