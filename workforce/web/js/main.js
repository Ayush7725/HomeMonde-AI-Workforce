// Boot: sign in, route between pages, keep the state fresh from the live event stream, and handle every action.
import { api, ApiError, setCsrf, setUnauthorizedHandler, subscribe } from './api.js';
import { createWorld } from './world.js';
import { hydrateIcons } from './icons.js';
import * as pages from './pages.js';

const $ = (s) => document.querySelector(s);
const ctx = {
  boot: null, state: {}, events: [], live: false, technical: false, route: { page: 'home', id: null },
  workView: { view: 'queue', q: '', employee: '' }, homeTab: 'all', activityTab: 'all', briefKind: 'daily',
  formMode: null, openRecords: null, openMenu: null, openKeep: new Set(), feedback: {}, aiTest: {}, floorSel: null, world: null, clock: null, reports: null,
};
try { ctx.technical = localStorage.getItem('wf.technical') === '1'; } catch { /* private window: plain view */ }
try { if (localStorage.getItem('wf.menu') === 'collapsed') $('#app').classList.add('collapsed'); } catch { /* not remembered */ }

// ---------------------------------------------------------------- loading with a small cache
const details = new Map();   // work id -> detail payload (null = not found)
const cache = new Map();
const loadingKeys = new Set();
const loadErrors = new Map(); // key -> message, for data that could not be loaded (never "Loading…" for ever)
let requested = new Set();     // keys the page being drawn asked for
ctx.detail = (id) => {
  requested.add(`w:${id}`);
  if (details.has(id)) return details.get(id) === null ? undefined : details.get(id);
  if (!loadingKeys.has(`w:${id}`) && !loadErrors.has(`w:${id}`)) {
    loadingKeys.add(`w:${id}`);
    api(`/api/work/${encodeURIComponent(id)}`).then((d) => { details.set(id, d); if (d.root.id !== id) details.set(d.root.id, d); })
      .catch((e) => { if (e instanceof ApiError && e.status === 404) details.set(id, null); else if (e.status !== 401) loadErrors.set(`w:${id}`, e.message); })
      .finally(() => { loadingKeys.delete(`w:${id}`); schedule(); });
  }
  return null;
};
function cached(key, url, map = (x) => x) {
  requested.add(key);
  if (cache.has(key)) return cache.get(key);
  if (!loadingKeys.has(key) && !loadErrors.has(key)) {
    loadingKeys.add(key);
    api(url).then((d) => cache.set(key, map(d))).catch((e) => { if (e.status !== 401) loadErrors.set(key, e.message); })
      .finally(() => { loadingKeys.delete(key); schedule(); });
  }
  return null;
}
const boardUrl = () => { const v = ctx.workView; return `/api/work?view=${v.view}&q=${encodeURIComponent(v.q)}${v.employee ? `&employee=${encodeURIComponent(v.employee)}` : ''}`; };
ctx.home = () => cached('home', '/api/home');
ctx.board = () => cached(`board:${boardUrl()}`, boardUrl());
ctx.report = (id) => cached(`report:${id}`, `/api/reports/${encodeURIComponent(id)}`);
ctx.health = () => cached('health', '/api/workforce/health');
ctx.notifications = () => cached('notifications', '/api/notifications', (d) => d.notifications);
ctx.usage = () => cached('usage', '/api/usage');
ctx.controls = () => cached('controls', '/api/controls');
ctx.team = () => cached('team', '/api/team');
ctx.aiSettings = () => cached('models', '/api/ai/settings');
ctx.records = (work, ref) => cached(`records:${work}:${ref}`, `/api/work/${encodeURIComponent(work)}/evidence/${encodeURIComponent(ref)}/records`);
ctx.assignees = (ap) => cached(`assignees:${ap}`, `/api/approvals/${encodeURIComponent(ap)}/assignees`, (d) => d.people);
const LIVE = ['home', 'board:', 'health'];   // views that follow every change in the state
const OPS = { EMPLOYEE_PAUSED: ['health'], EMPLOYEE_RESUMED: ['health'], NOTIFICATION: ['notifications'], CONTROL_CHANGED: ['controls'],
  ACCESS_GRANTED: ['team'], ACCESS_REVOKED: ['team'], AI_SETTINGS_CHANGED: ['models', 'usage'], AI_KEY_SET: ['models'], AI_KEY_REMOVED: ['models'] };
