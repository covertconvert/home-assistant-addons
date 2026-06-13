// Claude Code Messages — frontend
// Talks to FastAPI backend mounted at same origin under HA ingress.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  sessionId: null,
  sessions: [],
  projects: [],
  collapsedProjects: new Set(JSON.parse(localStorage.getItem('collapsedProjects') || '[]')),
  attachments: [], // [{path, name, dataUrl}]
  es: null,
  generating: false,
  turnStartedAt: 0,
  turnTokens: 0,
  typingTimer: null,
  turnActivity: null, // {verb, target} — what Claude is currently doing
  searchQuery: '',
  searchResults: null, // null = inactive; array = active (possibly empty)
};

const els = {
  thread: $('#thread'),
  input: $('#input'),
  send: $('#send'),
  stop: $('#stop'),
  fileInput: $('#file-input'),
  attachments: $('#attachments'),
  title: $('#session-title'),
  drawer: $('#drawer'),
  drawerToggle: $('#drawer-toggle'),
  drawerClose: $('#drawer-close'),
  drawerBody: $('#drawer-body'),
  drawerSearch: $('#drawer-search'),
  drawerSearchClear: $('#drawer-search-clear'),
  newSessionMore: $('#new-session-more'),
  newSession: $('#new-session'),
  newSessionDrawer: $('#new-session-drawer'),
  composerPlus: $('#composer-plus'),
  composerPanel: $('#composer-panel'),
  cpPhotos: $('#cp-photos'),
  cpPlan: $('#cp-plan'),
  cpModel: $('#cp-model'),
  cpModelLabel: $('#cp-model .cp-label'),
  actionMenu: $('#action-menu'),
};

function persistCollapsedProjects() {
  localStorage.setItem('collapsedProjects', JSON.stringify([...state.collapsedProjects]));
}

// --- Focus heartbeat -----------------------------------------------------
// Server-side push fires on generation_ended; this heartbeat tells the
// server "the user is still looking at this session, don't push." Without it
// every reply would buzz the phone even when the user is reading on-screen.
// Stops the moment the tab is hidden (iOS will suspend us anyway) so this
// never runs in the background.

const FOCUS_HEARTBEAT_MS = 20000;
let focusHeartbeatTimer = null;

async function postFocus(focused) {
  if (!state.sessionId) return;
  try {
    await api(`api/sessions/${state.sessionId}/focus`, {
      method: 'POST',
      body: JSON.stringify({ focused }),
    });
  } catch (_) { /* silent — best effort */ }
}

function startFocusHeartbeat() {
  if (focusHeartbeatTimer) return;
  postFocus(true);
  focusHeartbeatTimer = setInterval(() => postFocus(true), FOCUS_HEARTBEAT_MS);
}

function stopFocusHeartbeat({ flush = true } = {}) {
  if (focusHeartbeatTimer) {
    clearInterval(focusHeartbeatTimer);
    focusHeartbeatTimer = null;
  }
  if (flush) postFocus(false);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopFocusHeartbeat();
  else startFocusHeartbeat();
});
// iOS bfcache / app-suspend path that doesn't always fire visibilitychange.
window.addEventListener('pagehide', () => stopFocusHeartbeat());

// --- Theme ---------------------------------------------------------------
// Set as early as possible so the page never flashes the wrong colours.
function applyTheme(theme) {
  const t = ['dark', 'light', 'system'].includes(theme) ? theme : 'dark';
  document.documentElement.dataset.theme = t;
  localStorage.setItem('theme', t);
}
applyTheme(localStorage.getItem('theme') || 'dark');

// --- API helpers ---------------------------------------------------------

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try {
      const j = await r.json();
      if (j.detail) detail = j.detail;
    } catch {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

async function loadAll() {
  const [sessions, projects] = await Promise.all([
    api('api/sessions'),
    api('api/projects'),
  ]);
  state.sessions = sessions;
  state.projects = projects;
  renderDrawer();
}

async function createSession(projectId = null) {
  const sess = await api('api/sessions', {
    method: 'POST',
    body: JSON.stringify({ project_id: projectId }),
  });
  state.sessions.unshift({
    id: sess.id,
    title: sess.title || 'New conversation',
    project_id: sess.project_id ?? null,
    last_activity: Date.now() / 1000,
  });
  selectSession(sess.id);
}

async function deleteSession(id) {
  await api(`api/sessions/${id}`, { method: 'DELETE' });
  state.sessions = state.sessions.filter(s => s.id !== id);
  if (state.sessionId === id) {
    state.sessionId = null;
    els.thread.innerHTML = '';
    els.title.textContent = 'New conversation';
    if (state.es) { state.es.close(); state.es = null; }
    setGenerating(false);
  }
  renderDrawer();
}

async function renameSession(id, title) {
  const updated = await api(`api/sessions/${id}`, {
    method: 'PATCH', body: JSON.stringify({ title }),
  });
  const s = state.sessions.find(x => x.id === id);
  if (s) s.title = updated.title;
  if (state.sessionId === id) els.title.textContent = updated.title;
  renderDrawer();
}

async function moveSession(id, projectId) {
  const updated = await api(`api/sessions/${id}`, {
    method: 'PATCH', body: JSON.stringify({ project_id: projectId }),
  });
  const s = state.sessions.find(x => x.id === id);
  if (s) s.project_id = updated.project_id ?? null;
  renderDrawer();
}

async function createProject() {
  const name = prompt('Project name?');
  if (!name) return;
  const p = await api('api/projects', { method: 'POST', body: JSON.stringify({ name }) });
  state.projects.push(p);
  renderDrawer();
}

async function renameProject(id, name) {
  const updated = await api(`api/projects/${id}`, {
    method: 'PATCH', body: JSON.stringify({ name }),
  });
  const p = state.projects.find(x => x.id === id);
  if (p) p.name = updated.name;
  renderDrawer();
}

async function deleteProject(id) {
  await api(`api/projects/${id}`, { method: 'DELETE' });
  state.projects = state.projects.filter(p => p.id !== id);
  for (const s of state.sessions) if (s.project_id === id) s.project_id = null;
  renderDrawer();
}

async function sendMessage(text) {
  if (!state.sessionId) await createSession();
  const body = { text, attachments: state.attachments.map(a => a.path) };
  await api(`api/sessions/${state.sessionId}/message`, {
    method: 'POST', body: JSON.stringify(body),
  });
}

async function respondPermission(request_id, decision) {
  if (!state.sessionId || !request_id) return;
  removePermissionCard(request_id);
  try {
    await api(`api/sessions/${state.sessionId}/permission`, {
      method: 'POST', body: JSON.stringify({ decision, request_id }),
    });
  } catch (err) {
    appendSystem(`Permission send failed: ${err.message}`);
  }
}

async function interrupt() {
  if (!state.sessionId) return;
  await api(`api/sessions/${state.sessionId}/interrupt`, { method: 'POST' });
}

async function uploadFile(file) {
  const form = new FormData();
  form.append('file', file);
  const r = await fetch(`api/sessions/${state.sessionId}/upload`, { method: 'POST', body: form });
  if (!r.ok) throw new Error('upload failed');
  return r.json();
}

// --- SSE -----------------------------------------------------------------

function selectSession(id) {
  state.sessionId = id;
  els.thread.innerHTML = '';
  setGenerating(false);
  const sess = state.sessions.find(s => s.id === id);
  els.title.textContent = sess?.title || 'Conversation';
  renderModeToggle(sess?.permission_mode || 'default');
  renderModelTile();
  closeDrawer();
  if (state.es) state.es.close();
  state.es = new EventSource(`api/sessions/${id}/stream`);
  state.es.addEventListener('claude', (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err, e.data); }
  });
  state.es.addEventListener('ping', () => { /* keep-alive */ });
  state.es.onerror = () => { /* browser auto-reconnects */ };
  renderDrawer();
  // Mark the newly-selected session focused immediately; the periodic
  // heartbeat then refreshes every 20s. Skip when hidden — iOS will have
  // suspended us anyway and the server defaults sessions to "stale".
  if (!document.hidden) {
    postFocus(true);
    startFocusHeartbeat();
  }
}

function handleEvent(evt) {
  if (evt.type === 'snapshot') {
    els.thread.innerHTML = '';
    for (const e of evt.events) handleEvent(e);
    return;
  }
  switch (evt.type) {
    case 'user_message':
      appendUserMessage(evt.text, evt.attachments);
      break;
    case 'assistant_text':
      appendAssistantText(evt.text, evt.id);
      break;
    case 'assistant_delta':
      appendAssistantDelta(evt.text, evt.id);
      break;
    case 'tool_use':
      appendToolCard(evt);
      state.turnActivity = activityFor(evt);
      if (state.generating) updateTypingCaption();
      break;
    case 'tool_result':
      updateToolCard(evt);
      state.turnActivity = null; // back to "Thinking"
      if (state.generating) updateTypingCaption();
      break;
    case 'permission_request':
      appendPermissionCard(evt);
      break;
    case 'permission_resolved':
      // Fires when the user answered via notification action while the app
      // was also open — hide the in-thread card so they don't see stale buttons.
      removePermissionCard(evt.id);
      break;
    case 'generation_started':
      setGenerating(true, evt.started_at);
      break;
    case 'usage':
      addUsageTokens(evt);
      break;
    case 'generation_ended':
      setGenerating(false);
      if (evt.subtype === 'interrupted') appendSystem('Stopped');
      break;
    case 'system_message':
      appendSystem(evt.text);
      break;
    case 'error':
      appendSystem(`Error: ${evt.message}`);
      break;
    case 'session_ended':
      appendResumePrompt();
      setGenerating(false);
      break;
  }
  scrollToBottom();
}

