/* kyleAgent Web Console 前端逻辑
 * 协议:与 kyle-core 的 JSON-RPC 2.0 over NDJSON 完全一致,经桥接器中继。
 * - 响应:含 "jsonrpc" 字段 → 按 id 匹配 pending
 * - 事件:{"kind":"event","event":{...}} → 按 type 路由渲染
 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  ws: null,
  connected: false,
  sessionSeq: 0,
  sessionId: null,
  pending: new Map(),   // reqId -> {resolve, reject}
  reqSeq: 0,
  runId: null,
  running: false,
  streamEl: null,       // 当前流式 assistant 文本容器
  streamRaw: "",
  toolCards: new Map(), // tool_use_id -> {el, bodyEl, startedAt}
  subagents: new Map(), // child run_id -> container el
  currentSub: null,
  pendingPerm: null,    // {toolUseId, el}
  lastRunId: null,
  everConnected: false,
  retryTimer: null,
};

/* ── 小工具 ─────────────────────────────────────── */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// 追加到会话流;真实内容(user/assistant/工具/审批)出现后移除 hero 空状态,
// 系统提示行(sysline/divider)不清场
function append(node, keepHero) {
  if (!keepHero) {
    const hero = $("#hero");
    if (hero && hero.parentNode) hero.remove();
  }
  $("#transcript").appendChild(node);
  scrollBottom(true);
}

function scrollBottom(force) {
  const sc = $("#scroller");
  const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 140;
  if (force || nearBottom) sc.scrollTop = sc.scrollHeight;
}

let toastTimer = null;
function toast(text, kind) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (kind === "info" ? " info" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3800);
}

function setConn(stateName, label) {
  $("#connDot").className = "conn " + stateName;
  $("#connText").textContent = label;
}

function setSessionUI() {
  $("#sessionId").textContent = state.sessionId || "—";
  $("#sessionState").textContent = state.running ? "running" : (state.sessionId ? "waiting_for_input" : "—");
  $("#lastRun").textContent = state.lastRunId || "—";
  $("#btnCompact").disabled = !state.sessionId || state.running;
  $("#btnClose").disabled = !state.sessionId;
}

// 极简 Markdown:标题/粗斜体/行内代码/围栏/列表/链接(顺序敏感)
function mdRender(src) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const blocks = [];
  // 先抽出围栏代码块
  let text = src.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = text.split("\n");
  const out = [];
  let list = null;
  for (const line of lines) {
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.、]\s+(.*)$/.exec(line);
    if (h) { if (list) { out.push(`</${list}>`); list = null; } out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); }
    else if (ul) { if (list !== "ul") { if (list) out.push(`</${list}>`); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(ul[1])}</li>`); }
    else if (ol) { if (list !== "ol") { if (list) out.push(`</${list}>`); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(ol[1])}</li>`); }
    else if (line.trim() === "") { if (list) { out.push(`</${list}>`); list = null; } }
    else { if (list) { out.push(`</${list}>`); list = null; } out.push(`<p>${inline(line)}</p>`); }
  }
  if (list) out.push(`</${list}>`);
  let html = out.join("\n");
  html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)]);
  return html;
}

const TOOL_ICONS = {
  bash: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 17l6-5-6-5M12 19h8"/></svg>',
  read_file: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/></svg>',
  write_file: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>',
  list_dir: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  web_search: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
  note_save: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
  spawn_agent: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.6"/><circle cx="18" cy="18" r="2.6"/><path d="M8.5 8L15.5 16"/></svg>',
  compact: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 8h16M4 16h16M9 4l-2 4M15 20l2-4"/></svg>',
};
const toolIcon = (name) => TOOL_ICONS[name] ||
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="3"/></svg>';