async function loadReports() {
  try { ctx.reports = (await api('/api/reports')).reports; schedule(); } catch { ctx.reports = ctx.reports || []; }
}

// ---------------------------------------------------------------- routing
const ROUTES = { '': 'home', tasks: 'tasks', task: 'task', alerts: 'alerts', decisions: 'decisions', reports: 'reports', floor: 'floor',
  team: 'team', people: 'people', data: 'data', ai: 'ai', usage: 'usage', controls: 'controls', help: 'help', activity: 'activity' };
const OWNER_ONLY = new Set(['people', 'ai', 'usage', 'controls']);
function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  let page = ROUTES[parts[0]] || 'home';
  const id = parts[1] || null;
  if (page === 'reports' && id) page = 'report';
  if (page === 'team' && id) page = 'employee';
  if (OWNER_ONLY.has(page) && ctx.boot && ctx.boot.user.role !== 'owner') page = 'home';
  return { page, id };
}
function onRoute() {
  const prev = ctx.route;
  ctx.route = parseRoute();
  ctx.formMode = null; ctx.openMenu = null;
  if (prev.page !== ctx.route.page || prev.id !== ctx.route.id) { ctx.openKeep.clear(); loadErrors.clear(); clearFields = true; }
  if (prev.page !== ctx.route.page || prev.id !== ctx.route.id) {
    if (ctx.route.page === 'task' && ctx.route.id) details.delete(ctx.route.id);  // always open a task fresh
    for (const k of [...cache.keys()]) if (['notifications', 'health', 'team', 'controls', 'models', 'usage'].includes(k)) cache.delete(k);
    $('#content').scrollTop = 0;
  }
  if (ctx.route.page === 'alerts') api('/api/notifications/seen', { method: 'POST' }).then(() => refreshState(0)).catch(() => {});
  render(true);  // a new page is always drawn, even if the cursor was left in a box
}
window.addEventListener('hashchange', onRoute);

// ---------------------------------------------------------------- rendering
let queued = false, queuedForce = false;
/** force: the result of something the person just did; draw it even if their cursor is in a box (what they typed is kept). */
function schedule(force = false) {
  queuedForce = queuedForce || force;
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => { queued = false; const f = queuedForce; queuedForce = false; render(f); });
}
const PAGES = {
  home: () => pages.homePage(ctx), tasks: () => pages.tasksPage(ctx), task: () => pages.taskPage(ctx, ctx.route.id),
  alerts: () => pages.alertsPage(ctx), decisions: () => pages.decisionsPage(ctx), reports: () => pages.reportsPage(ctx),
  report: () => pages.reportPage(ctx, ctx.route.id), team: () => pages.teamPage(ctx), employee: () => pages.employeePage(ctx, ctx.route.id),
  people: () => pages.peoplePage(ctx), data: () => pages.dataPage(ctx), ai: () => pages.aiPage(ctx), usage: () => pages.usagePage(ctx),
  controls: () => pages.controlsPage(ctx), help: () => pages.helpPage(ctx), activity: () => pages.activityPage(ctx),
};
const TITLES = { home: 'Home', tasks: 'Tasks', task: 'Task', alerts: 'Alerts', decisions: 'Decisions', reports: 'Reports', report: 'Brief', floor: 'Live floor',
  team: 'AI team', employee: 'AI employee', people: 'People & access', data: 'Data sources', ai: 'AI models & keys', usage: 'AI cost & quality',
  controls: 'Pause switches', help: 'How this works', activity: 'Activity' };