// --- Rendering -----------------------------------------------------------

function appendUserMessage(text, attachments) {
  const div = document.createElement('div');
  div.className = 'msg user';
  div.textContent = text || '';
  for (const a of attachments || []) {
    const img = document.createElement('img');
    // Live events emit path strings (/data/uploads/xxx.png); history replay
    // emits {dataUrl} extracted from the jsonl's base64 image block, because
    // the original upload file is no longer on disk by then.
    if (a && typeof a === 'object' && a.dataUrl) {
      img.src = a.dataUrl;
    } else {
      const name = typeof a === 'string' ? a.split('/').pop() : (a.name || String(a).split('/').pop());
      img.src = `api/uploads/${name}`;
    }
    div.appendChild(img);
  }
  els.thread.appendChild(div);
}

function appendAssistantText(text, id) {
  let el = id && document.getElementById(`a-${id}`);
  if (!el) {
    el = document.createElement('div');
    el.className = 'msg assistant';
    if (id) el.id = `a-${id}`;
    els.thread.appendChild(el);
  }
  el.dataset.raw = text || '';
  el.innerHTML = renderMarkdown(el.dataset.raw);
}

function appendAssistantDelta(text, id) {
  let el = id && document.getElementById(`a-${id}`);
  if (!el) {
    el = document.createElement('div');
    el.className = 'msg assistant';
    if (id) el.id = `a-${id}`;
    el.dataset.raw = '';
    els.thread.appendChild(el);
  }
  el.dataset.raw = (el.dataset.raw || '') + (text || '');
  el.innerHTML = renderMarkdown(el.dataset.raw);
}

// Minimal markdown renderer: fenced code blocks, inline code, bold, italic,
// links, paragraphs/line breaks, simple lists. Returns HTML string.
function renderMarkdown(src) {
  src = String(src || '');
  const segments = [];
  let i = 0;
  while (i < src.length) {
    const fence = src.indexOf('```', i);
    if (fence === -1) {
      segments.push({ type: 'text', content: src.slice(i) });
      break;
    }
    if (fence > i) segments.push({ type: 'text', content: src.slice(i, fence) });
    const lineEnd = src.indexOf('\n', fence + 3);
    const lang = lineEnd === -1 ? '' : src.slice(fence + 3, lineEnd).trim();
    const codeStart = lineEnd === -1 ? src.length : lineEnd + 1;
    const closing = src.indexOf('```', codeStart);
    if (closing === -1) {
      segments.push({ type: 'code', lang, content: src.slice(codeStart), incomplete: true });
      break;
    }
    let codeEnd = closing;
    if (src[codeEnd - 1] === '\n') codeEnd--;
    segments.push({ type: 'code', lang, content: src.slice(codeStart, codeEnd) });
    i = closing + 3;
    if (src[i] === '\n') i++;
  }
  return segments.map((seg) => (seg.type === 'code' ? renderCodeBlock(seg) : renderProse(seg.content))).join('');
}

function renderCodeBlock(seg) {
  const lang = escapeHtml(seg.lang || '');
  const code = escapeHtml(seg.content);
  const copy = seg.incomplete ? '' : '<button class="code-copy" type="button">Copy</button>';
  const label = lang || 'code';
  return `<div class="code-block"><div class="code-head"><span class="code-lang">${escapeHtml(label)}</span>${copy}</div><pre><code>${code}</code></pre></div>`;
}

function renderProse(text) {
  const blocks = text.split(/\n{2,}/);
  return blocks.map((raw) => {
    const block = raw.replace(/^\n+|\n+$/g, '');
    if (!block) return '';
    const lines = block.split('\n');
    if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
      return `<ul>${lines.map((l) => `<li>${formatInline(l.replace(/^\s*[-*]\s+/, ''))}</li>`).join('')}</ul>`;
    }
    if (lines.every((l) => /^\s*\d+\.\s+/.test(l))) {
      return `<ol>${lines.map((l) => `<li>${formatInline(l.replace(/^\s*\d+\.\s+/, ''))}</li>`).join('')}</ol>`;
    }
    return `<p>${lines.map(formatInline).join('<br>')}</p>`;
  }).join('');
}

function formatInline(text) {
  let out = escapeHtml(text);
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\x00${codes.length - 1}\x00`;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
  out = out.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) => {
    if (!/^(https?:\/\/|\/|#)/i.test(u)) return m;
    return `<a href="${u}" target="_blank" rel="noopener">${t}</a>`;
  });
  out = out.replace(/\x00(\d+)\x00/g, (_, idx) => `<code>${codes[+idx]}</code>`);
  return out;
}

function friendlyToolLabel(name) {
  if (!name || !name.startsWith('mcp__')) return name || '?';
  const parts = name.split('__');
  const server = parts[1] || '';
  if (server === 'home-assistant') return 'HA';
  return server.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) || name;
}

function mcpInputSummary(name, input) {
  if (!name || !name.startsWith('mcp__')) return '';
  const tool = name.split('__').slice(2).join('__');
  const i = input && typeof input === 'object' ? input : {};
  const ent = (v) => Array.isArray(v) ? v.join(', ') : (v || '');
  const clip = (s, n = 50) => {
    const t = String(s);
    return t.length > n ? t.slice(0, n - 1) + '…' : t;
  };
  switch (tool) {
    case 'ha_call_service': {
      const action = i.domain && i.service ? `${i.domain}.${i.service}` : (i.service || i.domain || 'call_service');
      const target = ent(i.entity_id || (i.target && i.target.entity_id));
      return target ? `${action} → ${clip(target)}` : action;
    }
    case 'ha_get_state': return i.entity_id ? `Get state: ${clip(ent(i.entity_id))}` : 'Get state';
    case 'ha_get_bulk_status': {
      const e = ent(i.entity_ids || i.entity_id);
      const n = Array.isArray(i.entity_ids) ? i.entity_ids.length : 0;
      return n > 3 ? `Bulk status × ${n}` : (e ? `Bulk status: ${clip(e)}` : 'Bulk status');
    }
    case 'ha_bulk_control': {
      const n = Array.isArray(i.entity_ids) ? i.entity_ids.length : 0;
      const svc = i.domain && i.service ? `${i.domain}.${i.service}` : (i.service || 'control');
      return n ? `${svc} × ${n} entities` : svc;
    }
    case 'ha_search_entities': return i.query ? `Search entities: "${clip(i.query, 40)}"` : 'Search entities';
    case 'ha_deep_search': return i.query ? `Deep search: "${clip(i.query, 40)}"` : 'Deep search';
    case 'ha_eval_template': return i.template ? `Template: ${clip(i.template, 60)}` : 'Template';
    case 'ha_get_overview': return 'Get overview';
    case 'ha_get_logbook': return i.entity_id ? `Logbook: ${clip(ent(i.entity_id))}` : 'Logbook';
    case 'ha_get_domain_docs': return i.domain ? `Docs: ${i.domain}` : 'Domain docs';
    case 'ha_get_operation_status': return i.operation_id ? `Op status: ${i.operation_id}` : 'Op status';
    case 'ha_config_list_dashboards': return 'List dashboards';
    case 'ha_config_list_helpers': return 'List helpers';
    case 'ha_config_get_automation': return i.id ? `Get automation: ${i.id}` : 'Get automation';
    case 'ha_config_get_script': return i.id ? `Get script: ${i.id}` : 'Get script';
    case 'ha_config_get_dashboard': return i.dashboard_id ? `Get dashboard: ${i.dashboard_id}` : 'Get dashboard';
    case 'ha_config_set_automation': return i.id ? `Save automation: ${i.id}` : 'Save automation';
    case 'ha_config_set_script': return i.id ? `Save script: ${i.id}` : 'Save script';
    case 'ha_config_set_helper': return i.id ? `Save helper: ${i.id}` : 'Save helper';
    case 'ha_config_set_dashboard': return i.dashboard_id ? `Save dashboard: ${i.dashboard_id}` : 'Save dashboard';
    case 'ha_config_remove_automation': return i.id ? `Remove automation: ${i.id}` : 'Remove automation';
    case 'ha_config_remove_helper': return i.id ? `Remove helper: ${i.id}` : 'Remove helper';
    case 'ha_config_remove_script': return i.id ? `Remove script: ${i.id}` : 'Remove script';
    case 'ha_config_delete_dashboard': return i.dashboard_id ? `Delete dashboard: ${i.dashboard_id}` : 'Delete dashboard';
    case 'ha_config_update_dashboard_metadata': return i.dashboard_id ? `Update dashboard meta: ${i.dashboard_id}` : 'Update dashboard meta';
    case 'ha_get_card_types': return 'List card types';
    case 'ha_get_card_documentation': return i.card_type ? `Card docs: ${i.card_type}` : 'Card docs';
    case 'ha_get_dashboard_guide': return 'Dashboard guide';
    case 'ha_backup_create': return 'Create backup';
    case 'ha_backup_restore': return i.backup_id ? `Restore backup: ${i.backup_id}` : 'Restore backup';
    default: return tool.replace(/^ha_/, '').replace(/_/g, ' ');
  }
}

function appendToolCard(evt) {
  const card = document.createElement('div');
  card.className = 'tool-card';
  card.id = `tool-${evt.id}`;
  const isMcp = (evt.name || '').startsWith('mcp__');
  const label = isMcp ? friendlyToolLabel(evt.name) : evt.name;
  const derived = isMcp ? mcpInputSummary(evt.name, evt.input) : '';
  const summary = (isMcp && derived) ? derived : (evt.summary || 'running…');
  card.innerHTML = `
    <div class="tool-head" role="button" tabindex="0" aria-expanded="false">
      <span class="tool-caret">▸</span>
      <span class="tool-name">${escapeHtml(label)}</span>
      <span class="tool-dash">—</span>
      <span class="tool-summary">${escapeHtml(summary)}</span>
    </div>
    <div class="tool-body" hidden>
      <pre class="tool-input">${escapeHtml(JSON.stringify(evt.input || {}, null, 2))}</pre>
      <pre class="tool-output" hidden></pre>
    </div>
  `;
  const head = card.querySelector('.tool-head');
  const body = card.querySelector('.tool-body');
  head.addEventListener('click', () => {
    const open = card.classList.toggle('open');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.hidden = !open;
  });
  els.thread.appendChild(card);
}

function updateToolCard(evt) {
  const card = document.getElementById(`tool-${evt.tool_id}`);
  if (!card) return;
  const out = card.querySelector('.tool-output');
  out.hidden = false;
  out.textContent = typeof evt.output === 'string' ? evt.output : JSON.stringify(evt.output, null, 2);
  if (evt.error) card.classList.add('error');
}

function appendPermissionCard(evt) {
  const card = document.createElement('div');
  card.className = 'permission';
  card.id = `perm-${evt.id}`;
  const domain = evt.domain || '';
  const isPlan = evt.tool === 'exit_plan_mode' || evt.tool === 'ExitPlanMode';
  const isBash = evt.tool === 'Bash';
  const domainLine = domain
    ? `<div class="domain-line">Domain: <strong>${escapeHtml(domain)}</strong></div>`
    : '';
  const allowDomainBtn = domain
    ? `<button class="approve-domain">Always Allow</button>`
    : '';
  const allowTurnBtn = isBash
    ? `<button class="approve-turn">Trust Bash this turn</button>`
    : '';
  let title;
  if (isPlan) {
    title = 'Plan ready — start coding?';
  } else if (evt.title) {
    title = `${escapeHtml(evt.title)}?`;
  } else {
    title = `Approve ${escapeHtml(evt.tool || 'action')}?`;
  }
  const approveLabel = isPlan ? 'Approve & start coding' : 'Allow once';
  const rejectLabel = isPlan ? 'Refine plan' : 'Reject';
  const planNotice = isPlan
    ? `<div class="plan-notice"><strong>Approve & start coding</strong> switches this chat out of plan mode and begins implementation. <strong>Refine plan</strong> stays in plan mode — Claude will ask what you'd like to change.</div>`
    : '';
  card.innerHTML = `
    <div class="title">${title}</div>
    ${domainLine}
    <div class="body">${escapeHtml(evt.description || '')}</div>
    ${planNotice}
    <div class="actions">
      <button class="approve">${approveLabel}</button>
      ${allowDomainBtn}
      ${allowTurnBtn}
      <button class="reject">${rejectLabel}</button>
    </div>
  `;
  const reqId = evt.id;
  card.querySelector('.approve').addEventListener('click', () => respondPermission(reqId, 'allow_once'));
  card.querySelector('.reject').addEventListener('click', () => respondPermission(reqId, 'reject'));
  const allowDomain = card.querySelector('.approve-domain');
  if (allowDomain) allowDomain.addEventListener('click', () => respondPermission(reqId, 'allow_domain'));
  const allowTurn = card.querySelector('.approve-turn');
  if (allowTurn) allowTurn.addEventListener('click', () => respondPermission(reqId, 'allow_turn'));
  els.thread.appendChild(card);
}

