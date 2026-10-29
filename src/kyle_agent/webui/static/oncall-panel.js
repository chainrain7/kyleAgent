/* OnCallCopilot 值班面板(Phase 4)
 * 依赖 app.js 的 el/toast/autoResize/mdRender 与全局 DOM 节点。
 * 数据通道:/api/state(轮询)、/api/inject|reset、/api/report/{name}。
 */
"use strict";

const oc = {
  available: false,
  topology: {},
  externals: [],
  services: {},
  scenarios: [],
  lastReports: "",
  menuBuilt: false,
};

// 生成变更工具的影响面文案(依拓扑推导;规则见 docs/product/mockups.md)
function impactOf(toolName, params) {
  if (!oc.available || !params || typeof params !== "object") return null;
  const svc = params.service;
  if (!svc) return null;
  const dependents = Object.keys(oc.topology).filter(
    (n) => n !== svc && (oc.topology[n] || []).includes(svc)
  );
  const replicas = (oc.services[svc] || {}).replicas;
  if (toolName === "service_restart") {
    const r = replicas != null ? replicas : "?";
    return dependents.length
      ? `<b>影响面</b>:滚动重启 ${r} 副本约 45s;依赖方 [${dependents.join(", ")}] 的健康检查将瞬时失败(约 30s)。`
      : `<b>影响面</b>:滚动重启 ${r} 副本约 45s;无内部依赖方受影响。`;
  }
  if (toolName === "scale_up") {
    const delta = Number(params.delta || 1);
    const r = replicas != null ? Number(replicas) : null;
    const growth = r ? ` (+${Math.round((delta / Math.max(r, 1)) * 100)}%/服务)` : "";
    return `<b>影响面</b>:副本 ${r ?? "?"} → ${r != null ? Math.min(6, r + delta) : "?"}${growth},上限 6。`;
  }
  if (toolName === "log_cleanup") {
    return `<b>影响面</b>:截断历史日志仅保留最近 50 行以释放磁盘,不影响运行中的进程。`;
  }
  return null;
}

// 值班面板可见性
function setOncallVisible(v) {
  for (const id of ["drillRow", "alertsSec", "healthSec", "reportsSec"]) {
    document.getElementById(id).classList.toggle("hidden", !v);
  }
}

// 渲染告警队列(firing 在前;点击填入标准诊断指令)
function renderAlerts(alerts) {
  const list = document.getElementById("alertList");
  const firing = alerts.filter((a) => a.status === "firing");
  document.getElementById("alertCount").textContent = String(firing.length);
  if (!alerts.length) {
    list.innerHTML = "";
    list.appendChild(el("div", "sysline", "无告警 · 环境健康"));
    return;
  }
  const ordered = [...firing, ...alerts.filter((a) => a.status !== "firing")];
  list.innerHTML = "";
  for (const a of ordered.slice(0, 8)) {
    const card = el("button", "alert-card" + (a.status === "firing" ? "" : " resolved"));
    const row1 = el("div", "row1");
    row1.appendChild(el("span", "dot"));
    row1.appendChild(el("span", null, a.status.toUpperCase()));
    const time = el("span", "stat", String(a.ts || "").slice(11, 16));
    row1.appendChild(time);
    card.appendChild(row1);
    card.appendChild(el("div", "svc", `${a.service} · ${a.severity}`));
    card.appendChild(el("div", "sum", a.summary || ""));
    card.addEventListener("click", () => fillDiagnosis(a));
    list.appendChild(card);
  }
}

// 点击告警 → 输入框填标准诊断指令(与评测 goal 模板同构)
function fillDiagnosis(a) {
  const input = document.getElementById("input");
  input.value =
    "值班告警处理任务。\n" +
    `告警:${a.severity} | ${a.service} | ${a.summary}\n` +
    "要求:\n" +
    "1) 使用 service_status / logs_query / metrics_query / runbook_search 排查根因(注意区分表象与根因);\n" +
    "2) 若属于可自动恢复的故障,调用对应变更工具(service_restart / scale_up / log_cleanup)完成处置;\n" +
    "3) 若属于外部依赖、业务代码或需人工处理的问题,不要执行任何变更,只给处置建议;\n" +
    "4) 最后必须调用一次 submit_diagnosis 提交结构化结论。";
  autoResize();
  input.focus();
}

// 渲染服务健康卡
function renderHealth(services) {
  const list = document.getElementById("svcList");
  list.innerHTML = "";
  for (const [name, s] of Object.entries(services)) {
    const cls = s.status === "down" ? " down" : s.status !== "running" ? " warn" : "";
    const row = el("div", "svc-row" + cls);
    row.appendChild(el("span", "sdot"));
    row.appendChild(el("span", "nm", name));
    const err = Number(s.error_rate || 0);
    const m = el("span", "m");
    if (err >= 0.05) {
      const b = el("b", null, `${(err * 100).toFixed(0)}%`);
      m.append(`${s.replicas} 副本 · err `, b);
    } else {
      m.textContent = `${s.replicas} 副本 · err ${(err * 100).toFixed(1)}%`;
    }
    row.appendChild(m);
    list.appendChild(row);
  }
}