let clearFields = false;  // after a successful action or a new page, boxes start empty instead of being refilled
/** What the person has typed or chosen in the page, so a redraw never wipes it. Keys and passwords are never kept. */
function snapshotFields(root) {
  const out = {};
  for (const el of root.querySelectorAll('input[id], textarea[id], select[id]')) {
    if (el.type === 'password') continue;
    out[el.id] = el.type === 'checkbox' ? el.checked : el.value;
  }
  for (const el of root.querySelectorAll('input[type=checkbox][name]')) out[`cb:${el.name}:${el.value}`] = el.checked;
  return out;
}
function restoreFields(root, snap) {
  for (const [id, v] of Object.entries(snap)) {
    if (id.startsWith('cb:')) {
      const [, name, val] = id.split(':');
      const el = root.querySelector(`input[type=checkbox][name="${CSS.escape(name)}"][value="${CSS.escape(val)}"]`);
      if (el) el.checked = v;
      continue;
    }
    const el = root.querySelector(`#${CSS.escape(id)}`);
    if (!el || el.readOnly) continue;
    if (el.type === 'checkbox') el.checked = v; else el.value = v;
  }
}
function render(force = false) {
  if (!ctx.boot) return;
  $('#sidebar').innerHTML = pages.sidebarHTML(ctx);
  pages.renderTop(ctx);
  document.title = `${TITLES[ctx.route.page] || 'Home'} · ${ctx.boot.tenant.name}`;
  const floor = ctx.route.page === 'floor';
  $('#floorPage').hidden = !floor;
  $('#content').hidden = floor;
  if (floor) { $('#floorSide').innerHTML = pages.floorSide(ctx); return; }
  // live updates never redraw under someone typing or choosing; the result of their own action does (keeping what they typed)
  const content = $('#content'), a = document.activeElement;
  const typing = a && a.closest && a.closest('#content') && ['INPUT', 'SELECT', 'TEXTAREA'].includes(a.tagName);
  if (typing && !force) { pendingRender = true; return; }
  const focusId = typing ? a.id : null;
  const sel = typing && typeof a.selectionStart === 'number' ? [a.selectionStart, a.selectionEnd] : null;
  const snap = clearFields ? {} : snapshotFields(content);
  clearFields = false;
  requested = new Set();
  let html = (PAGES[ctx.route.page] || PAGES.home)();
  const failed = [...requested].map((k) => loadErrors.get(k)).filter(Boolean);
  if (failed.length && html.includes(pages.LOADING)) {
    html = html.replace(pages.LOADING, `<section class="card"><div class="card-body"><p style="margin:0 0 10px"><b>This could not be loaded.</b></p><p class="note">${pages.esc(failed[0])}</p><div class="form-row"><button class="btn" data-retry>Try again</button></div></div></section>`);
  }
  content.innerHTML = `<div class="page">${html}</div>`;
  restoreFields(content, snap);
  for (const d of content.querySelectorAll('details[data-keep]')) if (ctx.openKeep.has(d.dataset.keep)) d.open = true;
  hydrateIcons(content);
  if (focusId) {
    const el = document.getElementById(focusId);
    if (el) { el.focus(); if (sel && typeof el.setSelectionRange === 'function') { try { el.setSelectionRange(sel[0], sel[1]); } catch { /* number inputs */ } } }
  }
}
let pendingRender = false;
// a section someone opened stays open when live data redraws the page
$('#content').addEventListener('toggle', (e) => {
  const k = e.target.dataset && e.target.dataset.keep;
  if (k) { if (e.target.open) ctx.openKeep.add(k); else ctx.openKeep.delete(k); }
}, true);
document.addEventListener('focusout', () => { if (pendingRender) { pendingRender = false; setTimeout(schedule, 0); } });