function removePermissionCard(id) {
  // Target by id when provided so a later card arriving mid-flight isn't
  // killed by a stale cleanup.
  const card = id ? document.getElementById(`perm-${id}`) : document.querySelector('.permission');
  if (card) card.remove();
}

function appendSystem(text) {
  const div = document.createElement('div');
  div.className = 'msg system';
  div.textContent = text;
  els.thread.appendChild(div);
}

function appendResumePrompt() {
  const sid = state.sessionId;
  const wrap = document.createElement('div');
  wrap.className = 'msg system resume-prompt';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'resume-btn';
  btn.textContent = 'Session ended — tap to resume';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Resuming…';
    try {
      await api(`api/sessions/${sid}/resume`, { method: 'POST' });
      wrap.remove();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = 'Resume failed — tap to retry';
    }
  });
  wrap.appendChild(btn);
  els.thread.appendChild(wrap);
}

function setGenerating(on, startedAt) {
  state.generating = on;
  els.send.hidden = on;
  els.stop.hidden = !on;
  const indicator = document.getElementById('typing-indicator');
  if (on) {
    // Prefer the server's timestamp so reconnecting (HA re-mounting the panel,
    // companion app foreground/background) shows the true elapsed seconds
    // instead of resetting to 0s. Falls back to client clock if absent.
    state.turnStartedAt = startedAt || Date.now();
    state.turnTokens = 0;
    state.turnActivity = null;
    indicator.hidden = false;
    updateTypingCaption();
    clearInterval(state.typingTimer);
    state.typingTimer = setInterval(updateTypingCaption, 1000);
  } else {
    indicator.hidden = true;
    clearInterval(state.typingTimer);
    state.typingTimer = null;
    refreshUsage();
  }
}

function updateTypingCaption() {
  const caption = document.getElementById('typing-caption');
  const secs = Math.max(0, Math.floor((Date.now() - state.turnStartedAt) / 1000));
  const a = state.turnActivity;
  const activity = a ? (a.target ? `${a.verb} ${a.target}` : a.verb) : 'Thinking';
  const parts = [activity, `${secs}s`];
  if (state.turnTokens) {
    const t = state.turnTokens;
    parts.push(t >= 1000 ? `${(t / 1000).toFixed(1)}k tokens` : `${t} tokens`);
  }
  caption.textContent = parts.join(' · ');
}

function activityFor(evt) {
  const name = evt.name || '?';
  const summary = (evt.summary || '').toString().trim();
  let verb;
  if (name === 'Read') verb = 'Reading';
  else if (name === 'Bash') verb = 'Running';
  else if (name === 'Grep' || name === 'Glob') verb = 'Searching';
  else if (name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'NotebookEdit') verb = 'Editing';
  else if (name === 'WebFetch') verb = 'Fetching';
  else if (name === 'ExitPlanMode') verb = 'Finalizing plan';
  else if (name.startsWith('mcp__')) verb = 'Calling';
  else verb = name;

  let target = summary;
  // For file paths, prefer just the basename so the caption stays readable.
  if (target && target.startsWith('/') && !target.includes(' ') && !target.includes('\n')) {
    const last = target.split('/').pop();
    if (last) target = last;
  }
  // For MCP tools, strip the noisy server prefix → "ha_call_service"
  if (name.startsWith('mcp__')) {
    target = name.split('__').slice(-1)[0];
  }
  // Cap target length so a giant Bash command doesn't blow out the caption.
  if (target.length > 40) target = target.slice(0, 37) + '…';
  return { verb, target };
}

