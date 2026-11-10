/* 展示格式化纯函数:从旧版 app.js 逐字移植语义。 */

// 工具参数单行预览:优先 command/path/query/content/prompt/description 键,超 90 字截断
export function previewOf(params: unknown): string {
  try {
    const p = typeof params === "string" ? JSON.parse(params) : params
    if (p && typeof p === "object") {
      for (const k of ["command", "path", "query", "content", "prompt", "description"]) {
        const v = (p as Record<string, unknown>)[k]
        if (typeof v === "string") {
          const s = v.replace(/\s+/g, " ").trim()
          return `${k}=${s.length > 90 ? s.slice(0, 90) + "…" : s}`
        }
      }
    }
    const s = JSON.stringify(p)
    return s.length > 90 ? s.slice(0, 90) + "…" : s
  } catch {
    return String(params).slice(0, 90)
  }
}

// 参数区完整展示:对象美化缩进,字符串原样
export function fmtParams(params: unknown): string {
  return typeof params === "string" ? params : JSON.stringify(params, null, 2)
}

// hero 问候行:日期 + 星期 + 时段(值班台的"交接时间"感)
export function heroGreet(now: Date): string {
  const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][now.getDay()]
  const h = now.getHours()
  const slot = h < 5 ? "凌晨" : h < 12 ? "早上" : h < 18 ? "下午" : "晚上"
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${now.getMonth() + 1}月${now.getDate()}日 ${week} · ${slot}好 · ${pad(h)}:${pad(now.getMinutes())}`
}

// 事件参数对象安全取值(事件里可能是任意 JSON)
export function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {}
}

// 安全字符串取值
export function asStr(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback
}