let stateTimer = null;
function refreshState(delay = 400) {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(async () => {
    try {
      ctx.state = await api('/api/state');
      for (const k of [...cache.keys()]) if (LIVE.some((p) => k.startsWith(p))) cache.delete(k);
      schedule();
      ctx.world.setStatus(ctx.state.employees || {});
      ctx.world.setBeacons((ctx.state.approvals || []).length > 0);
    } catch (e) { if (!(e instanceof ApiError)) console.error('[wf] state refresh', e); }  // 401 is handled by the API client
  }, delay);
}

function onEvent(ev) {
  ctx.world.consume(ev);
  ctx.events.unshift(ev);
  if (ctx.events.length > 300) ctx.events.length = 300;
  if (ev.work_id || ev.root) {
    for (const [id, d] of details) {
      if (!d) continue;
      const ids = [d.root.id, ...d.children.map((c) => c.id)];
      if (ids.includes(ev.work_id) || ids.includes(ev.root) || id === ev.work_id) details.delete(id);
    }
  }
  for (const k of OPS[ev.type] || []) cache.delete(k);
  if (ev.type === 'REPORT_GENERATED') loadReports();
  if (ev.category === 'work') cache.delete('usage');
  refreshState();
}

// ---------------------------------------------------------------- boot and sign-in
// ---------------------------------------------------------------- one browser, one account
// Tabs share the sign-in. When another tab signs out or signs in as someone else, this tab must not keep showing
// the old person: it reloads and shows whoever is signed in now (or the sign-in screen).
const SESSION_KEY = 'wf.session';
function announceSession(value) { try { localStorage.setItem(SESSION_KEY, `${value}:${Date.now()}`); } catch { /* private window */ } }
window.addEventListener('storage', (e) => {
  if (e.key !== SESSION_KEY) return;
  const who = String(e.newValue || '').split(':')[0];
  if (ctx.boot ? who !== String(ctx.boot.user.id) : who && who !== 'out') location.reload();
});
let unsubscribe = null;
setUnauthorizedHandler(() => {
  if (!ctx.boot) return;
  if (unsubscribe) unsubscribe();
  ctx.boot = null;
  showLogin('Your session has ended. Please sign in again.');
});

async function start() {
  const boot = await api('/api/bootstrap');
  ctx.boot = boot;
  announceSession(boot.user.id);
  setCsrf(boot.csrf);
  ctx.clock = pages.makeClock(boot.tenant.timezone);
  $('#tenantName').textContent = boot.tenant.name;
  $('#userName').textContent = boot.user.name;
  $('#userRole').textContent = `${boot.user.role === 'owner' ? 'Owner' : 'Manager'} · ${boot.tenant.timezone.split('/').pop().replace(/_/g, ' ')} time`;
  $('#userInitial').textContent = pages.initials(boot.user.name).slice(0, 1);
  $('#userEmail').textContent = boot.user.email || boot.user.name;
  $('#login').hidden = true;
  $('#app').hidden = false;
  document.body.classList.toggle('technical', ctx.technical);
  hydrateIcons();
  ctx.world = createWorld($('#world'), boot, (kind, id) => { ctx.floorSel = { kind: kind === 'area' ? 'area' : kind, id }; ctx.world.highlight(kind, id); if (kind === 'area') ctx.world.focusArea(id); schedule(); });
  ctx.world.onChange(() => { if (ctx.route.page === 'floor') schedule(); });
  ctx.state = await api('/api/state');
  ctx.world.setStatus(ctx.state.employees || {});
  ctx.world.setBeacons((ctx.state.approvals || []).length > 0);
  for (const ap of ctx.state.approvals || []) ctx.world.placeAtArea(ap.employee, 'approvals', 'hourglass');
  ctx.route = parseRoute();
  loadReports();
  const hist = await api(`/api/events?after=${Math.max(0, ctx.state.seq - 150)}&limit=150`);
  ctx.events = hist.events.slice().reverse();
  ctx.freshSeq = ctx.state.seq;
  unsubscribe = subscribe(ctx.state.seq, onEvent, (live) => { ctx.live = live; schedule(); });
  render();
}