function previewOf(params) {
  try {
    const p = typeof params === "string" ? JSON.parse(params) : params;
    for (const k of ["command", "path", "query", "content", "prompt", "description"]) {
      if (p && typeof p[k] === "string") {
        const s = p[k].replace(/\s+/g, " ").trim();
        return `${k}=${s.length > 90 ? s.slice(0, 90) + "…" : s}`;
      }
    }
    const s = JSON.stringify(p);
    return s.length > 90 ? s.slice(0, 90) + "…" : s;
  } catch { return String(params).slice(0, 90); }
}

/* ── 流式 assistant 文本 ────────────────────────── */

function breakStream() {
  if (state.streamEl) {
    state.streamEl.innerHTML = mdRender(state.streamRaw);
    state.streamEl = null;
    state.streamRaw = "";
  }
}

function streamToken(token) {
  if (!state.streamEl) {
    const msg = el("div", "msg msg-assistant");
    msg.appendChild(el("div", "avatar", null)).append(el("span", null, "K"));
    const body = el("div", "body");
    const text = el("div", "assistant-text");
    text.appendChild(el("span", "caret"));
    body.appendChild(text);
    msg.appendChild(body);
    append(msg);
    state.streamEl = text;
    state.streamRaw = "";
  }
  state.streamRaw += token;
  const caret = state.streamEl.querySelector(".caret");
  const span = el("span");
  span.innerHTML = mdRender(state.streamRaw);
  state.streamEl.insertBefore(span, caret);
  scrollBottom();
}

/* ── 工具卡片 ───────────────────────────────────── */

function toolCard(toolUseId, name, params, container) {
  const card = el("div", "tool running");
  if (container) card.classList.add("nested");
  const head = el("div", "tool-head");
  head.appendChild(el("span", "ticon")).innerHTML = toolIcon(name);
  head.appendChild(el("span", "tool-name", name));
  head.appendChild(el("span", "tool-params", previewOf(params)));
  const st = el("span", "tool-state");
  st.appendChild(el("span", "badge run", "running"));
  head.appendChild(st);
  head.appendChild(el("span", "chev", "▸"));
  const body = el("div", "tool-body");
  body.appendChild(el("div", "sec", "params"));
  body.appendChild(el("div", null, typeof params === "string" ? params : JSON.stringify(params, null, 2)));
  card.append(head, body);
  head.addEventListener("click", () => card.classList.toggle("open"));
  (container || $("#transcript")).appendChild(card);
  state.toolCards.set(toolUseId, { el: card, body, st, t0: performance.now() });
  scrollBottom();
  return card;
}

function toolFinish(toolUseId, payload, isError) {
  const c = state.toolCards.get(toolUseId);
  if (!c) return;
  c.el.classList.remove("running");
  c.el.classList.add(isError ? "err" : "ok");
  const badge = c.st.querySelector(".badge");
  badge.className = "badge " + (isError ? "err" : "ok");
  badge.textContent = isError ? "failed" : "done";
  const ms = payload && payload.elapsed_ms !== undefined ? payload.elapsed_ms : Math.round(performance.now() - c.t0);
  c.st.appendChild(el("span", "ms", `${ms}ms`));
  if (payload && payload.output !== undefined) {
    c.body.appendChild(el("div", "sec", isError ? "error" : "output"));
    c.body.appendChild(el("div", null, String(payload.output)));
  }
}

/* ── 权限审批卡 ─────────────────────────────────── */

function permCard(e) {
  breakStream();
  const card = el("div", "perm");
  const title = el("div", "perm-title");
  title.append(el("span", null, "🔑 工具执行等待审批"));
  title.appendChild(el("span", "tool-name", e.tool_name || ""));
  card.appendChild(title);
  card.appendChild(el("div", "perm-detail", e.param_preview || previewOf(e.params)));
  const actions = el("div", "perm-actions");
  const choices = [
    ["y", "allow_once", "允许一次"], ["a", "always_allow", "始终允许"],
    ["n", "deny_once", "拒绝"], ["d", "always_deny", "始终拒绝"],
  ];
  for (const [key, decision, label] of choices) {
    const b = el("button", "perm-btn");
    b.appendChild(el("span", "keycap", key));
    b.appendChild(el("span", null, label));
    b.addEventListener("click", () => respondPermission(e.tool_use_id, decision));
    actions.appendChild(b);
  }
  card.appendChild(actions);
  append(card);
  state.pendingPerm = { toolUseId: e.tool_use_id, el: card };
}