function addUsageTokens(evt) {
  state.turnTokens += (evt.output_tokens || 0) + (evt.input_tokens || 0);
  if (state.generating) updateTypingCaption();
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    els.thread.scrollTop = els.thread.scrollHeight;
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// --- Drawer / sessions ---------------------------------------------------

function renderDrawer() {
  els.drawerBody.innerHTML = '';
  if (state.searchResults !== null) {
    renderSearchResults();
    return;
  }
  const byProject = new Map();
  byProject.set(null, []);
  for (const p of state.projects) byProject.set(p.id, []);
  for (const s of state.sessions) {
    const key = byProject.has(s.project_id) ? s.project_id : null;
    byProject.get(key).push(s);
  }
  // Render Unsorted first if it has any sessions, then projects in created order.
  const unsorted = byProject.get(null);
  if (unsorted.length) {
    els.drawerBody.appendChild(renderGroup(null, 'Unsorted', unsorted));
  }
  for (const p of state.projects) {
    els.drawerBody.appendChild(renderGroup(p.id, p.name, byProject.get(p.id)));
  }
  if (!state.projects.length && !unsorted.length) {
    const empty = document.createElement('p');
    empty.className = 'drawer-empty';
    empty.textContent = 'No conversations yet. Tap + to start one.';
    els.drawerBody.appendChild(empty);
  }
}

function renderSearchResults() {
  const results = state.searchResults || [];
  if (results.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'drawer-empty';
    empty.textContent = state.searchQuery.length < 2
      ? 'Type at least 2 characters to search.'
      : `No conversations match “${state.searchQuery}”.`;
    els.drawerBody.appendChild(empty);
    return;
  }
  const list = document.createElement('ul');
  list.className = 'search-results';
  for (const r of results) list.appendChild(renderSearchRow(r));
  els.drawerBody.appendChild(list);
}

function renderSearchRow(r) {
  const li = document.createElement('li');
  li.className = 'search-row';
  const title = document.createElement('div');
  title.className = 'search-row-title';
  title.textContent = r.title || 'Conversation';
  if (r.title_match) title.classList.add('title-hit');
  li.appendChild(title);
  for (const m of r.matches || []) {
    const snip = document.createElement('div');
    snip.className = `search-row-snippet role-${m.role}`;
    // before / after are user-provided strings — escape via textContent on
    // wrapper spans, then add a <mark> for the match in the middle.
    const beforeS = document.createElement('span');
    beforeS.textContent = (m.before || '').replace(/\s+/g, ' ');
    const mark = document.createElement('mark');
    mark.textContent = m.match;
    const afterS = document.createElement('span');
    afterS.textContent = (m.after || '').replace(/\s+/g, ' ');
    snip.append(beforeS, mark, afterS);
    li.appendChild(snip);
  }
  li.addEventListener('click', () => selectSession(r.session_id));
  return li;
}

let searchDebounce = null;

async function runSearch(q) {
  if (q.length < 2) {
    state.searchResults = q.length === 0 ? null : [];
    renderDrawer();
    return;
  }
  try {
    const data = await api(`api/search?q=${encodeURIComponent(q)}`);
    // Only apply if the query hasn't moved on since we sent the request.
    if (state.searchQuery !== q) return;
    state.searchResults = data.results || [];
    renderDrawer();
  } catch (_) { /* silent */ }
}

function onSearchInput() {
  const q = els.drawerSearch.value;
  state.searchQuery = q;
  els.drawerSearchClear.hidden = q.length === 0;
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => runSearch(q.trim()), 200);
}

function clearSearch() {
  els.drawerSearch.value = '';
  state.searchQuery = '';
  state.searchResults = null;
  els.drawerSearchClear.hidden = true;
  renderDrawer();
  els.drawerSearch.focus();
}

function renderGroup(projectId, name, sessions) {
  const group = document.createElement('section');
  group.className = 'project-group';
  const collapsed = projectId !== null && state.collapsedProjects.has(projectId);
  if (collapsed) group.classList.add('collapsed');

  const header = document.createElement('header');
  header.className = 'project-header';
  const caret = document.createElement('span');
  caret.className = 'caret';
  caret.textContent = '▸';
  const label = document.createElement('span');
  label.className = 'project-name';
  label.textContent = name;
  const count = document.createElement('span');
  count.className = 'project-count';
  count.textContent = sessions.length;
  header.append(caret, label, count);
  if (projectId !== null) {
    const menuBtn = document.createElement('button');
    menuBtn.className = 'row-menu';
    menuBtn.innerHTML = '⋯';
    menuBtn.setAttribute('aria-label', 'Project actions');
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openMenu(menuBtn, [
        { label: 'New chat here', action: () => createSession(projectId) },
        { label: 'Project instructions', action: () => openProjectNotes(projectId, name) },
        { label: 'Rename project', action: () => {
          const n = prompt('Project name?', name);
          if (n && n.trim()) renameProject(projectId, n.trim());
        }},
        { label: 'Delete project', danger: true, action: () => {
          if (confirm(`Delete project "${name}"? Chats inside will move to Unsorted.`)) deleteProject(projectId);
        }},
      ]);
    });
    header.append(menuBtn);
  }
  header.addEventListener('click', () => {
    if (projectId === null) return;
    if (state.collapsedProjects.has(projectId)) state.collapsedProjects.delete(projectId);
    else state.collapsedProjects.add(projectId);
    persistCollapsedProjects();
    renderDrawer();
  });
  group.appendChild(header);

  const list = document.createElement('ul');
  list.className = 'session-list';
  for (const s of sessions) list.appendChild(renderSessionRow(s));
  group.appendChild(list);
  return group;
}

function formatSessionDate(timestamp) {
  if (!timestamp) return '';
  const d = new Date(timestamp * 1000);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(d);
  target.setHours(0, 0, 0, 0);
  const diffDays = Math.round((today - target) / 86400000);
  if (diffDays <= 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return d.toLocaleDateString('en-GB', { weekday: 'long' });
  if (d.getFullYear() === today.getFullYear()) {
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  }
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

function renderSessionRow(s) {
  const li = document.createElement('li');
  if (s.id === state.sessionId) li.classList.add('active');
  const text = document.createElement('div');
  text.className = 'session-text';
  const title = document.createElement('span');
  title.className = 'session-title';
  title.textContent = s.title || 'Untitled';
  text.appendChild(title);
  const dateStr = formatSessionDate(s.created_at);
  if (dateStr) {
    const date = document.createElement('span');
    date.className = 'session-date';
    date.textContent = dateStr;
    text.appendChild(date);
  }
  text.addEventListener('click', () => selectSession(s.id));
  li.appendChild(text);

  const menuBtn = document.createElement('button');
  menuBtn.className = 'row-menu';
  menuBtn.innerHTML = '⋯';
  menuBtn.setAttribute('aria-label', 'Chat actions');
  menuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const moveTargets = [
      ...(s.project_id !== null ? [{ label: '— Unsorted —', action: () => moveSession(s.id, null) }] : []),
      ...state.projects
        .filter(p => p.id !== s.project_id)
        .map(p => ({ label: p.name, action: () => moveSession(s.id, p.id) })),
    ];
    openMenu(menuBtn, [
      { label: 'Rename', action: () => {
        const n = prompt('Chat name?', s.title);
        if (n && n.trim()) renameSession(s.id, n.trim());
      }},
      ...(moveTargets.length ? [{ label: 'Move to…', submenu: moveTargets }] : []),
      { label: 'Delete', danger: true, action: () => {
        if (confirm('Delete this conversation?')) deleteSession(s.id);
      }},
    ]);
  });
  li.appendChild(menuBtn);
  return li;
}

// --- Action menu ---------------------------------------------------------

function openMenu(trigger, items) {
  closeMenu();
  const menu = els.actionMenu;
  menu.innerHTML = '';
  for (const item of items) {
    const btn = document.createElement('button');
    btn.className = 'menu-item' + (item.danger ? ' danger' : '');
    btn.textContent = item.label + (item.submenu ? ' ▸' : '');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (item.submenu) {
        openMenu(trigger, item.submenu);
      } else {
        closeMenu();
        item.action();
      }
    });
    menu.appendChild(btn);
  }
  const rect = trigger.getBoundingClientRect();
  menu.style.right = `${window.innerWidth - rect.right}px`;
  menu.style.top = '0px';
  menu.style.visibility = 'hidden';
  menu.hidden = false;
  const menuHeight = menu.offsetHeight;
  const spaceBelow = window.innerHeight - rect.bottom;
  const flipUp = spaceBelow < menuHeight + 8;
  menu.style.top = flipUp
    ? `${Math.max(8, rect.top - menuHeight - 4)}px`
    : `${rect.bottom + 4}px`;
  menu.style.visibility = '';
  setTimeout(() => document.addEventListener('click', closeMenu, { once: true }), 0);
}

function closeMenu() {
  els.actionMenu.hidden = true;
}

function openDrawer() { els.drawer.hidden = false; }
function closeDrawer() { els.drawer.hidden = true; }

// --- Attachments ---------------------------------------------------------

async function addAttachment(file) {
  if (!state.sessionId) await createSession();
  const reader = new FileReader();
  reader.onload = () => {
    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    thumb.style.backgroundImage = `url(${reader.result})`;
    const x = document.createElement('button');
    x.textContent = '×';
    thumb.appendChild(x);
    els.attachments.appendChild(thumb);
    uploadFile(file).then(({ path, name }) => {
      state.attachments.push({ path, name, dataUrl: reader.result, thumb });
      x.addEventListener('click', () => {
        thumb.remove();
        state.attachments = state.attachments.filter(a => a.path !== path);
      });
    }).catch(err => {
      thumb.remove();
      appendSystem(`Upload failed: ${err.message}`);
    });
  };
  reader.readAsDataURL(file);
}

function clearAttachments() {
  state.attachments = [];
  els.attachments.innerHTML = '';
}

// --- Composer wiring -----------------------------------------------------

els.input.addEventListener('input', () => {
  els.input.style.height = 'auto';
  els.input.style.height = Math.min(140, els.input.scrollHeight) + 'px';
});

els.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !isCoarsePointer()) {
    e.preventDefault();
    submit();
  }
  if (e.key === 'Escape' && state.generating) interrupt();
});

els.input.addEventListener('paste', (e) => {
  for (const item of e.clipboardData?.items || []) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) addAttachment(file);
    }
  }
});