function showLogin(message = '') {
  $('#app').hidden = true;
  $('#login').hidden = false;
  $('#loginErr').textContent = message;
  $('#email').focus();
}
$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('#loginErr').textContent = '';
  const btn = e.currentTarget.querySelector('button[type=submit]');
  btn.disabled = true;
  try {
    const r = await api('/api/auth/login', { method: 'POST', body: { email: $('#email').value, password: $('#password').value } });
    setCsrf(r.csrf);
    $('#password').value = '';
    await start();
  } catch (err) { $('#loginErr').textContent = err.message; } finally { btn.disabled = false; }
});
$('#logoutBtn').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* signed out anyway */ } finally { announceSession('out'); location.hash = ''; location.reload(); }
});
$('#userBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  const open = $('#userDrop').hidden;
  $('#userDrop').hidden = !open;
  $('#userBtn').setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('#userMenu')) { $('#userDrop').hidden = true; $('#userBtn').setAttribute('aria-expanded', 'false'); }
  if (ctx.openMenu && !e.target.closest('[data-menu]') && !e.target.closest('.menu')) { ctx.openMenu = null; schedule(); }
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { $('#userDrop').hidden = true; if (ctx.openMenu) { ctx.openMenu = null; schedule(); } } });

// ---------------------------------------------------------------- ask a question
$('#askForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#askInput'), text = input.value.trim();
  if (text.length < 3) { toast('Type a question first, e.g. “Which products might run out this week?”', true); return; }
  const btn = e.currentTarget.querySelector('button');
  btn.disabled = true;
  try {
    const r = await api('/api/questions', { method: 'POST', body: { text } });
    input.value = '';
    input.blur();
    location.hash = `#/task/${encodeURIComponent(r.work_id)}`;
    toast('Question sent. The answer appears here when it is ready.');
  } catch (err) { toast(err.message, true); } finally { btn.disabled = false; }
});

// ---------------------------------------------------------------- toasts
let toastTimer = null;
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = `toast${bad ? ' bad' : ''}`; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 8000 : 4200);
}

// ---------------------------------------------------------------- actions
async function post(url, body, { scope, done, after } = {}) {
  try {
    await api(url, { method: 'POST', body });
    ctx.formMode = null;
    clearFields = true;
    if (after) after();
    if (done) toast(done);
  } catch (err) { ctx.formMode = { scope, error: err.message }; toast(err.message, true); }
  schedule(true);
}
async function decide(apId, decision, extra = {}) {
  try {
    await api(`/api/approvals/${encodeURIComponent(apId)}/decision`, { method: 'POST', body: { decision, ...extra } });
    ctx.formMode = null; clearFields = true; details.clear(); cache.delete('home'); refreshState(0);
    toast({ APPROVED: 'Approved. The action runs now and is checked afterwards.', MODIFIED: 'Approved with your changes.', REJECTED: 'Turned down. The AI employee has been told.', MORE_INVESTIGATION: 'Sent back to check more.' }[decision]);
  } catch (err) {
    if (err.status === 409) {  // decided meanwhile: in another tab, or by a colleague
      ctx.formMode = null; clearFields = true; details.clear(); cache.delete('home'); refreshState(0);
      toast(`This decision was already made (${err.message.replace(/^Approval is already /i, '')}), perhaps in another tab or by a colleague. Nothing more was done.`, true);
    } else {
      ctx.formMode = { ...(ctx.formMode || { ap: apId, mode: 'error' }), ap: apId, error: err.message };
      toast(err.message, true);
    }
  }
  schedule(true);
}