async function respondPermission(toolUseId, decision) {
  const p = state.pendingPerm;
  if (!p || p.toolUseId !== toolUseId) return;
  try {
    await send("permission.respond", { tool_use_id: toolUseId, decision });
    const allow = decision.startsWith("allow");
    p.el.classList.add(allow ? "done-allow" : "done-deny");
    const v = el("div", "perm-verdict " + (allow ? "allow" : "deny"),
      allow ? `✓ 已放行(${decision})` : `✕ 已拒绝(${decision})`);
    p.el.appendChild(v);
    p.el.querySelectorAll(".perm-btn").forEach((b) => (b.disabled = true));
  } catch (err) {
    toast(`审批发送失败: ${err.message || err}`);
  }
  state.pendingPerm = null;
}

/* ── 子代理容器 ─────────────────────────────────── */

function subagentStart(e) {
  breakStream();
  const box = el("div", "subagent");
  const head = el("div", "subagent-head");
  head.appendChild(el("span", "ticon")).innerHTML = toolIcon("spawn_agent");
  head.appendChild(el("span", null, `subagent · ${e.description || ""}`));
  head.appendChild(el("span", "badge run", e.run_id));
  box.appendChild(head);
  append(box);
  state.subagents.set(e.run_id, box);
  state.currentSub = e.run_id;
}

function subagentEnd(e) {
  const box = state.subagents.get(e.run_id);
  if (box) {
    const badge = box.querySelector(".badge");
    if (badge) {
      const ok = e.status === "success";
      badge.className = "badge " + (ok ? "ok" : "err");
      badge.textContent = `${e.status} · ${e.run_id}`;
    }
  }
  if (state.currentSub === e.run_id) state.currentSub = null;
}

// 事件归属容器:子代理 run → 其容器;否则主会话流
function eventContainer(e) {
  if (e.run_id && state.subagents.has(e.run_id) && e.run_id !== state.runId) {
    return state.subagents.get(e.run_id);
  }
  return null;
}

/* ── 事件路由 ───────────────────────────────────── */