els.send.addEventListener('click', submit);
els.stop.addEventListener('click', interrupt);

els.fileInput.addEventListener('change', () => {
  for (const f of els.fileInput.files) addAttachment(f);
  els.fileInput.value = '';
});

// Composer "+" panel: replaces attach + mode-chip with a single button that
// opens a sheet of options. Tapping + blurs the textarea so the keyboard
// dismisses cleanly into the panel's footprint (iMessage / WhatsApp pattern).
function setPanelOpen(open) {
  els.composerPanel.hidden = !open;
  els.composerPlus.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (!open) setComposerView('main');
}
els.composerPlus.addEventListener('click', () => {
  const isOpen = !els.composerPanel.hidden;
  if (isOpen) {
    setPanelOpen(false);
    els.input.focus();
  } else {
    els.input.blur();
    setPanelOpen(true);
  }
});
els.input.addEventListener('focus', () => setPanelOpen(false));
els.cpPhotos.addEventListener('click', () => {
  setPanelOpen(false);
  els.fileInput.click();
});
els.cpPlan.addEventListener('click', () => {
  setPermissionMode(currentMode() === 'plan' ? 'default' : 'plan');
});
els.cpModel.addEventListener('click', (e) => {
  e.stopPropagation();
  if (!state.sessionId) return;
  setComposerView('models');
});
document.querySelectorAll('.cp-model-pick').forEach((btn) => {
  btn.addEventListener('click', () => {
    const raw = btn.getAttribute('data-model') || '';
    setSessionModel(raw || null);
    setComposerView('main');
  });
});

function setComposerView(view) {
  els.composerPanel.dataset.view = view;
  const main = els.composerPanel.querySelector('.cp-view-main');
  const models = els.composerPanel.querySelector('.cp-view-models');
  if (main) main.hidden = view !== 'main';
  if (models) models.hidden = view !== 'models';
  if (view === 'models') renderModelPicks();
}

function renderModelPicks() {
  const sess = state.sessions.find(x => x.id === state.sessionId) || {};
  const cur = sess.model || null;
  document.querySelectorAll('.cp-model-pick').forEach((btn) => {
    const id = btn.getAttribute('data-model') || null;
    btn.classList.toggle('active', (id || null) === (cur || null));
  });
}

els.drawerToggle.addEventListener('click', openDrawer);
els.drawerClose.addEventListener('click', closeDrawer);
els.drawerSearch.addEventListener('input', onSearchInput);
els.drawerSearchClear.addEventListener('click', clearSearch);
els.drawer.addEventListener('click', (e) => { if (e.target === els.drawer) closeDrawer(); });
els.newSession.addEventListener('click', () => createSession(null));
els.newSessionDrawer.addEventListener('click', () => { createSession(null); closeDrawer(); });
els.newSessionMore.addEventListener('click', (e) => {
  e.stopPropagation();
  openMenu(els.newSessionMore, [
    { label: 'New project', action: createProject },
  ]);
});
function renderModeToggle(mode) {
  els.cpPlan.dataset.mode = mode;
  els.cpPlan.classList.toggle('active', mode === 'plan');
  els.input.classList.toggle('plan-mode', mode === 'plan');
}

function renderModelTile() {
  if (!els.cpModelLabel) return;
  if (!state.sessionId) {
    els.cpModelLabel.textContent = 'Model';
    els.cpModel.classList.remove('active');
    return;
  }
  const sess = state.sessions.find(x => x.id === state.sessionId) || {};
  const cur = sess.model || null;
  const label = (MODELS.find(m => m.id === cur) || MODELS[0]).label;
  els.cpModelLabel.textContent = label;
  els.cpModel.classList.toggle('active', cur != null);
}

function currentMode() {
  return els.cpPlan.dataset.mode || 'default';
}

