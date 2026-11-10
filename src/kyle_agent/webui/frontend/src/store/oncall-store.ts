/* OnCallCopilot 值班面板状态:/api/state 3s 轮询 + 演练操作 + 报告读取。
 * 数据语义与旧版 oncall-panel.js 一致(env 未初始化降级通用助手模式)。 */
import { create } from "zustand"
import { toast } from "./toast-store"
import { asRecord, asStr } from "@/lib/format"

// 服务健康条目(/api/state services 字段)
export interface ServiceInfo {
  status: string
  replicas: number
  error_rate: number
  [key: string]: unknown
}

// 告警条目
export interface AlertInfo {
  alert_id: string
  service: string
  severity: string
  summary: string
  ts: string
  status: string
}

// 故障剧本条目
export interface ScenarioInfo {
  id: string
  severity: string
  summary: string
  service: string
  recoverable: boolean
}

interface OncallState {
  available: boolean
  services: Record<string, ServiceInfo>
  alerts: AlertInfo[]
  topology: Record<string, string[]>
  externals: string[]
  reports: string[]
  scenarios: ScenarioInfo[]
  // 报告浮层
  reportName: string | null
  reportContent: string | null
  reportLoading: boolean
  poll: () => Promise<void>
  inject: (faultId: string) => Promise<void>
  resetEnv: () => Promise<void>
  openReport: (name: string) => Promise<void>
  closeReport: () => void
}

// 变更工具影响面文案:按拓扑推导依赖方,规则与旧版 impactOf 一致
export function impactOf(
  toolName: string,
  params: unknown,
  topology: Record<string, string[]>,
  services: Record<string, ServiceInfo>,
): string | null {
  if (!params || typeof params !== "object") return null
  const p = params as Record<string, unknown>
  const svc = typeof p.service === "string" ? p.service : ""
  if (!svc) return null
  const dependents = Object.keys(topology).filter(
    (n) => n !== svc && (topology[n] || []).includes(svc),
  )
  const replicas = services[svc]?.replicas
  if (toolName === "service_restart") {
    const r = replicas != null ? replicas : "?"
    return dependents.length
      ? `<b>影响面</b>:滚动重启 ${r} 副本约 45s;依赖方 [${dependents.join(", ")}] 的健康检查将瞬时失败(约 30s)。`
      : `<b>影响面</b>:滚动重启 ${r} 副本约 45s;无内部依赖方受影响。`
  }
  if (toolName === "scale_up") {
    const delta = Number(p.delta || 1)
    const r = replicas != null ? Number(replicas) : null
    const growth = r ? ` (+${Math.round((delta / Math.max(r, 1)) * 100)}%/服务)` : ""
    return `<b>影响面</b>:副本 ${r ?? "?"} → ${r != null ? Math.min(6, r + delta) : "?"}${growth},上限 6。`
  }
  if (toolName === "log_cleanup") {
    return `<b>影响面</b>:截断历史日志仅保留最近 50 行以释放磁盘,不影响运行中的进程。`
  }
  return null
}

export const useOncallStore = create<OncallState>((set, get) => ({
  available: false,
  services: {},
  alerts: [],
  topology: {},
  externals: [],
  reports: [],
  scenarios: [],
  reportName: null,
  reportContent: null,
  reportLoading: false,

  // 轮询 /api/state:不可用则降级;报告清单变化时轻提示
  poll: async () => {
    try {
      const r = await fetch("/api/state")
      const data = asRecord(await r.json())
      if (!data.available) {
        if (get().available) {
          set({ available: false, reportName: null, reportContent: null })
        }
        return
      }
      const services = (data.services as Record<string, ServiceInfo>) || {}
      const alerts = (data.alerts as AlertInfo[]) || []
      const reports = (data.reports as string[]) || []
      const fresh = get().available && get().reports.join(",") !== "" && reports.join(",") !== get().reports.join(",")
      set({
        available: true,
        services,
        alerts,
        topology: (data.topology as Record<string, string[]>) || {},
        externals: (data.externals as string[]) || [],
        reports,
        scenarios: (data.scenarios as ScenarioInfo[]) || [],
      })
      if (fresh) toast("处置报告已生成", "info")
    } catch {
      /* 桥接不可达时静默,下轮重试 */
    }
  },

  // 注入剧本(后端先重置保可复现),返回新告警
  inject: async (faultId) => {
    try {
      const r = await fetch("/api/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fault_id: faultId }),
      })
      const data = asRecord(await r.json())
      if (!r.ok) throw new Error(asStr(data.detail, "inject failed"))
      toast(`已注入:${asStr(data.service)} · ${asStr(data.severity)}(alert ${asStr(data.alert_id)})`, "info")
      await get().poll()
    } catch (e) {
      toast("注入失败: " + (e as Error).message)
    }
  },

  // 重置模拟环境到基线
  resetEnv: async () => {
    try {
      await fetch("/api/reset", { method: "POST" })
      toast("模拟环境已重置到基线", "info")
      await get().poll()
    } catch (e) {
      toast("重置失败: " + (e as Error).message)
    }
  },

  // 读取处置报告原文(markdown)
  openReport: async (name) => {
    set({ reportLoading: true, reportName: name, reportContent: null })
    try {
      const r = await fetch("/api/report/" + encodeURIComponent(name))
      const data = asRecord(await r.json())
      if (!r.ok) throw new Error(asStr(data.detail, "not found"))
      set({ reportContent: asStr(data.content), reportLoading: false })
    } catch (e) {
      set({ reportLoading: false })
      toast("报告读取失败: " + (e as Error).message)
    }
  },

  closeReport: () => set({ reportName: null, reportContent: null }),
}))

// firing 告警在前、resolved 在后的稳定排序(firing 最多展示 8 条)
export function orderedAlerts(alerts: AlertInfo[]): AlertInfo[] {
  const firing = alerts.filter((a) => a.status === "firing")
  return [...firing, ...alerts.filter((a) => a.status !== "firing")].slice(0, 8)
}