// 渲染报告列表(新报告出现时轻提示)
function renderReports(reports) {
  const sig = reports.join(",");
  const fresh = oc.lastReports !== "" && sig !== oc.lastReports;
  oc.lastReports = sig;
  const list = document.getElementById("reportList");
  if (!reports.length) {
    list.innerHTML = "";
    list.appendChild(el("div", "sysline", "尚无报告 · 完成一次诊断后生成"));
    return;
  }
  list.innerHTML = "";
  for (const name of reports.slice().reverse()) {
    const item = el("button", "report-item");
    item.innerHTML =
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
      '<path d="M14 2v6h6M9 15l2 2 4-4"/></svg>';
    item.appendChild(el("span", null, name));
    item.addEventListener("click", () => openReport(name));
    list.appendChild(item);
  }
  if (fresh) toast("处置报告已生成", "info");
}

// 打开报告浮层
async function openReport(name) {
  try {
    const r = await fetch("/api/report/" + encodeURIComponent(name));
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "not found");
    document.getElementById("reportMeta").textContent = name;
    const body = document.getElementById("reportBody");
    body.innerHTML = mdRender(data.content || "");
    document.getElementById("reportOverlay").classList.remove("hidden");
  } catch (e) {
    toast("报告读取失败: " + (e.message || e));
  }
}

// 构建演练下拉菜单(剧本清单来自 /api/state 缓存)
function buildDrillMenu(menu) {
  menu.innerHTML = "";
  const label = el("div", "sec-label", "选择故障剧本注入");
  label.style.padding = "6px 10px 4px";
  menu.appendChild(label);
  for (const s of oc.scenarios) {
    const item = el("button", "drill-item");
    item.appendChild(el("span", "sev " + s.severity, s.severity));
    item.appendChild(el("span", null, s.summary));
    item.appendChild(el("span", "rid", s.id + (s.recoverable ? "" : " · 诊断类")));
    item.addEventListener("click", async () => {
      menu.classList.add("hidden");
      await doInject(s.id);
    });
    menu.appendChild(item);
  }
}

// 注入剧本
async function doInject(faultId) {
  try {
    const r = await fetch("/api/inject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fault_id: faultId }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "inject failed");
    toast(`已注入:${data.service} · ${data.severity}(alert ${data.alert_id})`, "info");
    await pollOncall();
  } catch (e) {
    toast("注入失败: " + (e.message || e));
  }
}

// 轮询 /api/state 更新面板
async function pollOncall() {
  try {
    const r = await fetch("/api/state");
    const data = await r.json();
    const wasAvailable = oc.available;
    if (!data.available) {
      oc.available = false;
      if (wasAvailable) setOncallVisible(false);
      return;
    }
    oc.available = true;
    oc.topology = data.topology || {};
    oc.externals = data.externals || [];
    oc.services = data.services || {};
    oc.scenarios = data.scenarios || [];
    oc.menuBuilt = false; // 场景清单可能更新
    setOncallVisible(true);
    renderAlerts(data.alerts || []);
    renderHealth(data.services || {});
    renderReports(data.reports || []);
  } catch { /* 桥接不可达时静默,下轮重试 */ }
}

// 演练按钮与全局事件绑定(幂等)
(function bindOncallControls() {
  const btnDrill = document.getElementById("btnDrill");
  const menu = document.getElementById("drillMenu");
  btnDrill.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!oc.menuBuilt) {
      buildDrillMenu(menu);
      oc.menuBuilt = true;
    }
    const rect = btnDrill.getBoundingClientRect();
    menu.style.left = Math.max(8, rect.left) + "px";
    menu.style.top = rect.bottom + 6 + "px";
    menu.classList.toggle("hidden");
  });
  document.getElementById("btnDrillReset").addEventListener("click", async () => {
    await fetch("/api/reset", { method: "POST" });
    toast("模拟环境已重置到基线", "info");
    await pollOncall();
  });
  document.addEventListener("click", (ev) => {
    if (!menu.classList.contains("hidden") &&
        !menu.contains(ev.target) && ev.target !== btnDrill) {
      menu.classList.add("hidden");
    }
  });
  document.getElementById("reportBack").addEventListener("click", () => {
    document.getElementById("reportOverlay").classList.add("hidden");
  });
})();

// 启动轮询(3s)
setInterval(pollOncall, 3000);
pollOncall();