async function setPermissionMode(next) {
  if (!state.sessionId) return;
  const cur = currentMode();
  if (cur === next) return;
  renderModeToggle(next);
  try {
    await api(`api/sessions/${state.sessionId}/permission_mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: next }),
    });
    const s = state.sessions.find(x => x.id === state.sessionId);
    if (s) s.permission_mode = next;
    if (next === 'plan' && !localStorage.getItem('planModeHintShown')) {
      appendSystem('Plan mode on: Claude will investigate and propose, but won\u2019t make changes until you turn it off.');
      localStorage.setItem('planModeHintShown', '1');
    }
  } catch (err) {
    renderModeToggle(cur);
    appendSystem(`Mode change failed: ${err.message}`);
  }
}

els.thread.addEventListener('click', async (e) => {
  const btn = e.target.closest('.code-copy');
  if (!btn) return;
  const code = btn.closest('.code-block')?.querySelector('code');
  if (!code) return;
  const text = code.textContent || '';
  const ok = await copyText(text);
  const orig = btn.dataset.label || btn.textContent;
  btn.dataset.label = orig;
  btn.textContent = ok ? 'Copied' : 'Copy failed';
  setTimeout(() => { btn.textContent = btn.dataset.label || 'Copy'; }, 1500);
});

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {}
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

// --- Settings ------------------------------------------------------------

const settingsEls = {
  panel: document.getElementById('settings'),
  toggleBtn: document.getElementById('settings-toggle'),
  closeBtn: document.getElementById('settings-close'),
  saveBtn: document.getElementById('settings-save'),
  theme: document.getElementById('theme-select'),
  askBash: document.getElementById('ask-bash'),
  askWebfetch: document.getElementById('ask-webfetch'),
  allowlist: document.getElementById('webfetch-allowlist'),
  allowlistItems: document.getElementById('webfetch-allowlist-items'),
  enabled: document.getElementById('ha-mcp-enabled'),
  fields: document.getElementById('ha-mcp-fields'),
  url: document.getElementById('ha-url'),
  token: document.getElementById('ha-token'),
  tokenStatus: document.getElementById('ha-token-status'),
  status: document.getElementById('settings-status'),
};

// In-memory copy of the persisted allowlist while the settings modal is open.
// Edits (removals) only commit when the user hits Save.
let webfetchDomains = [];
let safeBashEnabled = [];   // command keys currently ticked
let safeBashCatalog = {};   // {command: description} from the server
let notifyDevices = [];     // selected notify.mobile_app_* service names

function renderSafeBashList() {
  const ul = document.getElementById('safe-bash-list');
  if (!ul) return;
  ul.innerHTML = '';
  const cmds = Object.keys(safeBashCatalog).sort();
  for (const cmd of cmds) {
    const li = document.createElement('li');
    const id = `safe-bash-${cmd.replace(/\s+/g, '_')}`;
    li.innerHTML = `
      <label for="${id}">
        <input type="checkbox" id="${id}" data-cmd="${cmd}">
        <span class="safe-bash-cmd"><code>${escapeHtml(cmd)}</code></span>
        <span class="safe-bash-desc">${escapeHtml(safeBashCatalog[cmd] || '')}</span>
      </label>
    `;
    const cb = li.querySelector('input');
    cb.checked = safeBashEnabled.includes(cmd);
    cb.addEventListener('change', () => {
      if (cb.checked) {
        if (!safeBashEnabled.includes(cmd)) safeBashEnabled.push(cmd);
      } else {
        safeBashEnabled = safeBashEnabled.filter(c => c !== cmd);
      }
    });
    ul.appendChild(li);
  }
}

async function renderNotifyDevices() {
  const ul = document.getElementById('notify-devices-list');
  const status = document.getElementById('notify-devices-status');
  if (!ul) return;
  ul.innerHTML = '';
  status.hidden = true;
  if (!settingsEls.enabled.checked) {
    status.hidden = false;
    status.textContent = 'Enable Home Assistant integration above to pick devices.';
    return;
  }
  let targets;
  try {
    targets = await api('api/ha/notify_targets');
  } catch (err) {
    status.hidden = false;
    status.textContent = `Couldn't reach Home Assistant: ${err.message}`;
    return;
  }
  if (!targets.length) {
    status.hidden = false;
    status.textContent = 'No mobile_app notify services found. Install the HA companion app and link your phone first.';
    return;
  }
  for (const t of targets) {
    const li = document.createElement('li');
    const id = `notify-${t.service.replace(/\W+/g, '_')}`;
    li.innerHTML = `
      <label for="${id}">
        <input type="checkbox" id="${id}" data-service="${escapeHtml(t.service)}">
        <span class="safe-bash-cmd"><code>${escapeHtml(t.label)}</code></span>
        <span class="safe-bash-desc">${escapeHtml(t.service)}</span>
      </label>
    `;
    const cb = li.querySelector('input');
    cb.checked = notifyDevices.includes(t.service);
    cb.addEventListener('change', () => {
      if (cb.checked) {
        if (!notifyDevices.includes(t.service)) notifyDevices.push(t.service);
      } else {
        notifyDevices = notifyDevices.filter(s => s !== t.service);
      }
    });
    ul.appendChild(li);
  }
}

function renderAllowlist() {
  settingsEls.allowlistItems.innerHTML = '';
  if (!webfetchDomains.length) {
    settingsEls.allowlist.hidden = true;
    return;
  }
  settingsEls.allowlist.hidden = false;
  for (const host of webfetchDomains) {
    const li = document.createElement('li');
    li.textContent = host;
    const x = document.createElement('button');
    x.textContent = '×';
    x.setAttribute('aria-label', `Remove ${host}`);
    x.addEventListener('click', () => {
      webfetchDomains = webfetchDomains.filter(h => h !== host);
      renderAllowlist();
    });
    li.appendChild(x);
    settingsEls.allowlistItems.appendChild(li);
  }
}

// --- Warning-confirm modal -----------------------------------------------

const warnEls = {
  panel: document.getElementById('warn-modal'),
  title: document.getElementById('warn-title'),
  body: document.getElementById('warn-body'),
  cancel: document.getElementById('warn-cancel'),
  accept: document.getElementById('warn-accept'),
};

function confirmDangerous({ title, body, acceptLabel = 'I understand, turn off' }) {
  return new Promise((resolve) => {
    warnEls.title.textContent = title;
    warnEls.body.textContent = body;
    warnEls.accept.textContent = acceptLabel;
    warnEls.panel.hidden = false;
    const cleanup = (result) => {
      warnEls.panel.hidden = true;
      warnEls.accept.removeEventListener('click', onAccept);
      warnEls.cancel.removeEventListener('click', onCancel);
      resolve(result);
    };
    const onAccept = () => cleanup(true);
    const onCancel = () => cleanup(false);
    warnEls.accept.addEventListener('click', onAccept);
    warnEls.cancel.addEventListener('click', onCancel);
  });
}

function syncHaFieldsVisibility() {
  settingsEls.fields.hidden = !settingsEls.enabled.checked;
}

const consentEls = {
  panel: document.getElementById('ha-mcp-consent'),
  allow: document.getElementById('ha-mcp-consent-allow'),
  cancel: document.getElementById('ha-mcp-consent-cancel'),
};

function askConsent() {
  return new Promise((resolve) => {
    consentEls.panel.hidden = false;
    const cleanup = (result) => {
      consentEls.panel.hidden = true;
      consentEls.allow.removeEventListener('click', onAllow);
      consentEls.cancel.removeEventListener('click', onCancel);
      resolve(result);
    };
    const onAllow = () => cleanup(true);
    const onCancel = () => cleanup(false);
    consentEls.allow.addEventListener('click', onAllow);
    consentEls.cancel.addEventListener('click', onCancel);
  });
}

async function onToggleChange() {
  if (settingsEls.enabled.checked) {
    const ok = await askConsent();
    if (!ok) {
      settingsEls.enabled.checked = false;
    }
  }
  syncHaFieldsVisibility();
  renderNotifyDevices();
}

async function openSettings() {
  try {
    settingsEls.theme.value = localStorage.getItem('theme') || 'dark';
    const s = await api('api/settings');
    settingsEls.askBash.checked = !!s.ask_bash;
    settingsEls.askWebfetch.checked = !!s.ask_webfetch;
    webfetchDomains = (s.webfetch_allowed_domains || []).slice();
    renderAllowlist();
    safeBashCatalog = s.safe_bash_commands || {};
    safeBashEnabled = (s.bash_auto_allow || []).slice();
    renderSafeBashList();
    notifyDevices = (s.notify_devices || []).slice();
    settingsEls.enabled.checked = !!s.ha_mcp_enabled;
    settingsEls.url.value = s.ha_url || '';
    settingsEls.token.value = '';
    settingsEls.tokenStatus.textContent = s.ha_token_set
      ? 'A token is already saved. Leave blank to keep it, or paste a new one to replace.'
      : 'No token saved yet.';
    settingsEls.status.hidden = true;
    syncHaFieldsVisibility();
    // Must run AFTER settingsEls.enabled.checked is set — the picker is
    // gated on the HA-integration toggle and would otherwise see stale state.
    renderNotifyDevices();
    settingsEls.panel.hidden = false;
  } catch (e) {
    alert(`Failed to load settings: ${e.message}`);
  }
}

function closeSettings() { settingsEls.panel.hidden = true; }

const haBannerEl = document.getElementById('ha-banner');
const haBannerTextEl = haBannerEl ? haBannerEl.querySelector('.ha-banner-text') : null;
const haBannerCtaEl = haBannerEl ? haBannerEl.querySelector('.ha-banner-cta') : null;
if (haBannerCtaEl) haBannerCtaEl.addEventListener('click', () => openSettings());

function setHaBanner(text) {
  if (!haBannerEl) return;
  if (text) {
    if (haBannerTextEl) haBannerTextEl.textContent = text;
    haBannerEl.hidden = false;
    document.body.classList.add('has-ha-banner');
  } else {
    haBannerEl.hidden = true;
    document.body.classList.remove('has-ha-banner');
  }
}

async function updateHaBanner() {
  try {
    const s = await api('api/settings');
    if (s.ha_mcp_enabled && (!s.ha_url || !s.ha_token_set)) {
      setHaBanner('Home Assistant token missing — Claude can\u2019t control your house yet. Tap to add a Long-Lived Access Token.');
    } else {
      setHaBanner(null);
    }
  } catch {
    setHaBanner(null);
  }
}

// --- Usage meter + 5h-window banner --------------------------------------

const usageEls = {
  ringBtn: document.getElementById('usage-ring-btn'),
  banner: document.getElementById('usage-banner'),
};
const usageBannerTextEl = usageEls.banner ? usageEls.banner.querySelector('.ha-banner-text') : null;
const usageBannerCtaEl = usageEls.banner ? usageEls.banner.querySelector('.ha-banner-cta') : null;
if (usageEls.ringBtn) usageEls.ringBtn.addEventListener('click', () => toggleCost());
if (usageBannerCtaEl) usageBannerCtaEl.addEventListener('click', () => openCost());

function setMeterTone(el, p) {
  if (!el) return;
  el.classList.toggle('warn', p != null && p >= 70 && p < 90);
  el.classList.toggle('danger', p != null && p >= 90);
}

function updateUsageIcons(pct) {
  const p100 = pct == null ? 0 : Math.max(0, Math.min(100, Math.round(pct * 100)));
  const ring = usageEls.ringBtn;
  if (ring) {
    const fill = ring.querySelector('.ring-fill');
    if (fill) fill.setAttribute('stroke-dasharray', `${p100} 100`);
    setMeterTone(ring, pct == null ? null : p100);
  }
}

function setUsageBanner(text) {
  if (!usageEls.banner) return;
  if (text) {
    if (usageBannerTextEl) usageBannerTextEl.textContent = text;
    usageEls.banner.hidden = false;
    document.body.classList.add('has-usage-banner');
  } else {
    usageEls.banner.hidden = true;
    document.body.classList.remove('has-usage-banner');
  }
}

let _usageLastFetch = 0;
const USAGE_THROTTLE_MS = 5 * 60 * 1000;
async function refreshUsage(force = false) {
  const now = Date.now();
  if (!force && now - _usageLastFetch < USAGE_THROTTLE_MS) return;
  _usageLastFetch = now;
  try {
    const u = await api('api/usage');
    updateUsageIcons(u.five_hour_pct);
    const p = u.five_hour_pct == null ? 0 : Math.round(u.five_hour_pct * 100);
    if (p >= 90) {
      const when = u.five_hour_reset
        ? fmtUntil(u.five_hour_reset).replace(/^Resets /, 'resets ')
        : 'resets soon';
      setUsageBanner(`${p}% of your 5-hour Claude quota used — ${when}.`);
    } else {
      setUsageBanner(null);
    }
  } catch {
    // Silent: usage check failed, leave icons in last-known state.
  }
}

async function saveSettings() {
  settingsEls.saveBtn.disabled = true;
  settingsEls.saveBtn.textContent = 'Saving…';
  try {
    await api('api/settings', {
      method: 'POST',
      body: JSON.stringify({
        ha_mcp_enabled: settingsEls.enabled.checked,
        ha_url: settingsEls.url.value.trim(),
        ha_token: settingsEls.token.value.trim() || null,
        ask_bash: settingsEls.askBash.checked,
        ask_webfetch: settingsEls.askWebfetch.checked,
        webfetch_allowed_domains: webfetchDomains,
        bash_auto_allow: safeBashEnabled,
        notify_devices: notifyDevices,
      }),
    });
    const savedMsg = 'Saved. Start a new chat (or use \u201cSummarize & start fresh\u201d on an existing one) to apply.';
    settingsEls.status.hidden = false;
    settingsEls.status.classList.remove('error');
    if (settingsEls.enabled.checked) {
      settingsEls.status.textContent = 'Saved. Testing HA credentials\u2026';
      try {
        const r = await api('api/ha/test_token', { method: 'POST', body: JSON.stringify({}) });
        if (r && r.ok) {
          settingsEls.status.textContent = 'Saved. HA connection verified. ' + savedMsg.replace(/^Saved\. /, '');
        } else {
          settingsEls.status.classList.add('error');
          settingsEls.status.textContent = `Saved, but HA test failed: ${(r && r.error) || 'unknown error'}`;
        }
      } catch (e) {
        settingsEls.status.classList.add('error');
        settingsEls.status.textContent = `Saved, but HA test failed: ${e.message}`;
      }
      updateHaBanner();
    } else {
      settingsEls.status.textContent = savedMsg;
      updateHaBanner();
    }
  } catch (e) {
    settingsEls.status.hidden = false;
    settingsEls.status.textContent = `Save failed: ${e.message}`;
  } finally {
    settingsEls.saveBtn.disabled = false;
    settingsEls.saveBtn.textContent = 'Save settings';
  }
}

// --- Project instructions modal -----------------------------------------

const notesEls = {
  panel: document.getElementById('project-notes-modal'),
  close: document.getElementById('project-notes-close'),
  title: document.getElementById('project-notes-title'),
  path: document.getElementById('project-notes-path'),
  text: document.getElementById('project-notes-text'),
  save: document.getElementById('project-notes-save'),
  status: document.getElementById('project-notes-status'),
};
let currentNotesProjectId = null;

async function openProjectNotes(projectId, projectName) {
  currentNotesProjectId = projectId;
  notesEls.title.textContent = `${projectName} — instructions`;
  const proj = state.projects.find(p => p.id === projectId);
  const slug = (proj && proj.slug) || projectName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  notesEls.path.textContent = `/config/claude-project-notes/${slug}.md`;
  notesEls.text.value = '…';
  notesEls.status.hidden = true;
  notesEls.panel.hidden = false;
  try {
    const r = await api(`api/projects/${projectId}/notes`);
    notesEls.text.value = r.notes || '';
  } catch (err) {
    notesEls.text.value = '';
    notesEls.status.hidden = false;
    notesEls.status.textContent = `Load failed: ${err.message}`;
  }
}

async function saveProjectNotes() {
  if (!currentNotesProjectId) return;
  try {
    await api(`api/projects/${currentNotesProjectId}/notes`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: notesEls.text.value }),
    });
    notesEls.status.hidden = false;
    notesEls.status.textContent = 'Saved. Active for new and resumed chats.';
  } catch (err) {
    notesEls.status.hidden = false;
    notesEls.status.textContent = `Save failed: ${err.message}`;
  }
}

