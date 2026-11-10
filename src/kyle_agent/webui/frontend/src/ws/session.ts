/* 会话引导与 WS 生命周期绑定:对齐旧版 app.js onOpen/onClose 语义——
 * - 首连:session.resume(有历史)或 session.create(全新);带 replay 锚点订阅
 * - 重连:会话保持,重新订阅(不带锚点,避免双推)
 * - 断线:banner 提示,client 内部 1.6s 重连循环
 * 必须且只在应用挂载时调用一次(重复订阅会双推事件)。 */
import { wsClient } from "@/ws/client"
import { useAppStore } from "@/store/app-store"
import { useSessionStore } from "@/store/session-store"
import { toast } from "@/store/toast-store"
import { asRecord, asStr } from "@/lib/format"

// 事件回调里追加 sysline(引导期 store action 尚未齐全,直接写 items)
function pushSysline(text: string, tone: "plain" | "ok" | "err" = "plain"): void {
  const s = useSessionStore.getState()
  const items = s.items.slice()
  items.push({ kind: "sysline", id: Date.now() + Math.random(), text, tone })
  useSessionStore.setState({ items })
}

// 绑定 onOpen/onClose/事件分发并发起连接;在 App useEffect 中调用一次
export function wireSessionLifecycle(): () => void {
  wsClient.onOpen = async () => {
    useAppStore.getState().setConn("connecting", "subscribing")
    const s = useSessionStore.getState()
    try {
      let replayAnchor: string | null = null
      if (!s.everConnected) {
        try {
          const r = await wsClient.send("session.resume", {})
          const sessionId = asStr(asRecord(r).session_id)
          const lastRunId = asStr(asRecord(r).last_run_id) || null
          useSessionStore.setState({ sessionId, lastRunId })
          replayAnchor = lastRunId
          pushSysline(`↩ 恢复会话 ${sessionId}`, "ok")
        } catch {
          const c = await wsClient.send("session.create", { mode: "chat" })
          const sessionId = asStr(asRecord(c).session_id)
          useSessionStore.setState({ sessionId })
          pushSysline(`✦ 新会话 ${sessionId}`, "ok")
        }
      } else {
        pushSysline("✓ 已重新连接,会话保持", "ok")
      }
      // 单次订阅:首次连接带 replay 锚点(避免二次订阅导致实时事件双推)
      const cur = useSessionStore.getState()
      const subParams: Record<string, unknown> = { topics: ["*"], scope: "global" }
      if (replayAnchor) subParams.replay_from_run = replayAnchor
      const sub = await wsClient.send("event.subscribe", subParams)
      const replayed = Number(asRecord(sub).replayed_count) || 0
      if (replayed > 0) {
        pushSysline(`↺ 回放了 ${replayed} 条历史事件`)
      }
      useSessionStore.setState({ everConnected: true })
      useAppStore
        .getState()
        .setConn(cur.running ? "busy" : "ready", cur.running ? "running" : "ready")
    } catch (err) {
      useAppStore.getState().setConn("down", "error")
      toast(`初始化失败: ${(err as Error).message || String(err)}`)
    }
  }

  wsClient.onClose = () => {
    useAppStore.getState().setConn("down", "reconnecting")
    // pending 已由 client 统一 reject;会话 id 保留,重连后 resume
  }

  // 发起连接(断线重连循环在 client 内部)
  wsClient.connect()

  // 所有 daemon 事件 → 会话状态机
  const offEvent = wsClient.on("*", (e) => useSessionStore.getState().handleEvent(e))
  return () => {
    offEvent()
    wsClient.onOpen = null
    wsClient.onClose = null
  }
}
