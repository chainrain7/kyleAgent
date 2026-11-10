/* WS ↔ JSON-RPC 客户端:协议帧格式逐字对齐旧版 app.js 的实现——
 * - 命令:{ jsonrpc: "2.0", id: "w<n>", method, params },id 关联 pending,300s 超时
 * - 事件:{"kind":"event","event":{...}} 按 type 路由给订阅者
 * - 断线:close 后 1600ms 重连退避;断开时全部 pending reject(connection lost)
 * - daemon 不在线时桥接器以 1013 关闭,前端进入同样的重连循环
 */

// 事件处理器注册表类型:多个组件可按 type 订阅,"*" 兜底
type EventHandler = (event: Record<string, unknown>) => void

interface PendingEntry {
  resolve: (result: Record<string, unknown>) => void
  reject: (err: Error) => void
  timer: ReturnType<typeof setTimeout>
}

// JSON-RPC 错误:携带 daemon 侧错误码(如 -32010 session 不存在)
export class RpcError extends Error {
  code: number
  constructor(code: number, message: string) {
    super(message)
    this.code = code
  }
}

export class WsRpcClient {
  private ws: WebSocket | null = null
  private pending = new Map<string, PendingEntry>()
  private reqSeq = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private handlers = new Map<string, Set<EventHandler>>()
  // 连接生命周期回调(由 store/UI 层注入,client 不感知 React)
  onOpen: (() => void) | null = null
  onClose: (() => void) | null = null

  // 发起连接:同源 /ws(生产经桥接器,开发经 vite proxy)。
  // 已有连接处于 CONNECTING/OPEN 时跳过(React StrictMode 会双挂载 effect,
  // 重复 connect 会让 onOpen 的 send 撞上未就绪的套接字)
  connect(): void {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return
    const proto = location.protocol === "https:" ? "wss" : "ws"
    const ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws = ws
    ws.onopen = () => this.onOpen?.()
    ws.onclose = (ev) => this.handleClose(ev)
    ws.onerror = () => ws.close()
    ws.onmessage = (ev) => {
      try {
        this.dispatch(JSON.parse(ev.data as string))
      } catch {
        /* 忽略非 JSON 行 */
      }
    }
  }

  // 断线处理:拒绝全部 pending → 通知 UI → 1.6s 后重连
  private handleClose(_ev: CloseEvent): void {
    this.ws = null
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer)
      reject(new Error("connection lost"))
    }
    this.pending.clear()
    this.onClose?.()
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = setTimeout(() => this.connect(), 1600)
  }

  // 事件订阅:精确 type 或 "*" 兜底;返回解绑函数
  on(type: string, handler: EventHandler): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    set.add(handler)
    return () => set.delete(handler)
  }

  // 响应/事件分发:含 jsonrpc 字段按 id 匹配 pending,事件按 type 路由
  private dispatch(msg: Record<string, unknown>): void {
    if (msg.jsonrpc) {
      const p = this.pending.get(msg.id as string)
      if (!p) return
      this.pending.delete(msg.id as string)
      clearTimeout(p.timer)
      const err = msg.error as { code: number; message: string } | undefined
      if (err) {
        p.reject(new RpcError(err.code, `[${err.code}] ${err.message}`))
      } else {
        p.resolve((msg.result as Record<string, unknown>) || {})
      }
    } else if (msg.kind === "event") {
      const event = (msg.event as Record<string, unknown>) || {}
      const type = (event.type as string) || ""
      this.handlers.get(type)?.forEach((h) => h(event))
      this.handlers.get("*")?.forEach((h) => h(event))
    }
  }

  // 发送 JSON-RPC 命令并等待响应(300s 超时与旧版一致)
  send(method: string, params?: Record<string, unknown>): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("not connected"))
        return
      }
      const id = `w${++this.reqSeq}`
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`${method}: timeout`))
        }
      }, 300_000)
      this.pending.set(id, { resolve, reject, timer })
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} }))
    })
  }

  // 主动关闭并停止重连(供测试/卸载使用)
  dispose(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = null
    this.handlers.clear()
    this.ws?.close()
    this.ws = null
  }
}

// 单例:整页一个连接(与旧版一致)
export const wsClient = new WsRpcClient()