notesEls.close.addEventListener('click', () => { notesEls.panel.hidden = true; });
notesEls.panel.addEventListener('click', (e) => { if (e.target === notesEls.panel) notesEls.panel.hidden = true; });
notesEls.save.addEventListener('click', saveProjectNotes);

// --- Chat actions menu ---------------------------------------------------

const chatActionsBtn = document.getElementById('chat-actions');
const costEls = {
  panel: document.getElementById('cost-modal'),
  close: document.getElementById('cost-close'),
  pct5h: document.getElementById('usage-5h-pct'),
  bar5h: document.getElementById('usage-5h-bar'),
  reset5h: document.getElementById('usage-5h-reset'),
  pct7d: document.getElementById('usage-7d-pct'),
  bar7d: document.getElementById('usage-7d-bar'),
  reset7d: document.getElementById('usage-7d-reset'),
};

function fmtUntil(ts) {
  if (!ts) return 'Resets —';
  const ms = ts * 1000 - Date.now();
  if (ms <= 0) return 'Resetting now';
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const d = Math.floor(h / 24);
  const date = new Date(ts * 1000);
  const when = date.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  if (d >= 1) return `Resets ${when} (${d}d ${h % 24}h)`;
  if (h >= 1) return `Resets ${when} (${h}h ${m}m)`;
  return `Resets ${when} (${m}m)`;
}

function setUsageBlock(pctEl, barEl, resetEl, pct, ts) {
  const p = pct == null ? null : Math.round(pct * 100);
  pctEl.textContent = p == null ? '—' : `${p}%`;
  barEl.style.width = p == null ? '0%' : `${Math.min(100, p)}%`;
  barEl.classList.toggle('danger', p != null && p >= 90);
  barEl.classList.toggle('warn', p != null && p >= 70 && p < 90);
  resetEl.textContent = fmtUntil(ts);
}

async function openCost() {
  costEls.pct5h.textContent = '…';
  costEls.pct7d.textContent = '…';
  costEls.bar5h.style.width = '0%';
  costEls.bar7d.style.width = '0%';
  costEls.reset5h.textContent = 'Resets —';
  costEls.reset7d.textContent = 'Resets —';
  costEls.panel.hidden = false;
  try {
    const u = await api('api/usage');
    setUsageBlock(costEls.pct5h, costEls.bar5h, costEls.reset5h, u.five_hour_pct, u.five_hour_reset);
    setUsageBlock(costEls.pct7d, costEls.bar7d, costEls.reset7d, u.seven_day_pct, u.seven_day_reset);
  } catch (err) {
    appendSystem(`Usage lookup failed: ${err.message}`);
    costEls.panel.hidden = true;
  }
}

function toggleCost() {
  if (costEls.panel.hidden) {
    openCost();
  } else {
    costEls.panel.hidden = true;
  }
}

async function clearContext() {
  if (!state.sessionId) return;
  if (!confirm('Clear this conversation\u2019s context? The visible history is wiped and Claude starts fresh.')) return;
  try {
    await api(`api/sessions/${state.sessionId}/clear`, { method: 'POST' });
    els.thread.innerHTML = '';
  } catch (err) {
    appendSystem(`Clear failed: ${err.message}`);
  }
}

const MODELS = [
  { id: null, label: 'Auto' },
  { id: 'claude-opus-4-8', label: 'Opus 4.8' },
  { id: 'claude-sonnet-4-6', label: 'Sonnet 4.6' },
  { id: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
];

async function setSessionModel(modelId) {
  if (!state.sessionId) return;
  try {
    await api(`api/sessions/${state.sessionId}/model`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelId }),
    });
    const label = (MODELS.find(m => m.id === modelId) || {}).label || 'Default';
    appendSystem(`Model set to ${label}. Takes effect on the next message.`);
    const s = state.sessions.find(x => x.id === state.sessionId);
    if (s) s.model = modelId;
    renderModelTile();
  } catch (err) {
    appendSystem(`Set model failed: ${err.message}`);
  }
}

async function summarizeFresh() {
  if (!state.sessionId) return;
  if (!confirm('Summarize this chat and continue in a new one? Old chat stays in the list.')) return;
  appendSystem('Summarizing…');
  try {
    const s = await api(`api/sessions/${state.sessionId}/summarize_fresh`, { method: 'POST' });
    await loadAll();
    selectSession(s.id);
  } catch (err) {
    appendSystem(`Summarize failed: ${err.message}`);
  }
}

chatActionsBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  const items = [];
  if (state.sessionId) {
    items.push({ label: 'Summarize & start fresh', action: summarizeFresh });
    items.push({ label: 'Clear context', danger: true, action: clearContext });
  }
  if (!items.length) return;
  openMenu(chatActionsBtn, items);
});
costEls.close.addEventListener('click', () => { costEls.panel.hidden = true; });
costEls.panel.addEventListener('click', (e) => { if (e.target === costEls.panel) costEls.panel.hidden = true; });

settingsEls.toggleBtn.addEventListener('click', openSettings);
settingsEls.closeBtn.addEventListener('click', closeSettings);
settingsEls.panel.addEventListener('click', (e) => { if (e.target === settingsEls.panel) closeSettings(); });

const securityModal = document.getElementById('security-modal');
document.getElementById('security-policy-open').addEventListener('click', () => { securityModal.hidden = false; });
document.getElementById('security-close').addEventListener('click', () => { securityModal.hidden = true; });
securityModal.addEventListener('click', (e) => { if (e.target === securityModal) securityModal.hidden = true; });
settingsEls.saveBtn.addEventListener('click', saveSettings);
settingsEls.enabled.addEventListener('change', onToggleChange);

settingsEls.theme.addEventListener('change', () => {
  applyTheme(settingsEls.theme.value);
});

document.getElementById('safe-bash-all').addEventListener('click', () => {
  safeBashEnabled = Object.keys(safeBashCatalog);
  renderSafeBashList();
});
document.getElementById('safe-bash-none').addEventListener('click', () => {
  safeBashEnabled = [];
  renderSafeBashList();
});

document.getElementById('delete-all-data').addEventListener('click', async () => {
  const ok = await confirmDangerous({
    title: 'Delete everything?',
    body: 'Wipes every chat, every project, and the CLI\u2019s per-session transcripts. '
      + 'Settings (Bash/WebFetch toggles, HA token, allowlist) are kept. '
      + 'This cannot be undone.',
    acceptLabel: 'Delete all',
  });
  if (!ok) return;
  try {
    const r = await api('api/data/all', { method: 'DELETE' });
    settingsEls.status.hidden = false;
    settingsEls.status.textContent =
      `Deleted ${r.sessions} chat${r.sessions === 1 ? '' : 's'} and `
      + `${r.projects} project${r.projects === 1 ? '' : 's'}. Reloading…`;
    setTimeout(() => location.reload(), 700);
  } catch (e) {
    settingsEls.status.hidden = false;
    settingsEls.status.textContent = `Delete failed: ${e.message}`;
  }
});


