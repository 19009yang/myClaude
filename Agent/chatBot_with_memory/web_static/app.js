"use strict";

const $ = (selector) => document.querySelector(selector);
const app = $(".app");
const state = {bootstrap: null, sessions: [], id: null, messages: [], jobs: [], diagnostics: {}, run: null,
  source: null, selectedMemory: null, edit: null, refreshVersion: 0, memoryVersion: 0, warnings: [], lastReply: null};
const paths = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  blocks: '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><path d="M17.5 14v7M14 17.5h7"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3Z"/>',
  history: '<path d="M3 10a9 9 0 1 1 1 7M3 4v6h6M12 7v5l3 2"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  settings: '<path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 3 2 3-1 1-3 3-1 2-3-2-2 1-3-2-3-3 1-3-2Z"/><circle cx="12" cy="11" r="3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M5.5 8h1M5.5 11h1"/>',
  brain: '<path d="M12 5c-3-4-7-1-6 2-4 0-5 6-2 8-1 4 4 7 8 4m0-14c3-4 7-1 6 2 4 0 5 6 2 8 1 4-4 7-8 4Zm0 0v14M6 7l2 2m10-2-2 2M4 15l4-1m12 1-4-1"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  code: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3l-4 18"/>',
  list: '<path d="M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1"/>',
  spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  arrow: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>'
};
function icon(name) { return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.spark}</svg>`; }
document.querySelectorAll("[data-icon]").forEach(el => { el.innerHTML = icon(el.dataset.icon); });
function escapeHTML(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c])); }
function storage(key, value) { try { if (value === undefined) return localStorage.getItem("myclaude." + key); localStorage.setItem("myclaude." + key, value); } catch {} }
function inline(text) {
  const re = /`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*/g;
  let result = "", last = 0;
  for (const match of text.matchAll(re)) {
    result += escapeHTML(text.slice(last, match.index));
    if (match[1] !== undefined) result += `<code>${escapeHTML(match[1])}</code>`;
    else if (match[2] !== undefined) result += `<a href="${escapeHTML(match[3])}" target="_blank" rel="noopener noreferrer">${escapeHTML(match[2])}</a>`;
    else if (match[4] !== undefined) result += `<strong>${escapeHTML(match[4])}</strong>`;
    else result += `<em>${escapeHTML(match[5])}</em>`;
    last = match.index + match[0].length;
  }
  return result + escapeHTML(text.slice(last));
}
function markdown(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  let html = "", paragraph = [], list = "";
  const flush = () => { if (paragraph.length) { html += `<p>${paragraph.map(inline).join("<br>")}</p>`; paragraph = []; } if (list) { html += `</${list}>`; list = ""; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flush(); const language = line.slice(3).trim(); const code = [];
      while (++i < lines.length && !/^```/.test(lines[i])) code.push(lines[i]);
      html += `<div class="code-block"><div class="code-header"><span>${escapeHTML(language || "code")}</span><button class="copy-code" type="button">复制代码</button></div><pre><code>${escapeHTML(code.join("\n"))}</code></pre></div>`;
    } else if (/^#{1,3}\s/.test(line)) {
      flush(); const level = line.match(/^#+/)[0].length; html += `<h${level}>${inline(line.replace(/^#+\s/, ""))}</h${level}>`;
    } else if (/^\s*([-*+] |\d+\. )/.test(line)) {
      if (paragraph.length) flush();
      const type = /^\s*\d/.test(line) ? "ol" : "ul";
      if (list !== type) { if (list) html += `</${list}>`; html += `<${type}>`; list = type; }
      html += `<li>${inline(line.replace(/^\s*(?:[-*+] |\d+\. )/, ""))}</li>`;
    } else if (/^>\s?/.test(line)) { flush(); html += `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`; }
    else if (!line.trim()) flush();
    else { if (list) flush(); paragraph.push(line); }
  }
  flush(); return html;
}
let toastTimer;
function toast(message) { clearTimeout(toastTimer); $("#toast").textContent = message; $("#toast").hidden = false; toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 6500); }
async function api(path, data) {
  const options = data === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-MyClaude-Token": state.bootstrap.csrf_token}, body: JSON.stringify(data)};
  const response = await fetch(path, options);
  let value; try { value = await response.json(); } catch { throw new Error("服务返回无效响应，请检查服务是否仍在运行"); }
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}
function bindAsync(selector, event, fn) { $(selector).addEventListener(event, e => { Promise.resolve(fn(e)).catch(error => toast(error.message)); }); }
function dateLabel(value) { const day = new Date(value); return Number.isNaN(day.getTime()) ? "历史会话" : day.toLocaleDateString("zh-CN", {month: "short", day: "numeric"}); }
async function refreshSessions() {
  state.sessions = await api("/api/sessions"); renderSessions();
}
function renderSessions() {
  const query = $("#session-search").value.toLowerCase();
  const sessions = state.sessions.filter(s => s.title.toLowerCase().includes(query));
  let group = "";
  $("#session-list").innerHTML = sessions.map(s => {
    const label = dateLabel(s.updated_at); const heading = label !== group ? `<div class="session-group">${label}</div>` : ""; group = label;
    return `${heading}<button class="session-row ${s.session_id === state.id ? "selected" : ""}" data-session="${escapeHTML(s.session_id)}" title="${escapeHTML(s.title)}"><span>${escapeHTML(s.title)}</span>${s.running ? "<i></i>" : ""}</button>`;
  }).join("") || '<div class="session-group">没有匹配的会话</div>';
  const selected = state.sessions.find(s => s.session_id === state.id);
  $("#session-title").textContent = selected?.title || "新会话";
}
async function newSession() {
  const session = await api("/api/sessions", {}); await refreshSessions(); await selectSession(session.session_id); $("#prompt").focus();
}
async function selectSession(id) {
  state.source?.close(); state.source = null; state.id = id; state.run = null; state.warnings = []; state.lastReply = null;
  $("#prompt").value = ""; $("#prompt").style.height = ""; state.messages = []; renderMessages();
  storage("session", id); renderSessions();
  const session = await refreshSession(id);
  if (session && state.id === id && session.run_id) connectRun(session.run_id, id);
  if (window.innerWidth < 701) app.classList.add("no-sidebar");
}
async function refreshSession(id = state.id) {
  if (id !== state.id) return null;
  const version = ++state.refreshVersion;
  const session = await api("/api/session?session_id=" + encodeURIComponent(id));
  if (id !== state.id || version !== state.refreshVersion) return null;
  state.messages = session.messages; state.jobs = session.jobs; state.diagnostics = session.diagnostics; state.run = session.run_id;
  renderMessages(); renderContext(); renderJobs(); renderControls(); await refreshMemories(); return session;
}
function renderMessages() {
  const conversation = $("#conversation"); const nearBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 160;
  $("#welcome").hidden = state.messages.length > 0;
  const results = new Map(state.messages.filter(r => r.payload.role === "tool").map(r => [r.payload.tool_call_id, r.payload.content]));
  let html = state.messages.map(row => {
    const m = row.payload;
    if (m.role === "tool" || m.role === "system") return "";
    if (m.role === "user") return `<article class="message user"><div class="message-content">${escapeHTML(m.content)}</div></article>`;
    const reasoning = m.reasoning_content ? `<details class="reasoning-card"><summary>模型返回的思考内容</summary><pre>${escapeHTML(m.reasoning_content)}</pre></details>` : "";
    const tools = (m.tool_calls || []).map(call => {
      const fn = call.function || {}; const output = results.get(call.id);
      let args = fn.arguments; try { args = JSON.stringify(typeof args === "string" ? JSON.parse(args) : args, null, 2); } catch {}
      return `<details class="tool-card"><summary><span class="icon">${icon("code")}</span><code>${escapeHTML(fn.name)}</code><span class="tool-status">${output !== undefined ? "已返回" : (state.run ? "等待结果" : "本轮未完成")}</span></summary><div class="tool-section-label">输入参数</div><pre>${escapeHTML(args)}</pre><div class="tool-section-label">返回结果</div><pre>${escapeHTML(output ?? "尚无执行结果")}</pre></details>`;
    }).join("");
    return `<article class="message assistant"><div class="assistant-label"><span class="mark">✳</span>myClaude</div>${reasoning}${tools}<div class="message-content markdown">${markdown(m.content)}</div>${m.content ? `<div class="message-actions"><button data-copy-message="${escapeHTML(row.message_id)}">复制回复</button></div>` : ""}</article>`;
  }).join("");
  html += state.warnings.map(w => `<div class="inline-warning">${escapeHTML(w)}</div>`).join("");
  $("#messages").innerHTML = html;
  if (nearBottom) conversation.scrollTop = conversation.scrollHeight;
}
function renderControls() {
  const ready = state.bootstrap?.model.ready;
  $("#setup-banner").hidden = !!ready;
  $("#send").hidden = !!state.run; $("#stop").hidden = !state.run;
  $("#send").disabled = !state.id || !$("#prompt").value.trim() || !ready;
  $("#run-status").hidden = !state.run;
  $("#model-name").textContent = state.bootstrap?.demo ? "离线演示" : (state.bootstrap?.model.model || "配置模型");
  $("#settings-dot").hidden = !!ready;
}
function connectRun(runId, sessionId) {
  state.source?.close(); state.run = runId; renderControls();
  const source = new EventSource("/api/events?run_id=" + encodeURIComponent(runId)); state.source = source;
  let refreshTimer;
  const scheduleRefresh = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refreshSession(sessionId).catch(error => toast(error.message)), 50); };
  for (const event of ["message", "context", "tool_start", "tool_end", "status", "warning", "error", "done"]) {
    source.addEventListener(event, async e => {
      if (state.id !== sessionId || state.source !== source) return;
      const data = JSON.parse(e.data);
      if (event === "status") $("#run-status-text").textContent = data.text;
      if (event === "tool_start") $("#run-status-text").textContent = "正在执行 " + data.name;
      if (event === "message") { if (data.role === "assistant" && !data.tool_calls) state.lastReply = data; scheduleRefresh(); }
      if (event === "context") { state.diagnostics = data; renderContext(); }
      if (event === "tool_end") scheduleRefresh();
      if (event === "warning" || event === "error") { state.warnings.push(data.message); renderMessages(); }
      if (event === "done") {
        clearTimeout(refreshTimer); source.close(); state.source = null; state.run = null;
        try {
          await refreshSession(sessionId); await refreshSessions();
          // A generated reply can survive a storage error. Keep it visible even when absent from SQLite.
          if (state.id === sessionId && state.lastReply && !state.messages.some(m => m.payload.role === "assistant" && m.payload.content === state.lastReply.content)) {
            state.messages.push({message_id: "unsaved", payload: state.lastReply}); renderMessages();
          }
        } catch (error) { toast(error.message); }
        renderControls();
      }
    });
  }
  source.onerror = async () => {
    if (state.source !== source || state.id !== sessionId) return;
    $("#run-status-text").textContent = "连接中断，正在重新连接…";
    try {
      const session = await api("/api/session?session_id=" + encodeURIComponent(sessionId));
      if (!session.run_id && state.source === source) { source.close(); state.source = null; state.run = null; await refreshSession(sessionId); await refreshSessions(); }
    } catch { $("#connection").textContent = "连接已断开，请重启服务后刷新"; }
  };
}
async function refreshMemories() {
  const id = state.id; if (!id) return;
  const version = ++state.memoryVersion;
  const query = $("#memory-search").value.trim();
  const detail = query ? await api(`/api/memory-search?session_id=${encodeURIComponent(id)}&q=${encodeURIComponent(query)}`) : null;
  const memories = detail ? detail.results.map(hit => hit.item) : await api(`/api/memories?session_id=${encodeURIComponent(id)}`);
  if (id !== state.id || version !== state.memoryVersion) return;
  $("#memory-count").textContent = memories.length;
  const retrievalNote = detail ? `<div class="panel-intro">${escapeHTML(detail.mode)} · 索引 ${detail.index.indexed ?? "—"}/${detail.index.eligible ?? "—"}${detail.retrieval.degraded_reasons.length ? ` · ${escapeHTML(detail.retrieval.degraded_reasons.join(", "))}` : ""}</div>` : "";
  const types = {fact: "事实", preference: "偏好", event: "事件"}, scopes = {user: "跨会话", session: "当前会话", project: "项目"};
  $("#memory-items").innerHTML = retrievalNote + (memories.map(item => `<article class="memory-card"><div class="memory-card-top"><span class="type-label">${types[item.type] || escapeHTML(item.type)}</span><span>${scopes[item.scope_kind] || ""}</span><span>${item.status === "pending" ? "· 待裁决" : ""}</span></div><p>${escapeHTML(item.content)}</p><div class="memory-card-bottom"><button data-inspect="${escapeHTML(item.memory_id)}">${item.source_message_ids.length} 条来源 ↗</button><div class="memory-actions">${item.status === "active" ? `<button data-edit-memory="${escapeHTML(item.memory_id)}">更正</button>` : ""}<button data-forget="${escapeHTML(item.memory_id)}">遗忘</button></div></div></article>`).join("") || `<div class="empty-memory"><span class="icon">${icon("brain")}</span><h3>${query ? "没有匹配的记忆" : "留一点记忆给下一次对话"}</h3><p>${query ? "试试其他表述" : "告诉我「记住：…」<br>保存后，会在这里找到它的来源。"}</p></div>`);
}
function renderJobs() {
  const unfinished = state.jobs.filter(job => job.status !== "completed");
  $("#job-count").textContent = unfinished.length;
  const status = {pending: "等待整理", processing: "正在整理", failed: "整理失败", completed: "已完成"};
  $("#jobs").innerHTML = unfinished.map(job => `<div class="job">${escapeHTML(status[job.status] || job.status)} · ${escapeHTML(job.kind)}<button data-retry="${escapeHTML(job.job_id)}" ${state.run || state.bootstrap.demo ? "disabled" : ""}>重试整理</button><br><span>已尝试 ${job.attempts} 次</span>${job.last_error ? `<div class="job-error">${escapeHTML(job.last_error)}</div>` : ""}</div>`).join("") || '<div class="panel-intro">暂无待处理任务</div>';
}
function renderContext() {
  const d = state.diagnostics;
  if (!Object.keys(d).length) { $("#context-details").innerHTML = '<div class="empty-memory"><h3>等待第一轮对话</h3><p>发送消息后查看实际召回的记忆、<br>上下文用量和裁剪情况。</p></div>'; return; }
  const percentage = Math.min(100, Math.round(100 * d.estimated_tokens / d.input_budget));
  $("#context-details").innerHTML = `<div class="context-card"><h3>上下文用量</h3><div class="context-metric">${Number(d.estimated_tokens).toLocaleString()} <small>/ ${Number(d.input_budget).toLocaleString()}</small></div><progress class="context-progress" value="${Number(d.estimated_tokens)}" max="${Number(d.input_budget)}" aria-label="上下文用量"></progress><p>${percentage}% · UTF-8 估算，非模型精确 token 数</p></div><div class="context-row"><span>检索方式</span><span>${escapeHTML(d.retrieval_mode || "keyword")}</span></div><div class="context-row"><span>工具定义 tokens</span><span>${Number(d.tools_tokens || 0)}</span></div><div class="context-row"><span>裁剪的历史轮次</span><span>${Number(d.dropped_turns || 0)}</span></div><div class="context-row"><span>截短的工具视图</span><span>${Number(d.truncated_tool_views || 0)}</span></div><h3 class="section-label">本轮召回 · ${(d.retrieved || []).length} 条记忆</h3>${(d.retrieved || []).map(hit => `<div class="memory-card"><p>${escapeHTML(hit.item.content)}</p><button data-inspect="${escapeHTML(hit.item.memory_id)}">查看来源 ↗</button></div>`).join("") || '<div class="panel-intro">本轮没有召回匹配的记忆。</div>'}<div class="context-row"><span>聚合摘要</span><span>${(d.summaries || []).length}</span></div><div class="context-row"><span>已验证经验</span><span>${(d.experiences || []).length}</span></div>`;
}
function openSettings() {
  const model = state.bootstrap.model;
  $("#provider").value = model.provider; $("#model").value = model.model;
  $("#base-url").value = model.base_url; $("#api-key").value = "";
  $("#api-key").placeholder = model.has_key ? "已配置，留空保留当前路由的密钥" : "输入 API key";
  $("#context-window").value = model.context_window || 32768; $("#max-tokens").value = model.max_tokens;
  $("#settings-error").textContent = ""; $("#settings-dialog").showModal();
}
function openEdit(mode, title, content, memoryId = null) {
  state.edit = {mode, memoryId, sessionId: state.id};
  $("#edit-title").textContent = title; $("#edit-content").value = content;
  $("#edit-content").maxLength = mode === "rename" ? 100 : 4000;
  $("#edit-note").textContent = mode === "rename" ? "只修改会话显示名称。" : "更正将创建新版本，保留旧版本和原始证据。";
  $("#edit-dialog").showModal();
}
async function inspectMemory(id) {
  const detail = await api(`/api/memory?session_id=${encodeURIComponent(state.id)}&id=${encodeURIComponent(id)}`);
  $("#detail-title").textContent = "记忆与来源";
  $("#detail-content").innerHTML = `<p>${escapeHTML(detail.item.content)}</p><pre>${escapeHTML(detail.item.memory_id)}\n版本 ${detail.item.revision} · ${escapeHTML(detail.item.status)}</pre><h3>原始来源 · ${detail.sources.length}</h3>${detail.sources.map(source => `<div class="capability-item"><small class="muted">${escapeHTML(source.message_id)} · ${dateLabel(source.created_at)}</small><p>${escapeHTML(source.payload.content)}</p></div>`).join("") || '<p class="muted">此记录没有关联的消息来源。</p>'}<h3>版本记录 · ${detail.versions.length}</h3>${detail.versions.map(item => `<div class="capability-item"><small class="muted">v${item.revision} · ${escapeHTML(item.status)}</small><p>${escapeHTML(item.content)}</p></div>`).join("")}`;
  $("#detail-dialog").showModal();
}

bindAsync("#new-chat", "click", newSession);
bindAsync("#session-list", "click", async e => { const button = e.target.closest("[data-session]"); if (button) await selectSession(button.dataset.session); });
$("#session-search").addEventListener("input", renderSessions);
$("#sidebar-toggle").addEventListener("click", () => app.classList.toggle("no-sidebar"));
function toggleMemory() { app.classList.toggle("no-memory"); $("#memory-toggle").classList.toggle("active", !app.classList.contains("no-memory")); }
$("#memory-toggle").addEventListener("click", toggleMemory); $("#memory-close").addEventListener("click", toggleMemory);
$("#theme-toggle").addEventListener("click", () => { document.body.classList.toggle("dark"); storage("theme", document.body.classList.contains("dark") ? "dark" : "light"); });
for (const selector of ["#settings-button", "#setup-model", "#model-button"]) $(selector).addEventListener("click", () => { if (state.bootstrap) openSettings(); });
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $("#" + button.dataset.close).close()));
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => { $("#prompt").value = button.dataset.prompt; $("#prompt").focus(); renderControls(); }));
$("#prompt").addEventListener("input", () => { $("#prompt").style.height = "auto"; $("#prompt").style.height = Math.min(200, $("#prompt").scrollHeight) + "px"; renderControls(); });
$("#prompt").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!$("#send").disabled && !state.run) $("#composer").requestSubmit(); } });
bindAsync("#composer", "submit", async e => {
  e.preventDefault(); if (state.run || !$("#prompt").value.trim()) return;
  const content = $("#prompt").value; const id = state.id; $("#send").disabled = true;
  try {
    const result = await api("/api/messages", {session_id: id, content});
    if (id !== state.id) return;
    $("#prompt").value = ""; $("#prompt").style.height = ""; state.warnings = []; state.lastReply = null;
    $("#run-status-text").textContent = "正在准备上下文"; connectRun(result.run_id, id); await refreshSessions();
    $("#conversation").scrollTop = $("#conversation").scrollHeight;
  } finally { renderControls(); }
});
bindAsync("#stop", "click", async () => { if (state.run) { await api("/api/stop", {run_id: state.run}); $("#run-status-text").textContent = "正在停止，等待当前模型或工具返回…"; } });
bindAsync("#settings-form", "submit", async e => {
  e.preventDefault(); const button = e.target.querySelector('[type="submit"]'); button.disabled = true;
  try { state.bootstrap.model = await api("/api/settings", Object.fromEntries(new FormData(e.target))); $("#api-key").value = ""; $("#settings-dialog").close(); renderControls(); toast("模型配置已更新，仅在本次服务运行期间生效"); }
  catch (error) { $("#settings-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("#provider").addEventListener("change", () => { $("#base-url").value = ""; $("#api-key").value = ""; $("#api-key").placeholder = "输入该提供商的 API key"; });
$("#settings-dialog").addEventListener("close", () => { $("#api-key").value = ""; });
$("#session-title").addEventListener("click", () => { if (state.id) openEdit("rename", "重命名会话", $("#session-title").textContent); });
bindAsync("#edit-form", "submit", async e => {
  e.preventDefault(); const edit = state.edit; const content = $("#edit-content").value;
  await api(edit.mode === "rename" ? "/api/rename" : "/api/memory/update", {session_id: edit.sessionId, title: content, content, memory_id: edit.memoryId});
  $("#edit-dialog").close(); await refreshSessions(); await refreshMemories(); toast(edit.mode === "rename" ? "会话名称已更新" : "记忆已更正，旧版本与原文已保留");
});
let memorySearchTimer;
$("#memory-search").addEventListener("input", () => { clearTimeout(memorySearchTimer); memorySearchTimer = setTimeout(() => refreshMemories().catch(error => toast(error.message)), 250); });
$("#tab-library").addEventListener("click", () => setTab("library")); $("#tab-context").addEventListener("click", () => setTab("context"));
function setTab(tab) { for (const name of ["library", "context"]) { $("#tab-" + name).classList.toggle("selected", tab === name); $("#tab-" + name).setAttribute("aria-selected", String(tab === name)); $("#" + (name === "library" ? "library" : "context") + "-view").hidden = tab !== name; } }
bindAsync("#memory-panel", "click", async e => {
  const inspect = e.target.closest("[data-inspect]"); if (inspect) return inspectMemory(inspect.dataset.inspect);
  const edit = e.target.closest("[data-edit-memory]");
  if (edit) { const card = edit.closest(".memory-card"); openEdit("memory", "更正这条记忆", card.querySelector("p").textContent, edit.dataset.editMemory); return; }
  const forget = e.target.closest("[data-forget]");
  if (forget) {
    if (!window.confirm("遗忘这条记忆？它将停止参与召回，原始会话和版本证据仍然保留。")) return;
    await api("/api/memory/forget", {session_id: state.id, memory_id: forget.dataset.forget}); await refreshMemories(); toast("已遗忘，原始证据保留");
  }
  const retry = e.target.closest("[data-retry]");
  if (retry && !retry.disabled) { retry.disabled = true; retry.textContent = "整理中…"; try { await api("/api/jobs/retry", {session_id: state.id, job_id: retry.dataset.retry}); await refreshSession(); } finally { retry.disabled = false; retry.textContent = "重试整理"; } }
});
bindAsync("#messages", "click", async e => {
  const code = e.target.closest(".copy-code"); const message = e.target.closest("[data-copy-message]");
  const value = code ? code.closest(".code-block").querySelector("code").textContent : message ? state.messages.find(row => row.message_id === message.dataset.copyMessage)?.payload.content : null;
  if (value !== null && value !== undefined) { await navigator.clipboard.writeText(value); toast("已复制"); }
});
$("#capabilities").addEventListener("click", () => {
  if (!state.bootstrap) return;
  $("#detail-title").textContent = "工具与技能";
  $("#detail-content").innerHTML = `<p class="muted">Agent 根据任务自动选择工具。现有安全策略继续生效；需要确认的工具操作在 Web 版中返回未执行，不等待终端输入。</p><h3>内置工具 · ${state.bootstrap.tools.length}</h3>${state.bootstrap.tools.map(tool => `<div class="capability-item"><strong>${escapeHTML(tool.name)}</strong><p>${escapeHTML(tool.description)}</p></div>`).join("")}<h3>Skills · ${state.bootstrap.skills.length}</h3>${state.bootstrap.skills.map(skill => `<div class="capability-item"><strong>${escapeHTML(skill.name)}</strong><p>${escapeHTML(skill.description)}</p></div>`).join("")}<p class="muted">LaMemory 的保存、查询、更正与遗忘工具也会绑定到当前会话。</p>`;
  $("#detail-dialog").showModal();
});
document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); newSession().catch(error => toast(error.message)); }
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.querySelector("dialog[open]")) { e.preventDefault(); app.classList.remove("no-sidebar"); $("#session-search").focus(); }
});
async function init() {
  document.body.classList.toggle("dark", storage("theme") === "dark");
  if (window.innerWidth < 1151) app.classList.add("no-memory");
  if (window.innerWidth < 701) app.classList.add("no-sidebar");
  $("#memory-toggle").classList.toggle("active", !app.classList.contains("no-memory"));
  try {
    state.bootstrap = await api("/api/bootstrap");
    $("#workspace-name").textContent = state.bootstrap.project || state.bootstrap.workspace;
    $("#tool-count").textContent = state.bootstrap.tools.length;
    $("#demo-tag").hidden = !state.bootstrap.demo;
    $("#provider").innerHTML = state.bootstrap.providers.map(provider => `<option value="${escapeHTML(provider)}">${escapeHTML({"openai-compatible":"OpenAI 兼容服务",openai:"OpenAI",anthropic:"Anthropic",deepseek:"DeepSeek",qwen:"Qwen"}[provider] || provider)}</option>`).join("");
    $("#connection").textContent = state.bootstrap.demo ? "离线演示 · 临时工作台" : "已连接 · 127.0.0.1";
    await refreshSessions();
    const previous = storage("session"); const chosen = state.sessions.find(s => s.session_id === previous) || state.sessions[0];
    if (chosen) await selectSession(chosen.session_id); else await newSession();
    renderControls();
  } catch (error) { $("#connection").textContent = "连接失败，请检查本地服务"; toast(error.message); }
}
init();