function handleEvent(e) {
  const t = e.type || "";
  const inSub = eventContainer(e) !== null;

  switch (t) {
    case "run.started": {
      state.runId = e.run_id;
      state.running = true;
      state.lastRunId = e.run_id;
      setConn("busy", "running");
      setSessionUI();
      append(el("div", "divider", `run ${e.run_id}`), true);
      break;
    }
    case "run.finished": {
      state.running = false;
      state.runId = null;
      setConn("ready", "ready");
      setSessionUI();
      if (e.status !== "success") toast(`run 结束:${e.status}(${e.reason || "?"})`);
      break;
    }
    case "llm.token":
      streamToken(e.token || "");
      return; // 不 break 流
    case "llm.usage": {
      const pct = Math.min(100, Math.round((e.context_pct || 0) * 100));
      const meter = $("#meter"), fill = $("#meterFill");
      meter.classList.remove("hidden");
      fill.style.width = pct + "%";
      fill.className = "meter-fill" + (pct >= 85 ? " err" : pct >= 60 ? " warn" : "");
      $("#meterLabel").textContent = `ctx ${pct}%`;
      const hit = e.cache_hit_rate;
      if (hit !== undefined && hit !== null) {
        const chip = $("#hitChip");
        chip.classList.remove("hidden");
        chip.textContent = `cache ${(hit * 100).toFixed(0)}%`;
        chip.className = "chip" + (hit >= 0.6 ? " ok" : "");
      }
      break;
    }
    case "tool.call_started": {
      const card = toolCard(e.tool_use_id, e.tool_name, e.params, eventContainer(e));
      if (inSub) { /* nested 已加样式 */ }
      break;
    }
    case "tool.call_finished":
      toolFinish(e.tool_use_id, e, false);
      break;
    case "tool.call_failed":
      toolFinish(e.tool_use_id, e, true);
      break;
    case "permission.requested":
      permCard(e);
      break;
    case "permission.granted":
    case "permission.denied":
      if (state.pendingPerm && state.pendingPerm.toolUseId === e.tool_use_id) {
        // 超时等 daemon 侧自动决策:卡片落定
        const p = state.pendingPerm;
        const allow = t === "permission.granted";
        p.el.classList.add(allow ? "done-allow" : "done-deny");
        p.el.appendChild(el("div", "perm-verdict " + (allow ? "allow" : "deny"),
          allow ? "✓ 已放行(自动)" : "✕ 已拒绝(超时/断连)"));
        p.el.querySelectorAll(".perm-btn").forEach((b) => (b.disabled = true));
        state.pendingPerm = null;
      }
      break;
    case "subagent.started":
      subagentStart(e);
      break;
    case "subagent.finished":
      subagentEnd(e);
      break;
    case "context.compacted": {
      breakStream();
      const line = el("div", "sysline ok",
        `✦ 上下文已压缩:${e.original_tokens} → ${e.summary_tokens} tokens`);
      append(line);
      break;
    }
    case "skill.invoked":
      append(el("div", "sysline", `⚡ skill ${e.skill_name}(${e.arguments || ""})`), true);
      break;
    case "session.waiting_for_input":
      state.running = false;
      setConn("ready", "ready");
      setSessionUI();
      break;
    case "session.resumed":
      // onOpen 已渲染恢复提示,此处静默避免重复
      break;
    case "session.closed":
      state.sessionId = null;
      setSessionUI();
      break;
    case "step.started":
      if (!inSub) { /* 步进不单独渲染,减少噪音 */ }
      break;
    default:
      break;
  }
  // 任何非 token 事件都会终止当前流式块
  if (t !== "llm.token") breakStream();
  scrollBottom();
}

/* ── RPC 客户端 ─────────────────────────────────── */

function send(method, params) {
  return new Promise((resolve, reject) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      reject(new Error("not connected"));
      return;
    }
    const id = `w${++state.reqSeq}`;
    state.pending.set(id, { resolve, reject });
    state.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} }));
    setTimeout(() => {
      if (state.pending.has(id)) {
        state.pending.delete(id);
        reject(new Error(`${method}: timeout`));
      }
    }, 300000);
  });
}

function dispatch(msg) {
  if (msg.jsonrpc) {
    const p = state.pending.get(msg.id);
    if (!p) return;
    state.pending.delete(msg.id);
    if (msg.error) {
      const e = new Error(`[${msg.error.code}] ${msg.error.message}`);
      e.code = msg.error.code;
      p.reject(e);
    } else p.resolve(msg.result || {});
  } else if (msg.kind === "event") {
    handleEvent(msg.event || {});
  }
}

/* ── 连接生命周期 ───────────────────────────────── */

async function onOpen() {
  state.connected = true;
  setConn("connecting", "subscribing");
  try {
    const sub = await send("event.subscribe", {
      topics: ["*"], scope: "global",
    });
    if (sub.replayed_count > 0) {
      append(el("div", "sysline", `↺ 回放了 ${sub.replayed_count} 条历史事件`), true);
    }
    // 首次进入且无会话:resume 最近活跃会话(空 transcript 时回放其最近 run);
    // 否则新建
    if (!state.sessionId) {
      try {
        const r = await send("session.resume", {});
        state.sessionId = r.session_id;
        state.lastRunId = r.last_run_id || null;
        append(el("div", "sysline ok", `↩ 恢复会话 ${r.session_id}`), true);
        if (state.everConnected === false && r.last_run_id) {
          // 通过再次订阅携带 replay_from_run?桥接层允许重复订阅:
          // 直接发一次带 replay 的订阅把最近 run 的历史补齐
          await send("event.subscribe", {
            topics: ["*"], scope: "global", replay_from_run: r.last_run_id,
          });
        }
      } catch {
        const c = await send("session.create", { mode: "chat" });
        state.sessionId = c.session_id;
        append(el("div", "sysline ok", `✦ 新会话 ${c.session_id}`), true);
      }
    } else {
      append(el("div", "sysline ok", "✓ 已重新连接,会话保持"), true);
    }
    state.everConnected = true;
    setConn(state.running ? "busy" : "ready", state.running ? "running" : "ready");
    setSessionUI();
    $("#btnSend").disabled = false;
    $("#input").focus();
  } catch (err) {
    setConn("down", "error");
    toast(`初始化失败: ${err.message || err}`);
  }
}

