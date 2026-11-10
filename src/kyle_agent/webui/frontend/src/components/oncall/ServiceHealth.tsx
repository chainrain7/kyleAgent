/* 服务健康列表:状态点(绿/琥珀/红)+ 副本数 + 错误率(≥5% 红色加重)。 */
import { useOncallStore } from "@/store/oncall-store"

function ServiceHealth() {
  const services = useOncallStore((s) => s.services)

  return (
    <section className="flex flex-col gap-0.5">
      <div className="sec-label">服务健康</div>
      <div>
        {Object.entries(services).map(([name, s]) => {
          const cls = s.status === "down" ? "down" : s.status !== "running" ? "warn" : ""
          const err = Number(s.error_rate || 0)
          return (
            <div
              key={name}
              className="flex items-center gap-2 rounded-lg px-2 py-[5.5px] text-xs text-foreground/80 hover:bg-accent"
            >
              <span
                className={`size-[7px] flex-none rounded-full ${
                  cls === "down" ? "bg-err" : cls === "warn" ? "bg-warn" : "bg-ok"
                }`}
              />
              <span className="font-mono text-[11.5px]">{name}</span>
              <span className="font-tabular ml-auto font-mono text-[10.5px] text-muted-foreground">
                {s.replicas} 副本 · err{" "}
                {err >= 0.05 ? (
                  <b className="font-tabular font-semibold text-warn">{(err * 100).toFixed(0)}%</b>
                ) : (
                  (err * 100).toFixed(1) + "%"
                )}
              </span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

export default ServiceHealth
