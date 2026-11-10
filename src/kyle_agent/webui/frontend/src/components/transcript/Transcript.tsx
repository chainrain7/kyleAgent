/* 会话流:滚动容器 + 近底部自动跟随(140px 阈值,与旧版一致);
 * 条目路由:子代理容器聚合其归属的工具卡;hero 在无真实内容时显示。 */
import { useEffect, useLayoutEffect, useRef } from "react"
import type { ReactNode } from "react"
import type { TranscriptItem } from "@/store/session-store"
import { hasTranscriptContent, useSessionStore } from "@/store/session-store"
import { Markdown } from "./Markdown"
import ToolCard from "./ToolCard"
import PermissionCard from "./PermissionCard"
import SubagentBox from "./SubagentBox"
import Hero from "./Hero"

// 单条目 → React 节点(子代理内工具卡由容器聚合,这里跳过)
function ItemNode({ item }: { item: TranscriptItem }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="my-3 flex justify-end animate-rise">
          <div className="max-w-[78%] whitespace-pre-wrap break-words rounded-xl rounded-br-sm border bg-secondary px-3.5 py-2 text-sm leading-[1.65]">
            {item.content}
          </div>
        </div>
      )
    case "assistant":
      return (
        <div className="my-3 flex gap-3 animate-rise">
          <div className="mt-px grid size-6 flex-none place-items-center rounded-[7px] border bg-muted text-[11px] font-bold select-none">
            K
          </div>
          <div className="min-w-0 flex-1 pt-px">
            <div className={item.streaming ? "assistant-streaming" : ""}>
              <Markdown>{item.raw}</Markdown>
              {item.streaming && <span className="caret" />}
            </div>
          </div>
        </div>
      )
    case "tool":
      return item.parentRunId === null ? (
        <div className="my-1">
          <ToolCard item={item} />
        </div>
      ) : null
    case "perm":
      return (
        <div className="my-3.5 animate-rise">
          <PermissionCard item={item} />
        </div>
      )
    case "subagent": {
      return <SubagentHost item={item} />
    }
    case "sysline":
      return (
        <div
          className={`my-1 py-0.5 font-mono text-[11.5px] select-none ${
            item.tone === "ok" ? "text-ok/80" : "text-muted-foreground/70"
          }`}
        >
          {item.text}
        </div>
      )
    case "runline":
      return (
        <div className="my-6 mb-4 flex items-center gap-3 font-mono text-[10.5px] tracking-[0.06em] text-muted-foreground/40 select-none">
          <span className="h-px flex-1 bg-border" />
          <span>run {item.runId}</span>
          <span className="h-px flex-1 bg-border" />
        </div>
      )
    default:
      return null
  }
}

// 子代理容器:聚合 parentRunId 归属的工具卡
function SubagentHost({ item }: { item: TranscriptItem & { kind: "subagent" } }) {
  const items = useSessionStore((s) => s.items)
  const children = items.filter(
    (it): it is TranscriptItem & { kind: "tool" } =>
      it.kind === "tool" && it.parentRunId === item.runId,
  )
  const nodes: ReactNode[] = children.map((c) => <ToolCard key={c.toolUseId} item={c} />)
  return (
    <div className="my-3">
      <SubagentBox item={item}>{nodes}</SubagentBox>
    </div>
  )
}

function Transcript() {
  const items = useSessionStore((s) => s.items)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const nearBottomRef = useRef(true)
  // 程序化置底写入的 scrollTop:用于区分用户滚动与自身 pin 触发的 scroll 事件
  const lastPinTopRef = useRef(0)
  // 滚动跟随:内容增长时,若用户位于底部 140px 内则贴底(与旧版一致)。
  // useLayoutEffect 在绘制前钉一次,再在下一帧补钉(流式内容分帧增高)
  const pin = () => {
    const sc = scrollerRef.current
    if (sc && nearBottomRef.current) {
      sc.scrollTop = sc.scrollHeight
      lastPinTopRef.current = sc.scrollTop
    }
  }
  useLayoutEffect(pin, [items])
  // 工具卡展开是 200ms 的 grid-rows 过渡:高度在动画期间渐进增长,
  // 单次 pin 追不上,连续钉 ~14 帧覆盖过渡窗口
  useEffect(() => {
    let n = 0
    let raf = 0
    const tick = () => {
      pin()
      if (++n < 14) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items])

  const hasContent = hasTranscriptContent(items)

  return (
    <div
      ref={scrollerRef}
      className="transcript-scroller min-h-0 flex-1 overflow-y-auto overscroll-contain"
      onScroll={(e) => {
        // 仅用户滚动改判近底状态;自身 pin 触发的 scroll 事件不改判
        const el = e.currentTarget
        if (Math.abs(el.scrollTop - lastPinTopRef.current) <= 2) return
        nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 140
      }}
    >
      <div className="mx-auto max-w-[740px] px-6 pb-3 pt-9">
        {!hasContent && <Hero />}
        {items.map((item) => (
          <ItemNode key={item.id} item={item} />
        ))}
      </div>
    </div>
  )
}

export default Transcript