function onClose(ev) {
  state.connected = false;
  state.ws = null;
  for (const { reject } of state.pending.values()) reject(new Error("connection lost"));
  state.pending.clear();
  setConn("down", "reconnecting");
  $("#btnSend").disabled = true;
  $("#banner").textContent = "与 daemon 的连接已断开,正在重连…(确认 kyle-core 在运行)";
  $("#banner").classList.remove("hidden");
  clearTimeout(state.retryTimer);
  state.retryTimer = setTimeout(connect, 1600);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = onOpen;
  ws.onclose = onClose;
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    try { dispatch(JSON.parse(ev.data)); } catch { /* 忽略非 JSON 行 */ }
  };
}

/* ── 发送与控件 ─────────────────────────────────── */

async function sendMessage() {
  const input = $("#input");
  const content = input.value.trim();
  if (!content) return;
  if (state.running) { toast("上一条消息还在执行中(session busy)", "info"); return; }
  input.value = "";
  autoResize();
  // 用户气泡
  const msg = el("div", "msg msg-user");
  msg.appendChild(el("div", "bubble", content));
  append(msg);
  // 不 await:daemon 的 send_message 要等整个 run 结束才返回,
  // 过程已由事件实时渲染;这里只关心发送层错误
  send("session.send_message", { session_id: state.sessionId, content })
    .catch((err) => {
      toast(err.message || String(err));
      if (err.code === -32010) { state.sessionId = null; setSessionUI(); }
    });
}

function autoResize() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

/* ── 键盘 ───────────────────────────────────────── */

document.addEventListener("keydown", (ev) => {
  // 审批快捷键
  if (state.pendingPerm && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    const map = { y: "allow_once", a: "always_allow", n: "deny_once", d: "always_deny" };
    const d = map[ev.key.toLowerCase()];
    if (d) {
      ev.preventDefault();
      respondPermission(state.pendingPerm.toolUseId, d);
      return;
    }
  }
});

/* ── 控件绑定 ───────────────────────────────────── */

$("#btnSend").addEventListener("click", sendMessage);
$("#input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendMessage(); }
});
$("#input").addEventListener("input", autoResize);
$("#btnNew").addEventListener("click", async () => {
  try {
    const c = await send("session.create", { mode: "chat" });
    state.sessionId = c.session_id;
    state.lastRunId = null;
    $("#transcript").innerHTML = "";
    // 还原 hero 空状态
    location.reload();
  } catch (err) { toast(err.message || String(err)); }
});
$("#btnCompact").addEventListener("click", async () => {
  try {
    const r = await send("session.compact", { session_id: state.sessionId });
    toast(`压缩完成,节省 ${r.saved_tokens} tokens`, "info");
  } catch (err) { toast(err.message || String(err)); }
});
$("#btnClose").addEventListener("click", async () => {
  try { await send("session.close", { session_id: state.sessionId }); }
  catch (err) { toast(err.message || String(err)); }
});
document.querySelectorAll(".chip-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $("#input").value = b.dataset.prompt;
    autoResize();
    $("#input").focus();
  })
);

/* ── 启动 ───────────────────────────────────────── */
$("#coreAddr").textContent = `${location.hostname === "" ? "127.0.0.1" : location.hostname}:7437`;
connect();