settingsEls.askBash.addEventListener('change', async () => {
  if (settingsEls.askBash.checked) return;
  const ok = await confirmDangerous({
    title: 'Disable Bash approval?',
    body: 'Claude will run any shell command silently — no prompt, no review. '
      + 'Destructive patterns (rm -rf, mkfs, force pushes) are still hard-blocked, '
      + 'but everything else just runs. Only turn this off if you trust everything you ask.',
  });
  if (!ok) settingsEls.askBash.checked = true;
});

settingsEls.askWebfetch.addEventListener('change', async () => {
  if (settingsEls.askWebfetch.checked) return;
  const ok = await confirmDangerous({
    title: 'Disable WebFetch approval?',
    body: 'Claude will fetch any URL silently. Web pages can include hidden '
      + 'instructions that try to redirect Claude\u2019s actions (prompt injection). '
      + 'You can keep this on and just use "Allow this domain" for sites you trust.',
  });
  if (!ok) settingsEls.askWebfetch.checked = true;
});

async function submit() {
  const text = els.input.value.trim();
  if (!text && state.attachments.length === 0) return;
  els.input.value = '';
  els.input.style.height = 'auto';
  try {
    await sendMessage(text);
    clearAttachments();
  } catch (err) {
    appendSystem(`Send failed: ${err.message}`);
  }
}

function isCoarsePointer() {
  return window.matchMedia('(pointer: coarse)').matches;
}

// --- Auth ----------------------------------------------------------------

const authEls = {
  screen: document.getElementById('auth-screen'),
  app: document.getElementById('app'),
  step1: document.getElementById('auth-step-1'),
  step2: document.getElementById('auth-step-2'),
  startBtn: document.getElementById('auth-start'),
  link: document.getElementById('auth-link'),
  code: document.getElementById('auth-code'),
  submitBtn: document.getElementById('auth-submit'),
  token: document.getElementById('auth-token'),
  tokenBtn: document.getElementById('auth-token-submit'),
  error: document.getElementById('auth-error'),
};

function showAuthError(msg) {
  authEls.error.hidden = false;
  authEls.error.textContent = msg;
}

async function startAuth() {
  authEls.error.hidden = true;
  authEls.startBtn.disabled = true;
  authEls.startBtn.textContent = 'Loading…';
  try {
    const r = await api('api/auth/start', { method: 'POST' });
    authEls.link.href = r.url;
    authEls.link.textContent = r.url;
    authEls.step1.hidden = true;
    authEls.step2.hidden = false;
    authEls.code.focus();
  } catch (e) {
    showAuthError(e.message);
    authEls.startBtn.disabled = false;
    authEls.startBtn.textContent = 'Start sign in';
  }
}

async function submitAuth() {
  const code = authEls.code.value.trim();
  if (!code) return;
  authEls.error.hidden = true;
  authEls.submitBtn.disabled = true;
  authEls.submitBtn.textContent = 'Verifying…';
  try {
    await api('api/auth/complete', { method: 'POST', body: JSON.stringify({ code }) });
    bootApp();
  } catch (e) {
    showAuthError(e.message);
    authEls.submitBtn.disabled = false;
    authEls.submitBtn.textContent = 'Finish';
  }
}

async function submitToken() {
  const token = authEls.token.value.trim();
  if (!token) return;
  authEls.error.hidden = true;
  authEls.tokenBtn.disabled = true;
  authEls.tokenBtn.textContent = 'Saving…';
  try {
    await api('api/auth/token', { method: 'POST', body: JSON.stringify({ token }) });
    bootApp();
  } catch (e) {
    showAuthError(e.message);
    authEls.tokenBtn.disabled = false;
    authEls.tokenBtn.textContent = 'Save token';
  }
}

authEls.startBtn.addEventListener('click', startAuth);
authEls.submitBtn.addEventListener('click', submitAuth);
authEls.code.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') submitAuth();
});
authEls.tokenBtn.addEventListener('click', submitToken);
authEls.token.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') submitToken();
});

// --- Boot ----------------------------------------------------------------

async function bootApp() {
  authEls.screen.hidden = true;
  authEls.app.hidden = false;
  await loadAll();
  updateHaBanner();
  refreshUsage(true);
  // Deep-link from notification tap: ?session=<id> drops the user straight
  // into the right chat. Falls back to most-recent if the id is unknown.
  let target = null;
  try {
    const qs = new URLSearchParams(window.location.search);
    const wanted = qs.get('session');
    if (wanted && state.sessions.some(s => s.id === wanted)) target = wanted;
  } catch (_) { /* ignore malformed query */ }
  if (!target && state.sessions.length > 0) target = state.sessions[0].id;
  if (target) selectSession(target);
}

// iOS keyboard handling inside HA's ingress iframe. (v0.1.53)
//
// HA's panel container scrolls the iframe ELEMENT up when iOS shows the
// keyboard. From inside the iframe we can't see that movement — vv.offsetTop
// reports zero. But ingress is same-origin and HA's iframe has
// `allow-same-origin`, so we can reach `window.parent.visualViewport` and
// write to `window.frameElement.style` directly. That pins the iframe to the
// visual viewport from inside, no companion HACS integration needed.
//
// Fallback (outside iframe / cross-origin block): just pin #app to vv.height.
(() => {
  const vv = window.visualViewport;
  if (!vv) return;
  const app = document.getElementById('app');

  let fe = null, pvv = null;
  try {
    if (window.frameElement && window.parent && window.parent.visualViewport) {
      fe = window.frameElement;
      pvv = window.parent.visualViewport;
    }
  } catch (_) {
    fe = null; pvv = null;
  }

  // Report the HA panel URL we're loaded inside so the backend can use it
  // as the base for notification deep-links. HA serves the addon panel at
  // a URL that depends on its (possibly hash-prefixed) slug; guessing from
  // config.yaml 401s / 404s. The frontend already knows the right path.
  try {
    if (window.parent && window.parent.location && window.parent.location.pathname) {
      const path = window.parent.location.pathname;
      fetch('api/panel_url', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path}),
      }).catch(() => {});
    }
  } catch (_) { /* cross-origin block — backend falls back to slug guess */ }

  // Keyboard-up detection: when iOS shows the keyboard, the visible viewport
  // shrinks but env(safe-area-inset-bottom) keeps reporting ~34px for the
  // home indicator. That inset is meaningless when the composer sits above
  // the keyboard, and turns into a ~34px dark band under the composer.
  // Toggle `html.keyboard-up` so CSS can zero --safe-bottom in that state.
  const setKeyboardClass = (visibleH, fullH) => {
    document.documentElement.classList.toggle(
      'keyboard-up',
      fullH - visibleH > 100
    );
  };

  // Inside an iframe on iOS WKWebView, `100%` / `100dvh` resolve against the
  // parent window's layout viewport, not the iframe element's actual size.
  // Even with the iframe sized to pvv.height, html/body end up taller than
  // the visible area, and iOS's scroll-input-into-view shifts the whole
  // iframe content upward — exposing body background under the composer.
  // Lock html + body to pvv.height explicitly to stop that scroll.
  const lockHeights = (h) => {
    document.documentElement.style.height = h + 'px';
    document.body.style.height = h + 'px';
    app.style.height = h + 'px';
  };

  // The iframe-reposition trick is iOS-only. On desktop Safari/Chrome the HA
  // sidebar overlays the first ~250px of the window, and pinning the iframe
  // to `left: 0` of the parent viewport hides the chat behind the sidebar.
  // iPadOS reports as MacIntel with maxTouchPoints>1, so check both UA and
  // touch capability.
  const isIOS =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  if (fe && pvv && isIOS) {
    fe.style.position = 'fixed';
    fe.style.left = '0';
    fe.style.right = '0';
    const apply = () => {
      fe.style.top = pvv.offsetTop + 'px';
      fe.style.height = pvv.height + 'px';
      lockHeights(pvv.height);
      let fullH = pvv.height;
      try { fullH = window.parent.innerHeight || fullH; } catch (_) {}
      setKeyboardClass(pvv.height, fullH);
    };
    pvv.addEventListener('resize', apply);
    pvv.addEventListener('scroll', apply);
    window.addEventListener('focus', apply);
    apply();
  } else {
    const apply = () => {
      lockHeights(vv.height);
      setKeyboardClass(vv.height, window.innerHeight);
    };
    vv.addEventListener('resize', apply);
    vv.addEventListener('scroll', apply);
    window.addEventListener('focus', apply);
    apply();
  }
})();

// #topbar is position:fixed so it stays glued to the iframe viewport top
// regardless of rubber-band scroll. The thread needs padding-top equal to
// the topbar's real height (varies with safe-area-inset on rotation, dynamic
// island, etc.) — measure it and publish as --topbar-h.
(() => {
  const tb = document.getElementById('topbar');
  if (!tb) return;
  const measure = () => {
    document.documentElement.style.setProperty('--topbar-h', tb.offsetHeight + 'px');
  };
  measure();
  window.addEventListener('resize', measure);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', measure);
  new ResizeObserver(measure).observe(tb);
})();

(async () => {
  try {
    const r = await api('api/auth/status');
    if (r.authed) {
      bootApp();
    } else {
      authEls.screen.hidden = false;
    }
  } catch {
    authEls.screen.hidden = false;
    showAuthError('Backend unreachable');
  }
})();
