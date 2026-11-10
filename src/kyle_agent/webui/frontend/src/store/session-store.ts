/* 会话状态机:WS 事件 → transcript 渲染模型。
 * 事件语义逐项对齐旧版 app.js handleEvent;流式块改为单一 markdown
 * 容器(旧版按 token 追加 span 的写法在多 token 流下会重复渲染,属
 * 已知缺陷,此处修正)。 */
import { create } from "zustand"
import { wsClient, RpcError } from "@/ws/client"
import { toast } from "./toast-store"
import { useAppStore } from "./app-store"
import { asStr, fmtParams, previewOf } from "@/lib/format"
import { impactOf, useOncallStore } from "./oncall-store"

export type ToolState = "running" | "ok" | "err"

export interface UserItem {
  kind: "user"
  id: number
  content: string
}

// assistant 消息:streaming 期间末尾渲染光标,结束后整体定稿
export interface AssistantItem {
  kind: "assistant"
  id: number
  raw: string
  streaming: boolean
}

export interface ToolItem {
  kind: "tool"
  id: number
  toolUseId: string
  name: string
  paramsPreview: string
  paramsJson: string
  state: ToolState
  elapsedMs: number | null
  output: string | null
  outputIsError: boolean
  open: boolean
  // 归属:子代理 run_id → 渲染进其容器;null → 主流
  parentRunId: string | null
  startedAt: number
}

export interface PermItem {
  kind: "perm"
  id: number
  toolUseId: string
  toolName: string
  preview: string
  // 影响面文案(OnCallCopilot 拓扑推导,P3 注入;纯文案已由后端语义保证)
  impact: string | null
  status: "pending" | "allow" | "deny"
  verdict: string | null
}

export interface SubagentItem {
  kind: "subagent"
  id: number
  runId: string
  description: string
  status: string
}

export interface SyslineItem {
  kind: "sysline"
  id: number
  text: string
  tone: "plain" | "ok" | "err"
}

export interface RunlineItem {
  kind: "runline"
  id: number
  runId: string
}

export type TranscriptItem =
  | UserItem
  | AssistantItem
  | ToolItem
  | PermItem
  | SubagentItem
  | SyslineItem
  | RunlineItem

// 会话 store 形状(索引 map 不进渲染依赖,变更后以 set({}) 触发)
interface SessionState {
  sessionId: string | null
  runId: string | null
  lastRunId: string | null
  running: boolean
  everConnected: boolean
  items: TranscriptItem[]
  toolIndex: Map<string, number>
  subagentIndex: Map<string, number>
  pendingPermToolUseId: string | null
  streamIndex: number | null
  streamRaw: string
  contextPct: number | null
  cacheHitRate: number | null
  handleEvent: (e: Record<string, unknown>) => void
  sendMessage: (content: string) => void
  respondPermission: (toolUseId: string, decision: string) => Promise<void>
  newSession: () => Promise<void>
  compactSession: () => Promise<void>
  closeSession: () => Promise<void>
  resetForNewSession: () => void
}

let itemSeq = 0