$('#content').addEventListener('submit', (e) => {
  if (e.target.id !== 'boardFilter') return;
  e.preventDefault();
  ctx.workView = { ...ctx.workView, q: $('#boardSearch').value.trim(), employee: $('#boardEmp').value };
  document.activeElement.blur();
  schedule();
});
$('#content').addEventListener('change', (e) => {
  const tier = e.target.dataset && e.target.dataset.provTier;
  if (!tier) return;  // a different AI company: offer its model names and clear the old one
  $(`#models_${tier}`).innerHTML = (pages.SUGGESTED[e.target.value] || []).map((m) => `<option value="${m}">`).join('');
  const model = $(`#model_${tier}`); model.value = ''; model.focus();
});

document.addEventListener('click', async (e) => {
  const row = e.target.closest('[data-href]');
  if (row && !e.target.closest('a,button')) { location.hash = row.dataset.href; return; }
  const t = e.target.closest('button, a[data-floor-emp], a[data-scroll]');
  if (!t || !ctx.boot) return;
  const d = t.dataset;
  if (d.scroll !== undefined) { e.preventDefault(); const el = document.getElementById(d.scroll); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
  if (d.floorEmp) { ctx.floorSel = { kind: 'worker', id: d.floorEmp }; setTimeout(() => { ctx.world.highlight('worker', d.floorEmp); ctx.world.focusEmployee(d.floorEmp); }, 60); return; }
  if (d.collapse !== undefined) {
    const c = $('#app').classList.toggle('collapsed');
    try { localStorage.setItem('wf.menu', c ? 'collapsed' : 'open'); } catch { /* not remembered */ }
    return;
  }
  if (d.technical !== undefined) {
    ctx.technical = !ctx.technical;
    try { localStorage.setItem('wf.technical', ctx.technical ? '1' : '0'); } catch { /* not remembered */ }
    document.body.classList.toggle('technical', ctx.technical);
    schedule();
    return;
  }
  if (d.retry !== undefined) { loadErrors.clear(); schedule(true); return; }
  if (d.menu) { ctx.openMenu = ctx.openMenu === d.menu ? null : d.menu; schedule(); return; }
  if (d.brief) { ctx.briefKind = d.brief; schedule(); return; }
  if (d.homeTab) { ctx.homeTab = d.homeTab; schedule(); return; }
  if (d.activityTab) { ctx.activityTab = d.activityTab; schedule(); return; }
  if (d.board) { ctx.workView = { ...ctx.workView, view: d.board }; schedule(); return; }
  if (d.records) { const key = `${d.work}:${d.records}`; ctx.openRecords = ctx.openRecords === key ? null : key; schedule(); return; }
  if (d.follow) { ctx.world.setFollow(ctx.world.follow === d.follow ? null : d.follow); schedule(); return; }
  if (d.verdict) { ctx.feedback = { ...ctx.feedback, [d.work]: d.verdict }; schedule(true); return; }
  if (d.saveFeedback) {
    const verdict = ctx.feedback[d.saveFeedback];
    if (!verdict) return;
    return post(`/api/work/${encodeURIComponent(d.saveFeedback)}/review`, { verdict, note: ($('#reviewNote') || {}).value || '' },
      { scope: 'feedback', done: 'Thank you. This builds the accuracy score.', after: () => { details.clear(); cache.delete('health'); } });
  }
  if (d.stop) { ctx.openMenu = null; return post(`/api/work/${encodeURIComponent(d.stop)}/stop`, undefined, { scope: 'task', done: 'Stopping: it stops at its next step.', after: () => { details.clear(); refreshState(0); } }); }
  if (d.explain) {
    t.disabled = true;
    return post(`/api/work/${encodeURIComponent(d.explain)}/explain`, undefined, { scope: 'task', done: 'Asked the AI to explain. This takes a minute.', after: () => { details.clear(); refreshState(0); } });
  }
  if (d.resolve) {
    return post(`/api/work/${encodeURIComponent(d.work)}/resolve`, { action: d.resolve, note: ($('#resolveNote') || {}).value || '' },
      { scope: 'task', done: d.resolve === 'close' ? 'Closed.' : 'Sent back to look into it again.', after: () => { details.clear(); refreshState(0); } });
  }
  if (d.control) {
    const body = { kind: d.control, target: d.target, paused: d.paused === '1', reason: ($('#controlReason') || {}).value || '' };
    return post('/api/controls', body, { scope: 'controls', done: d.paused === '1' ? 'Paused.' : 'Restarted.', after: () => cache.delete('controls') });
  }
  if (d.grant) {
    const areas = [...document.querySelectorAll('input[name="grantArea"]:checked')].map((i) => i.value);
    if (!areas.length) { ctx.formMode = { scope: 'people', error: 'Choose at least one area.' }; schedule(); return; }
    return post(`/api/team/${encodeURIComponent($('#grantUser').value)}/grant`, { areas, hours: Number($('#grantHours').value), reason: $('#grantReason').value.trim() },
      { scope: 'people', done: 'Temporary access given.', after: () => cache.delete('team') });
  }
  if (d.revoke) return post(`/api/grants/${encodeURIComponent(d.revoke)}/revoke`, undefined, { scope: 'people', done: 'Access ended.', after: () => cache.delete('team') });
  if (d.routeSave) {
    const tier = d.routeSave, pin = $(`#pin_${tier}`).value, pout = $(`#pout_${tier}`).value;
    const body = { tier, provider: $(`#prov_${tier}`).value, model: $(`#model_${tier}`).value.trim(), effort: $(`#effort_${tier}`).value,
      price_input: pin === '' ? null : Number(pin), price_output: pout === '' ? null : Number(pout) };
    ctx.aiTest = { ...ctx.aiTest, [tier]: null };
    return post('/api/ai/route', body, { scope: 'ai', done: 'Saved. It applies to the next task.', after: () => { cache.delete('models'); refreshState(0); } });
  }
  if (d.routeReset) return post('/api/ai/route/reset', { tier: d.routeReset }, { scope: 'ai', done: 'Back to the company default.', after: () => { cache.delete('models'); refreshState(0); } });
  if (d.keySave) {
    const input = $(`#key_${d.keySave}`), key = input.value.trim();
    input.value = '';  // never keep a key on the page
    if (!key) { ctx.formMode = { scope: 'ai', error: 'Paste the key first.' }; schedule(); return; }
    return post(`/api/ai/keys/${encodeURIComponent(d.keySave)}`, { key }, { scope: 'ai', done: 'Key saved (encrypted).', after: () => { cache.delete('models'); refreshState(0); } });
  }
  if (d.keyRemove) return post(`/api/ai/keys/${encodeURIComponent(d.keyRemove)}/remove`, undefined, { scope: 'ai', done: 'Key removed.', after: () => { cache.delete('models'); refreshState(0); } });
  if (d.aiTest) {
    const tier = d.aiTest;
    t.disabled = true; t.textContent = 'Testing…';
    try { ctx.aiTest = { ...ctx.aiTest, [tier]: await api('/api/ai/test', { method: 'POST', body: { tier } }) }; }
    catch (err) { ctx.aiTest = { ...ctx.aiTest, [tier]: { ok: false, message: err.message } }; }
    cache.delete('usage'); schedule();
    return;
  }
  if (d.genReport) {
    t.disabled = true;
    try {
      const r = await api('/api/reports', { method: 'POST', body: { kind: d.genReport } });
      cache.delete(`report:${r.id}`); await loadReports();
      location.hash = `#/reports/${encodeURIComponent(r.id)}`;
    } catch (err) { ctx.formMode = { scope: 'report', error: err.message }; schedule(); }
    return;
  }
  if (d.pause || d.resume) {
    const id = d.pause || d.resume;
    return post(`/api/employees/${encodeURIComponent(id)}/${d.pause ? 'pause' : 'resume'}`, d.pause ? { reason: ($('#pauseReason') || {}).value || '' } : undefined,
      { scope: 'employee', done: d.pause ? 'Paused. It does nothing until you restart it.' : 'Restarted.', after: () => { cache.delete('health'); refreshState(0); } });
  }
  const act = d.act, ap = d.ap;
  if (!act) return;
  if (act === 'cancel') { ctx.formMode = null; clearFields = true; schedule(true); return; }
  if (act === 'approve') { t.disabled = true; return decide(ap, 'APPROVED'); }
  if (act === 'retry') return post(`/api/approvals/${encodeURIComponent(ap)}/retry`, undefined, { scope: 'task', done: 'Trying the action again.', after: () => { details.clear(); refreshState(0); } });
  if (act === 'assign') { cache.delete(`assignees:${ap}`); ctx.formMode = { ap, mode: 'assign' }; schedule(); return; }
  if (act === 'assign-confirm') {
    try {
      await api(`/api/approvals/${encodeURIComponent(ap)}/assign`, { method: 'POST', body: { user_id: Number($('#assignTo').value), note: $('#assignNote').value } });
      ctx.formMode = null; clearFields = true; details.clear(); refreshState(0); toast('Handed over. They get an alert.');
    } catch (err) { ctx.formMode = { ap, mode: 'assign', error: err.message }; toast(err.message, true); }
    schedule(true);
    return;
  }
  if (act === 'modify' || act === 'reject' || act === 'more') {
    ctx.formMode = { ap, mode: act };
    render(true);
    const f = $('#content').querySelector('input:not([readonly]), textarea'); if (f) f.focus();
    return;
  }
  if (act === 'modify-confirm') {
    const params = {};
    for (const inp of $('#content').querySelectorAll('input[data-param]')) {
      const label = inp.dataset.label || inp.dataset.param, raw = inp.value.trim(), v = Number(raw);
      const min = inp.min === '' ? null : Number(inp.min), max = inp.max === '' ? null : Number(inp.max);
      const problem = raw === '' ? `Enter the ${label}.` : !Number.isFinite(v) ? `The ${label} must be a number.`
        : (min !== null && v < min) || (max !== null && v > max) ? `The ${label} must be ${min !== null && max !== null ? `between ${min} and ${max}` : min !== null ? `at least ${min}` : `at most ${max}`}.` : null;
      if (problem) { ctx.formMode = { ap, mode: 'modify', error: problem }; inp.focus(); schedule(true); return; }
      params[inp.dataset.param] = v;
    }
    return decide(ap, 'MODIFIED', { params });
  }
  const reason = ($('#decisionReason') || {}).value || '';
  if (act === 'reject-confirm') return decide(ap, 'REJECTED', { reason });
  if (act === 'more-confirm') return decide(ap, 'MORE_INVESTIGATION', { reason });
});

// ---------------------------------------------------------------- live floor controls
$('#visToggle').addEventListener('change', (e) => {
  const on = e.target.checked;
  ctx.world.setEnabled(on);
  $('#world').style.visibility = on ? 'visible' : 'hidden';
  $('#offPanel').hidden = on;
  $('#pauseBtn').disabled = !on;
  schedule();
});
$('#pauseBtn').addEventListener('click', (e) => {
  const p = !ctx.world.paused;
  ctx.world.setPaused(p);
  e.currentTarget.setAttribute('aria-pressed', p);
  e.currentTarget.textContent = p ? 'Resume' : 'Pause';
});
$('#zin').addEventListener('click', () => ctx.world.zoomBy(1 / 1.25));
$('#zout').addEventListener('click', () => ctx.world.zoomBy(1.25));
$('#zreset').addEventListener('click', () => { ctx.world.setFollow(null); ctx.world.fit(); });

start().catch((e) => {
  // 401: not signed in, or the session ended while loading (then the API client already said so; keep its message)
  if (e instanceof ApiError && e.status === 401) { if ($('#login').hidden) showLogin(); }
  else document.body.innerHTML = '<p style="font-family:sans-serif;padding:40px">The workforce could not load. Check the server and refresh.</p>';
});
