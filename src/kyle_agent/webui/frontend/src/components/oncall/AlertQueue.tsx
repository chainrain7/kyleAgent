/* 告警队列:firing 在前(resolved 灰化);点击填入标准诊断指令(与评测
 * goal 模板同构)。 */
import { orderedAlerts, useOncallStore, type AlertInfo } from "@/store/oncall-store"
import { useComposerStore } from "@/store/composer-store"

// 点击告警 → 输入框填标准诊断指令(与旧版 fillDiagnosis 逐字一致)
export function fillDiagnosis(a: AlertInfo): void {
  const { setDraft, requestFocus } = useComposerStore.getState()
  setDraft(
    "值班告警处理任务。\n" +
      `告警:${a.severity} | ${a.service} | ${a.summary}\n` +
      "要求:\n" +
      "1) 使用 service_status / logs_query / metrics_query / runbook_search 排查根因(注意区分表象与根因);\n" +
      "2) 若属于可自动恢复的故障,调用对应变更工具(service_restart / scale_up / log_cleanup)完成处置;\n" +
      "3) 若属于外部依赖、业务代码或需人工处理的问题,不要执行任何变更,只给处置建议;\n" +
      "4) 最后必须调用一次 submit_diagnosis 提交结构化结论。",
  )
  requestFocus()
}

function AlertQueue() {
  const alerts = useOncallStore((s) => s.alerts)
  const firingCount = alerts.filter((a) => a.status === "firing").length
  const ordered = orderedAlerts(alerts)

  return (
    <section className="flex flex-col gap-0.5">
      <div className="sec-label">
        告警队列
        <span
          className={`inline-grid min-w-[18px] place-items-center rounded-full border px-[5px] font-tabular font-mono text-[10.5px] font-semibold ${
            firingCount > 0
              ? "border-err/30 bg-err/10 text-err"
              : "border-border bg-muted text-muted-foreground/70"
          }`}
        >
          {firingCount}
        </span>
      </div>
      {!alerts.length ? (
        <div className="flex items-center gap-[7px] px-2 py-1 text-xs text-muted-foreground/70">
          <span className="size-1.5 rounded-full bg-ok" />
          <span>环境平稳 · 无活跃告警</span>
        </div>
      ) : (
        <div className="flex flex-col gap-[7px]">
          {ordered.map((a) => {
            const firing = a.status === "firing"
            return (
              <button
                key={a.alert_id}
                type="button"
                className={`relative overflow-hidden rounded-[10px] border px-2.5 pb-2 pt-2.5 text-left transition-colors duration-150 ${
                  firing
                    ? "border-err/30 bg-err/[0.045] hover:border-err/55 hover:bg-err/[0.08]"
                    : "border-border bg-card opacity-60 hover:opacity-80"
                }`}
                onClick={() => fillDiagnosis(a)}
              >
                {firing && <span className="absolute inset-y-0 left-0 w-0.5 bg-err" />}
                <div
                  className={`flex items-center gap-1.5 text-[10px] font-bold tracking-[0.06em] ${
                    firing ? "text-err" : "text-muted-foreground"
                  }`}
                >
                  {firing && <span className="size-1.5 rounded-full bg-err animate-breathe" />}
                  <span>{a.status.toUpperCase()}</span>
                  <span className="rounded border border-err/40 px-1 font-tabular font-mono text-[9.5px] leading-[1.5]">
                    {a.severity}
                  </span>
                  <span className="font-tabular ml-auto font-mono font-medium text-muted-foreground/50">
                    {String(a.ts || "").slice(11, 16)}
                  </span>
                </div>
                <div className="mb-0.5 mt-1 font-mono text-[11.5px]">{a.service}</div>
                <div className="text-[11.5px] leading-snug text-muted-foreground">{a.summary}</div>
              </button>
            )
          })}
        </div>
      )}
    </section>
  )
}

export default AlertQueue