// 事件处理后统一收尾流式块(与旧版"非 token 事件终止流"一致)
function breakStream(s: SessionState): Partial<SessionState> {
  if (s.streamIndex === null) return {}
  const idx = s.streamIndex
  const items = s.items.slice()
  const cur = items[idx]
  if (cur && cur.kind === "assistant") {
    items[idx] = { ...cur, streaming: false }
  }
  return { items, streamIndex: null, streamRaw: "" }
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessionId: null,
  runId: null,
  lastRunId: null,
  running: false,
  everConnected: false,
  items: [],
  toolIndex: new Map(),
  subagentIndex: new Map(),
  pendingPermToolUseId: null,
  streamIndex: null,
  streamRaw: "",
  contextPct: null,
  cacheHitRate: null,

  // WS 事件路由:type 分发;语义与旧版 app.js 一致
  handleEvent: (e) => {
    const t = asStr(e.type)
    const s = get()
    let patch: Partial<SessionState> = {}

    switch (t) {
      case "run.started": {
        const runId = asStr(e.run_id)
        const items = s.items.slice()
        items.push({ kind: "runline", id: ++itemSeq, runId })
        patch = {
          items,
          runId,
          lastRunId: runId,
          running: true,
        }
        useAppStore.getState().setConn("busy", "running")
        break
      }
      case "run.finished": {
        patch = { running: false, runId: null }
        useAppStore.getState().setConn("ready", "ready")
        const status = asStr(e.status, "success")
        if (status !== "success") {
          toast(`run 结束:${status}(${asStr(e.reason, "?")})`)
        }
        break
      }
      case "llm.token": {
        const token = asStr(e.token)
        if (!token) return
        if (s.streamIndex === null) {
          const items = s.items.slice()
          items.push({ kind: "assistant", id: ++itemSeq, raw: token, streaming: true })
          patch = { items, streamIndex: items.length - 1, streamRaw: token }
        } else {
          const idx = s.streamIndex
          const items = s.items.slice()
          const cur = items[idx]
          const raw = s.streamRaw + token
          if (cur && cur.kind === "assistant") {
            items[idx] = { ...cur, raw, streaming: true }
          }
          patch = { items, streamRaw: raw }
        }
        break
      }
      case "llm.usage": {
        const pct = Math.min(100, Math.round((Number(e.context_pct) || 0) * 100))
        patch = { contextPct: pct }
        const hit = e.cache_hit_rate
        if (hit !== undefined && hit !== null) {
          patch.cacheHitRate = Number(hit)
        }
        break
      }
      case "tool.call_started": {
        const toolUseId = asStr(e.tool_use_id)
        const eRunId = asStr(e.run_id)
        const parentRunId =
          eRunId && s.subagentIndex.has(eRunId) && eRunId !== s.runId ? eRunId : null
        const params = e.params
        const items = s.items.slice()
        items.push({
          kind: "tool",
          id: ++itemSeq,
          toolUseId,
          name: asStr(e.tool_name),
          paramsPreview: previewOf(params),
          paramsJson: fmtParams(params),
          state: "running",
          elapsedMs: null,
          output: null,
          outputIsError: false,
          open: false,
          parentRunId,
          startedAt: performance.now(),
        })
        s.toolIndex.set(toolUseId, items.length - 1)
        patch = { items }
        break
      }
      case "tool.call_finished":
      case "tool.call_failed": {
        const toolUseId = asStr(e.tool_use_id)
        const idx = s.toolIndex.get(toolUseId)
        if (idx === undefined) break
        const cur = s.items[idx]
        if (!cur || cur.kind !== "tool") break
        const isError = t === "tool.call_failed"
        const elapsed =
          e.elapsed_ms !== undefined ? Number(e.elapsed_ms) : Math.round(performance.now() - cur.startedAt)
        const output = e.output !== undefined ? asStr(e.output) : null
        const items = s.items.slice()
        items[idx] = {
          ...cur,
          state: isError ? "err" : "ok",
          elapsedMs: elapsed,
          output,
          outputIsError: isError,
          // 与旧版一致:有输出即自动展开
          open: output !== null ? true : cur.open,
        }
        patch = { items }
        break
      }
      case "permission.requested": {
        const params = e.params
        const oc = useOncallStore.getState()
        const items = s.items.slice()
        items.push({
          kind: "perm",
          id: ++itemSeq,
          toolUseId: asStr(e.tool_use_id),
          toolName: asStr(e.tool_name),
          preview: asStr(e.param_preview, previewOf(params)),
          // 变更工具按拓扑推导影响面(诊断/无参数工具为 null)
          impact: impactOf(asStr(e.tool_name), params, oc.topology, oc.services),
          status: "pending",
          verdict: null,
        })
        patch = { ...breakStream(s), items, pendingPermToolUseId: asStr(e.tool_use_id) }
        break
      }
      case "permission.granted":
      case "permission.denied": {
        const toolUseId = asStr(e.tool_use_id)
        if (s.pendingPermToolUseId === toolUseId) {
          const idx = s.items.findIndex(
            (it) => it.kind === "perm" && it.toolUseId === toolUseId,
          )
          if (idx >= 0) {
            const allow = t === "permission.granted"
            const items = s.items.slice()
            const cur = items[idx]
            if (cur.kind === "perm") {
              items[idx] = {
                ...cur,
                status: allow ? "allow" : "deny",
                verdict: allow ? "✓ 已放行(自动)" : "✕ 已拒绝(超时/断连)",
              }
            }
            patch = { items, pendingPermToolUseId: null }
          }
        }
        break
      }
      case "subagent.started": {
        const runId = asStr(e.run_id)
        const items = s.items.slice()
        items.push({
          kind: "subagent",
          id: ++itemSeq,
          runId,
          description: asStr(e.description),
          status: "running",
        })
        s.subagentIndex.set(runId, items.length - 1)
        patch = { ...breakStream(s), items }
        break
      }
      case "subagent.finished": {
        const runId = asStr(e.run_id)
        const idx = s.subagentIndex.get(runId)
        if (idx !== undefined) {
          const cur = s.items[idx]
          if (cur && cur.kind === "subagent") {
            const items = s.items.slice()
            items[idx] = { ...cur, status: asStr(e.status, "finished") }
            patch = { items }
          }
        }
        break
      }
      case "context.compacted": {
        const items = s.items.slice()
        items.push({
          kind: "sysline",
          id: ++itemSeq,
          tone: "ok",
          text: `✦ 上下文已压缩:${asStr(e.original_tokens)} → ${asStr(e.summary_tokens)} tokens`,
        })
        patch = { ...breakStream(s), items }
        break
      }
      case "skill.invoked": {
        const items = s.items.slice()
        items.push({
          kind: "sysline",
          id: ++itemSeq,
          tone: "plain",
          text: `⚡ skill ${asStr(e.skill_name)}(${asStr(e.arguments)})`,
        })
        patch = { items }
        break
      }
      case "session.waiting_for_input": {
        patch = { running: false }
        useAppStore.getState().setConn("ready", "ready")
        break
      }
      case "session.resumed":
        // onOpen 已渲染恢复提示,静默避免重复
        break
      case "session.closed": {
        patch = { sessionId: null }
        break
      }
      case "step.started":
        // 步进不单独渲染,减少噪音
        break
      default:
        break
    }

    // 非 token 事件一律终止当前流式块
    if (t !== "llm.token") {
      patch = { ...breakStream(s), ...patch }
    }
    if (Object.keys(patch).length > 0) set(patch)
  },

  // 发送目标消息:不 await run 完成(过程由事件实时渲染),只捕获发送层错误
  sendMessage: (content) => {
    const s = get()
    const trimmed = content.trim()
    if (!trimmed) return
    if (s.running) {
      toast("上一条消息还在执行中(session busy)", "info")
      return
    }
    const items = s.items.slice()
    items.push({ kind: "user", id: ++itemSeq, content: trimmed })
    set({ items })
    wsClient
      .send("session.send_message", { session_id: s.sessionId, content: trimmed })
      .catch((err: Error) => {
        toast(err.message || String(err))
        if (err instanceof RpcError && err.code === -32010) {
          set({ sessionId: null })
        }
      })
  },

  // 审批应答:成功后卡片落定;失败 toast 并保留(与旧版一致清 pending)
  respondPermission: async (toolUseId, decision) => {
    const s = get()
    if (s.pendingPermToolUseId !== toolUseId) return
    const idx = s.items.findIndex((it) => it.kind === "perm" && it.toolUseId === toolUseId)
    try {
      await wsClient.send("permission.respond", { tool_use_id: toolUseId, decision })
      const allow = decision.startsWith("allow")
      if (idx >= 0) {
        const items = s.items.slice()
        const cur = items[idx]
        if (cur.kind === "perm") {
          items[idx] = {
            ...cur,
            status: allow ? "allow" : "deny",
            verdict: allow ? `✓ 已放行(${decision})` : `✕ 已拒绝(${decision})`,
          }
        }
        set({ items })
      }
    } catch (err) {
      toast(`审批发送失败: ${(err as Error).message || String(err)}`)
    }
    set({ pendingPermToolUseId: null })
  },

  // 新会话:创建 + 清空 transcript(等价旧版 reload,但不闪屏)
  newSession: async () => {
    try {
      const c = await wsClient.send("session.create", { mode: "chat" })
      get().resetForNewSession()
      set({ sessionId: asStr((c as Record<string, unknown>).session_id) })
    } catch (err) {
      toast((err as Error).message || String(err))
    }
  },

  // 压缩上下文
  compactSession: async () => {
    try {
      const r = await wsClient.send("session.compact", { session_id: get().sessionId })
      const saved = (r as Record<string, unknown>).saved_tokens
      toast(`压缩完成,节省 ${String(saved)} tokens`, "info")
    } catch (err) {
      toast((err as Error).message || String(err))
    }
  },

  // 关闭会话(session.closed 事件回写 sessionId=null)
  closeSession: async () => {
    try {
      await wsClient.send("session.close", { session_id: get().sessionId })
    } catch (err) {
      toast((err as Error).message || String(err))
    }
  },

  // 新会话前清空运行时状态(hero 随 items 清空回归)
  resetForNewSession: () => {
    set({
      items: [],
      toolIndex: new Map(),
      subagentIndex: new Map(),
      pendingPermToolUseId: null,
      streamIndex: null,
      streamRaw: "",
      contextPct: null,
      cacheHitRate: null,
      lastRunId: null,
      running: false,
      runId: null,
    })
  },
}))

// 是否存在真实内容(决定 hero 空状态是否显示;sysline/runline 不清场)
export function hasTranscriptContent(items: TranscriptItem[]): boolean {
  return items.some(
    (it) =>
      it.kind === "user" ||
      it.kind === "assistant" ||
      it.kind === "tool" ||
      it.kind === "perm" ||
      it.kind === "subagent",
  )
}

// dev-only 事件注入钩子:供浏览器控制台演练子代理等难以离线触发的事件
// 渲染路径(生产构建剔除,不影响运行时行为)
if (import.meta.env.DEV) {
  ;(window as unknown as Record<string, unknown>).__kyleInjectEvent = (
    e: Record<string, unknown>,
  ) => useSessionStore.getState().handleEvent(e)
}
