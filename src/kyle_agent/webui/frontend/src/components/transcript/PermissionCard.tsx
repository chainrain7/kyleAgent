/* 权限审批卡:y/a/n/d 键盘 + 点击;always 语义由 daemon 持久化;
 * 落定后按钮隐藏、verdict 展示,边框转绿/红。 */
import { AlertTriangle, Shield } from "lucide-react"
import type { PermItem } from "@/store/session-store"
import { useSessionStore } from "@/store/session-store"

// 四个选项:键位 / decision / 文案;allow_once 为首选动作(行动色主按钮)
const CHOICES: Array<{ key: string; decision: string; label: string; primary?: boolean }> = [
  { key: "y", decision: "allow_once", label: "允许一次", primary: true },
  { key: "a", decision: "always_allow", label: "始终允许" },
  { key: "n", decision: "deny_once", label: "拒绝" },
  { key: "d", decision: "always_deny", label: "始终拒绝" },
]

function PermissionCard({ item }: { item: PermItem }) {
  const respondPermission = useSessionStore((s) => s.respondPermission)
  const done = item.status !== "pending"

  return (
    <div
      className={`rounded-[10px] border bg-card p-4 transition-colors duration-300 ${
        item.status === "allow"
          ? "border-ok/35"
          : item.status === "deny"
            ? "border-err/35"
            : "border-warn/40 bg-warn/[0.03]"
      }`}
    >
      <div
        className={`mb-2.5 flex items-center gap-2 text-[13px] font-semibold ${
          done ? (item.status === "allow" ? "text-ok" : "text-err") : "text-warn"
        }`}
      >
        <Shield className="size-4" />
        <span>工具执行需要审批</span>
        <span className="rounded-md border bg-muted px-2 py-px font-mono text-xs font-semibold text-foreground">
          {item.toolName}
        </span>
      </div>
      <pre className="mb-3 max-h-[170px] overflow-y-auto whitespace-pre-wrap break-words rounded-lg border bg-background px-3 py-2 font-mono text-xs leading-relaxed text-muted-foreground">
        {item.preview}
      </pre>
      {item.impact && (
        <div className="mb-3 flex gap-2 rounded-lg border border-dashed border-warn/25 bg-warn/[0.04] px-3 py-2 text-xs leading-relaxed text-muted-foreground">
          <AlertTriangle className="mt-0.5 size-3.5 flex-none text-warn" />
          <div dangerouslySetInnerHTML={{ __html: item.impact }} />
        </div>
      )}
      {!done && (
        <div className="flex flex-wrap gap-1.5">
          {CHOICES.map((c) => (
            <button
              key={c.decision}
              type="button"
              disabled={done}
              onClick={() => respondPermission(item.toolUseId, c.decision)}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12.5px] font-medium transition-colors duration-150 ${
                c.primary
                  ? "border-primary bg-primary font-semibold text-primary-foreground hover:opacity-90"
                  : "border-border bg-secondary text-foreground hover:border-border-strong hover:bg-accent"
              }`}
            >
              <kbd className="rounded border border-border bg-background/60 px-1 py-px font-mono text-[10.5px] text-muted-foreground">
                {c.key}
              </kbd>
              <span>{c.label}</span>
            </button>
          ))}
        </div>
      )}
      {done && item.verdict && (
        <div className={`mt-2.5 text-[12.5px] font-medium ${item.status === "allow" ? "text-ok" : "text-err"}`}>
          {item.verdict}
        </div>
      )}
    </div>
  )
}

export default PermissionCard
