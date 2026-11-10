/* Hero 空状态:值班台开场——问候行、标题、环境摘要(P3 接 oncall 数据)、
 * 2×2 建议卡(点击填入输入框)。真实内容出现后由 Transcript 隐藏。 */
import { useEffect, useState } from "react"
import { Activity, FileText, FolderTree, PenLine } from "lucide-react"
import { heroGreet } from "@/lib/format"
import { useComposerStore } from "@/store/composer-store"
import { useOncallStore } from "@/store/oncall-store"

// 建议卡:标题 / 描述 / 填入的 prompt(与旧版一致)
const CARDS = [
  {
    icon: FileText,
    title: "总结当前项目",
    desc: "阅读 README,三句话讲清这个项目是做什么的",
    prompt: "阅读 README.md 并用三句话总结这个项目",
  },
  {
    icon: FolderTree,
    title: "梳理代码结构",
    desc: "扫一遍源码目录,标出最核心的三个模块",
    prompt: "列出 src 目录结构,指出最核心的三个模块",
  },
  {
    icon: PenLine,
    title: "试一次文件写入",
    desc: "修改 hello.txt,体验工具审批与执行回执",
    prompt: "把 hello.txt 的内容改成 kyle rulez 并确认写入成功",
  },
  {
    icon: Activity,
    title: "值班交接摘要",
    desc: "盘点服务健康与告警,生成交接要点",
    prompt: "检查当前各服务健康状态与告警队列,输出一份值班交接摘要(现状/风险/建议关注点)",
  },
]

function Hero() {
  const [greet, setGreet] = useState(() => heroGreet(new Date()))
  const setDraft = useComposerStore((s) => s.setDraft)
  const available = useOncallStore((s) => s.available)
  const services = useOncallStore((s) => s.services)
  const alerts = useOncallStore((s) => s.alerts)
  // 分钟级刷新问候行(值班台的"交接时间"感)
  useEffect(() => {
    const t = setInterval(() => setGreet(heroGreet(new Date())), 30_000)
    return () => clearInterval(t)
  }, [])

  // 环境摘要行:与顶栏徽章同源(旧版 renderEnvSummary 语义)
  const names = Object.keys(services)
  const down = names.filter((n) => services[n] && services[n].status !== "running")
  const firing = alerts.filter((a) => a.status === "firing")
  const alerting = firing.length > 0 || down.length > 0

  return (
    <div className="hero-dots relative pb-[6vh] pt-[13vh]">
      <p className="relative mb-2.5 font-mono text-xs tracking-[0.08em] text-muted-foreground/70">{greet}</p>
      <h1 className="relative mb-2.5 text-[27px] font-bold leading-tight tracking-tight">值班台已就绪</h1>
      <p className="relative mb-8 flex items-center gap-2 text-[13px] text-muted-foreground">
        <span
          className={`size-[7px] rounded-full ${
            available ? (alerting ? "bg-err animate-breathe" : "bg-ok") : "bg-muted-foreground/40"
          }`}
        />
        {!available && <span>演练环境未初始化 · 通用助手模式</span>}
        {available && firing.length > 0 && (
          <span>
            <b className="font-semibold text-foreground">{firing.length} 条活跃告警</b>
            {" — "}
            {firing[0].summary || firing[0].service}
          </span>
        )}
        {available && firing.length === 0 && down.length > 0 && (
          <span>
            <b className="font-semibold text-foreground">{down.length} 个服务异常</b>
            {" — "}
            {down.join(", ")}
          </span>
        )}
        {available && !alerting && (
          <span>
            <b className="font-semibold text-foreground">{names.length} 个服务</b>
            全部运行中 · 0 活跃告警 · 环境平稳
          </span>
        )}
      </p>
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        {CARDS.map((c) => (
          <button
            key={c.title}
            type="button"
            className="flex flex-col items-start gap-1 rounded-xl border bg-card px-4 pb-3 pt-3.5 text-left transition-all duration-150 hover:-translate-y-px hover:border-border-strong hover:bg-accent"
            onClick={() => setDraft(c.prompt)}
          >
            <span className="mb-1.5 grid size-7 place-items-center rounded-lg border bg-muted text-muted-foreground">
              <c.icon className="size-3.5" />
            </span>
            <span className="text-[13.5px] font-semibold">{c.title}</span>
            <span className="text-xs leading-relaxed text-muted-foreground/80">{c.desc}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

export default Hero
