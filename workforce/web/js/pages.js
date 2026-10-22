// Every screen of the app, as HTML strings built from the API's data. Written for founders and managers: plain
// business words first; codes, IDs and raw data only when "Technical details" is on. Pages never compute business
// numbers: every figure shown comes from the server's metric engine or checks.
import {
  CERTAINTY, DECISION, EMPLOYEE, FRESH, HEALTH, KIND, OUTCOME, PRIORITY, ROLE, STATUS, VERDICT,
  aiOffReason, esc, humanize, money, pill, readable, sentence, toolName, value, word,
} from './words.js';
import { icon, taskIcon } from './icons.js';

export { esc };

// ---------------------------------------------------------------- small helpers
export function makeClock(tz) {
  const t = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false });
  const ts = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const d = new Intl.DateTimeFormat('en-GB', { timeZone: tz, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  const day = new Intl.DateTimeFormat('en-GB', { timeZone: tz, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  const hour = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: 'numeric', hour12: false });
  return {
    hm: (iso) => t.format(iso ? new Date(iso) : new Date()),
    time: (iso) => ts.format(iso ? new Date(iso) : new Date()),
    stamp: (iso) => (iso ? d.format(new Date(iso)) : '—'),
    today: () => day.format(new Date()),
    hour: () => Number(hour.format(new Date())),
  };
}
const pct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);
const usd = (v) => (v === null || v === undefined ? 'price not set' : `$${Number(v).toFixed(v < 1 ? 4 : 2)}`);
const tech = (ctx, html) => (ctx.technical ? html : '');
const techRef = (ctx, text) => tech(ctx, `<span class="ref">${esc(text)}</span>`);
const empName = (ctx, id) => (ctx.boot.employees[id] || {}).name || humanize(id);
const isOwner = (ctx) => ctx.boot.user.role === 'owner';
export const LOADING = '<div class="empty">Loading…</div>';
const loading = LOADING;
/** The next-step line without the technical model name the server appends ("… (gemini:gemini-x)"), unless asked for. */
const plainNext = (ctx, text) => (ctx.technical ? String(text || '') : String(text || '').replace(/\s*\([a-z0-9_-]+:[^)\s]+\)/gi, ''));
export const initials = (name) => String(name || '?').replace(/ AI$/, '').split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase() || '?';
/** Two-letter badges for AI employees, unique within the team ("Finance AI" -> FI, "Marketing AI" -> MK next to "Manufacturing AI" -> MA). */
let badgeMap = null;
function badge(ctx, id) {
  if (!badgeMap) {
    badgeMap = {};
    const used = new Set();
    for (const [eid, e] of Object.entries(ctx.boot.employees)) {
      const w = String(e.name || eid).replace(/ AI$/, '').replace(/[^A-Za-z]/g, '').toUpperCase() || 'AI';
      let code = w.slice(0, 2);
      for (let i = 2; used.has(code) && i < w.length; i++) code = w[0] + w[i];
      used.add(code);
      badgeMap[eid] = code;
    }
  }
  return badgeMap[id] || initials(empName(ctx, id));
}
const owner = (ctx, id) => `<span class="owner"><span class="initials">${esc(badge(ctx, id))}</span>${esc(empName(ctx, id))}</span>`;
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
function certaintyPill(q) {
  if (!q) return '';
  const [t, cls] = CERTAINTY[q] || [humanize(q), 'p-grey'];
  return `<span class="pill ${cls}">${q === 'CONFIRMED' ? icon('check', 12) : ''}${esc(t)}</span>`;
}
function head(kick, title, sub, right = '') {
  return `<div class="page-head"><div>${kick ? `<div class="kick">${esc(kick)}</div>` : ''}<h1>${esc(title)}</h1>${sub ? `<p class="sub">${sub}</p>` : ''}</div>${right ? `<div class="actions">${right}</div>` : ''}</div>`;
}
const card = (title, body, { right = '', foot = '', cls = '', count = null, countCls = '' } = {}) =>
  `<section class="card ${cls}"><div class="card-head"><h2>${esc(title)}</h2>${count !== null ? `<span class="count-badge ${countCls}">${count}</span>` : ''}${right ? `<div class="right">${right}</div>` : ''}</div>${body}${foot ? `<div class="card-foot">${foot}</div>` : ''}</section>`;
const errorNote = (ctx, scope) => (ctx.formMode && ctx.formMode.error && (!scope || ctx.formMode.scope === scope) ? `<p class="err-msg">${esc(ctx.formMode.error)}</p>` : '');

/** A key figure from the server (never computed here), the way a person says it. */
function keyFigure(ctx, kf) {
  if (!kf) return '<span class="muted">—</span>';
  const cur = ctx.boot.tenant.currency;
  const main = value(kf.value, kf.unit, cur);
  const sub = kf.quantity != null && kf.unit_price != null ? `${Number(kf.quantity).toLocaleString('en-IN')} × ${money(cur, kf.unit_price)}` : (kf.label || '');
  return `<div class="figure"><b>${esc(main)}</b><span>${esc(sub)}</span></div>`;
}
/** The title without the code and party shown on the line below it ("Stockout risk · HM-204" -> "Stockout risk"). */
function shortTitle(w) {
  let t = String(w.title || '').replace(/ and \d+ more$/, '');
  for (const part of [w.party, w.code].filter(Boolean)) t = t.split(` · ${part}`).join('').split(` ${part}`).join('');
  return t.trim() || w.title;
}

// ---------------------------------------------------------------- shell: sidebar and top bar
const NAV = [
  ['Overview', [['home', 'Home', 'home', '#/'], ['tasks', 'Tasks', 'tasks', '#/tasks'], ['alerts', 'Alerts', 'bell', '#/alerts'],
    ['decisions', 'Decisions', 'decision', '#/decisions'], ['reports', 'Reports', 'report', '#/reports'], ['floor', 'Live floor', 'floor', '#/floor']]],
  ['Team', [['team', 'AI team', 'team', '#/team'], ['people', 'People & access', 'people', '#/people', 'owner']]],
  ['Settings', [['data', 'Data sources', 'data', '#/data'], ['ai', 'AI models & keys', 'key', '#/ai', 'owner'],
    ['usage', 'AI cost & quality', 'gauge', '#/usage', 'owner'], ['controls', 'Pause switches', 'pause', '#/controls', 'owner']]],
];
export function sidebarHTML(ctx) {
  const st = ctx.state, srcs = st.sources || ctx.boot.sources || [];
  const fresh = srcs.filter((s) => s.freshness === 'CURRENT').length;
  const badge = {
    alerts: st.unread_notifications ? `<span class="badge warn">${st.unread_notifications}</span>` : '',
    decisions: `<span class="badge${(st.approvals || []).length ? ' warn' : ''}">${(st.approvals || []).length}</span>`,
    team: `<span class="badge">${Object.keys(ctx.boot.employees).length}</span>`,
    data: `<span class="badge ${fresh === srcs.length ? 'ok' : 'warn'}">${fresh}/${srcs.length}</span>`,
  };
  const page = ctx.route.page;
  const active = (k) => k === page || (k === 'tasks' && page === 'task') || (k === 'reports' && page === 'report') || (k === 'team' && page === 'employee');
  let h = '';
  for (const [sec, items] of NAV) {
    const shown = items.filter((i) => !i[4] || isOwner(ctx));
    if (!shown.length) continue;
    h += `<div class="nav-sec">${sec}</div>`;
    h += shown.map(([k, label, ic, href]) => `<a class="nav-item${active(k) ? ' on' : ''}" href="${href}" title="${esc(label)}"${active(k) ? ' aria-current="page"' : ''}>${icon(ic)}<span class="lbl">${label}</span>${badge[k] || ''}</a>`).join('');
  }
  h += `<div class="nav-foot"><a class="nav-item${page === 'help' ? ' on' : ''}" href="#/help" title="How this works">${icon('help')}<span class="lbl">How this works</span></a>
    <button class="nav-item" data-technical aria-pressed="${ctx.technical}" title="Show codes, IDs and raw data"><span class="toggle${ctx.technical ? ' on' : ''}"></span><span class="lbl">Technical details</span></button>
    <button class="nav-item" data-collapse title="Collapse or expand the menu">${icon('sidebar')}<span class="lbl">Collapse menu</span></button></div>`;
  return h;
}

export function renderTop(ctx) {
  const st = ctx.state, srcs = st.sources || [];
  const stale = srcs.filter((s) => s.freshness !== 'CURRENT');
  const latest = srcs.map((s) => s.last_success).filter(Boolean).sort().slice(-1)[0];
  const chip = document.getElementById('dataChip');
  chip.className = `status-chip${stale.length ? ' warn' : ''}`;
  chip.title = stale.length ? stale.map((s) => `${s.label}: ${word(FRESH, s.freshness)}`).join('\n') : 'Every data feed is up to date';
  chip.innerHTML = stale.length ? `<span class="dot"></span>${plural(stale.length, 'data feed')} out of date`
    : `<span class="dot"></span>Data up to date${latest ? ` <span class="t">${esc(ctx.clock.hm(latest))}</span>` : ''}`;
  const ai = st.ai || {};
  const on = !ai.unavailable;
  const aiChip = document.getElementById('aiChip');
  aiChip.title = on ? 'AI employees explain causes and suggest next steps.' : aiOffReason(ai.unavailable);
  aiChip.innerHTML = `<span class="toggle${on ? ' on' : ''}"></span>AI explanations <b>${on ? 'On' : 'Off'}</b>`;
  if (!isOwner(ctx)) aiChip.removeAttribute('href');
  const n = st.unread_notifications || 0, count = document.getElementById('bellCount');
  count.hidden = !n;
  count.textContent = n > 99 ? '99+' : String(n);
}

// ---------------------------------------------------------------- home
export function homePage(ctx) {
  const d = ctx.home();
  const u = ctx.boot.user;
  const hr = ctx.clock.hour();
  const greet = hr < 12 ? 'Good morning' : hr < 17 ? 'Good afternoon' : 'Good evening';
  const kind = ctx.briefKind || 'daily';
  const brief = (ctx.reports || []).find((r) => r.kind === kind);
  const briefLabel = { daily: 'Daily brief', weekly: 'Weekly brief', monthly: 'Monthly brief' }[kind];
  const sub = brief ? `${briefLabel} · Covers ${esc(ctx.clock.stamp(brief.period_start))} – ${esc(ctx.clock.stamp(brief.period_end))}` : `${u.role === 'owner' ? 'The whole business' : 'Your areas'} at a glance`;
  const seg = `<div class="seg" role="group" aria-label="Brief">${['daily', 'weekly', 'monthly'].map((k) => `<button data-brief="${k}" aria-pressed="${kind === k}">${humanize(k)}</button>`).join('')}</div>`;
  const open = brief ? `<a class="btn" href="#/reports/${encodeURIComponent(brief.id)}">Open full brief ${icon('arrow-right', 14)}</a>`
    : isOwner(ctx) ? `<button class="btn" data-gen-report="${kind}">Make the ${kind} brief</button>` : '';
  let h = head(ctx.clock.today(), greet, sub, seg + open);
  for (const g of u.temporary_access || []) {
    h += `<p class="note" style="margin:-8px 0 14px">You can also see ${esc(g.areas.map((a) => (ctx.boot.layout.areas[a] || {}).name || a).join(', '))} until ${esc(ctx.clock.stamp(g.expires_at))} (${esc(g.reason)}).</p>`;
  }
  if (!d) return h + loading;
  const cur = ctx.boot.tenant.currency;
  const byArea = {};
  for (const w of d.attention) byArea[w.area || 'Other'] = (byArea[w.area || 'Other'] || 0) + 1;
  const breakdown = Object.entries(byArea).map(([a, n]) => `${a} ${n}`).join(' · ');
  const kpi = (label, val, small, ic, tone = '', href = '') => {
    const tag = href.startsWith('#/') ? `a href="${href}"` : href ? `a href="#/" data-scroll="${href}"` : 'div';
    return `<${tag} class="kpi"><div class="top"><span>${esc(label)}</span><span class="ico ${tone}">${icon(ic, 14)}</span></div><b>${esc(val)}</b><small class="${tone === 'bad' ? 'bad' : tone === 'good' ? 'good' : ''}">${small}</small></${tag.split(' ')[0]}>`;
  };
  h += '<div class="kpis">';
  h += kpi('Needs your attention', String(d.attention_total ?? d.attention.length), esc(breakdown || 'Nothing urgent right now'), 'warning', d.attention.length ? 'warn' : '', 'attention');
  h += kpi('Waiting for your decision', String(d.decisions_total), d.decisions_total ? `${plural(d.decisions_total, 'decision')} to make` : 'Nothing to approve right now', 'decision', d.decisions_total ? 'warn' : '', '#/decisions');
  for (const k of d.kpis) {
    const tone = k.good === null ? '' : k.good ? 'good' : 'bad';
    const change = k.change === null ? 'No earlier figure yet' : `${k.trend === 'down' ? 'Down' : k.trend === 'up' ? 'Up' : 'Flat'} ${Math.abs(k.change * 100).toFixed(1)}% vs before`;
    h += kpi(k.label, value(k.value, k.unit, cur), `${esc(change)}${k.freshness !== 'CURRENT' ? ` · ${esc(word(FRESH, k.freshness))}` : ''}`,
      k.trend === 'down' ? 'trend-down' : 'trend-up', tone);
  }
  h += '</div><div class="layout-2"><div class="stack">';
  if (d.decisions.length) {
    h += card('Waiting for your decision', `<ul class="rows">${d.decisions.map((a) => `<li><span class="grow"><b>${esc(a.title)}</b><small>${esc(empName(ctx, a.employee))}${a.amount != null ? ` · ${esc(money(cur, a.amount))}` : ''}</small></span><a class="btn sm" href="#/task/${encodeURIComponent(a.work_id)}">Decide</a></li>`).join('')}</ul>`,
      { count: d.decisions_total, foot: `<span>A person decides every action</span><a href="#/decisions">All decisions</a>` });
  }
  const areas = Object.keys(byArea);
  const tab = areas.includes(ctx.homeTab) ? ctx.homeTab : 'all';
  const rows = d.attention.filter((w) => tab === 'all' || (w.area || 'Other') === tab);
  const tabs = `<div class="seg" role="group" aria-label="Filter by area"><button data-home-tab="all" aria-pressed="${tab === 'all'}">All<span class="n">${d.attention.length}</span></button>${areas.map((a) => `<button data-home-tab="${esc(a)}" aria-pressed="${tab === a}">${esc(a)}<span class="n">${byArea[a]}</span></button>`).join('')}</div>`;
  const table = rows.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Issue</th><th>Owner</th><th>Key figure</th><th>Confidence</th><th></th></tr></thead><tbody>${rows.map((w) => attentionRow(ctx, w)).join('')}</tbody></table></div>`
    : '<div class="empty">Nothing urgent right now. Your AI team is watching.</div>';
  h += `<div id="attention">${card('Needs your attention', table, { count: d.attention_total ?? d.attention.length, right: d.attention.length ? tabs : '',
    foot: `<span>Showing ${rows.length} of ${d.attention_total ?? d.attention.length}</span><a href="#/tasks">View all tasks</a>` })}</div>`;
  h += activityCard(ctx, 6);
  h += '</div><div class="stack">';
  h += card('Good news', d.good_news.length ? `<div class="card-body stack">${d.good_news.map((w) => `<div class="good-item"><span class="ico">${icon('trend-up', 14)}</span><div style="flex:1;min-width:0"><b>${esc(shortTitle(w))}${w.code ? ` · ${esc(w.code)}` : ''}</b><p>${esc(w.headline && w.headline !== w.title ? w.headline : w.key_figure ? `${w.key_figure.label}: ${value(w.key_figure.value, w.key_figure.unit, ctx.boot.tenant.currency)}` : '')}</p><div style="display:flex;justify-content:space-between;align-items:center">${certaintyPill(w.quality)}<a href="#/task/${encodeURIComponent(w.id)}">Review</a></div></div></div>`).join('')}</div>`
    : '<div class="empty">Nothing new to celebrate yet. Opportunities show up here when your data shows them.</div>', { count: d.good_news.length, countCls: 'green' });
  const other = briefSection(ctx, 'other');
  if (other !== null) {
    h += card('Other findings', `<div class="card-body"><p class="note" style="margin-bottom:12px">${other ? `${plural(other, 'more finding')} in the latest brief. None of them needs action from you.` : 'No other findings in the latest brief.'}</p>${other && brief ? `<a class="btn sm" href="#/reports/${encodeURIComponent(brief.id)}">View findings</a>` : ''}</div>`, { count: other, countCls: 'grey' });
  }
  h += dataSourcesCard(ctx);
  h += teamCard(ctx);
  return h + '</div></div>';
}

function attentionRow(ctx, w) {
  const code = [w.code, w.party].filter(Boolean).join(' · ') + (w.more ? ` and ${w.more} more` : '');
  const desc = w.headline && w.headline !== w.title ? w.headline : '';
  return `<tr class="clickable" data-href="#/task/${encodeURIComponent(w.id)}"><td><div class="issue"><span class="ico">${icon(taskIcon(w.type))}</span><div><b>${esc(shortTitle(w))}</b>${code ? `<div class="code">${esc(code)}</div>` : ''}${desc ? `<p>${esc(desc)}</p>` : ''}${techRef(ctx, w.id)}</div></div></td>
    <td>${owner(ctx, w.employee)}</td><td>${keyFigure(ctx, w.key_figure)}</td><td>${certaintyPill(w.quality) || pill(STATUS, w.status)}</td>
    <td class="num"><a class="btn sm" href="#/task/${encodeURIComponent(w.id)}">Review</a></td></tr>`;
}

/** Item count of a section in the newest brief of the chosen kind, or null when there is no brief yet. */
function briefSection(ctx, key) {
  const kind = ctx.briefKind || 'daily';
  const r = (ctx.reports || []).find((x) => x.kind === kind);
  const full = r ? ctx.report(r.id) : null;
  if (!full) return null;
  const sec = full.sections.find((s) => s.key === key);
  return sec ? sec.items.length : 0;
}

function dataSourcesCard(ctx) {
  const srcs = ctx.state.sources || ctx.boot.sources || [];
  const fresh = srcs.filter((s) => s.freshness === 'CURRENT').length;
  const right = `<span class="pill ${fresh === srcs.length ? 'p-green' : 'p-warn'}">${fresh} of ${srcs.length} up to date</span>`;
  return card('Data sources', `<div class="card-body" style="padding-top:6px;padding-bottom:6px"><ul class="mini-list">${srcs.map((s) =>
    `<li><span class="dot ${s.freshness === 'CURRENT' ? '' : s.freshness === 'NEVER_SYNCED' ? 'grey' : 'warn'}"></span><div><b>${esc(s.label)}</b>${s.datasets.length > 1 || s.freshness !== 'CURRENT' ? `<small>${s.freshness !== 'CURRENT' ? `${esc(word(FRESH, s.freshness))} · ` : ''}${esc(s.datasets.join(', '))}</small>` : ''}</div></li>`).join('')}</ul></div>`,
  { right, foot: '<span>Read-only: the AI never changes your systems</span><a href="#/data">Details</a>' });
}

function empStatus(ctx, id) {
  const st = (ctx.state.employees || {})[id] || { status: 'IDLE' };
  if (st.paused) return ['paused', 'Paused'];
  if (st.status === 'WAITING_APPROVAL') return ['waiting', 'Waiting for you'];
  if (st.status === 'WORKING' || st.status === 'QUEUED') return ['working', word(EMPLOYEE, st.status)];
  return ['', 'Idle'];
}
function teamCard(ctx) {
  const ids = Object.keys(ctx.boot.employees);
  const states = ids.map((id) => empStatus(ctx, id)[0]);
  const idle = states.filter((s) => !s).length, working = states.filter((s) => s === 'working').length;
  return card('AI team', `<div class="card-body"><div class="legend-row"><span><i class="st-dot"></i>Idle</span><span><i class="st-dot working"></i>Working</span><span><i class="st-dot waiting"></i>Waiting for you</span></div>
    <div class="team-grid">${ids.map((id) => { const [cls, label] = empStatus(ctx, id); return `<a class="team-cell" href="#/team/${encodeURIComponent(id)}" title="${esc(empName(ctx, id))}: ${esc(label)}"><span class="initials">${esc(badge(ctx, id))}</span><span class="nm">${esc(empName(ctx, id))}</span><span class="st-dot ${cls}"></span></a>`; }).join('')}</div></div>`,
  { right: `<span class="muted" style="font-size:12px">${idle} idle · ${working} working</span>`, foot: '<span>See them at work</span><a href="#/floor">Open live floor</a>' });
}

// ---------------------------------------------------------------- activity
const QUIET = new Set(['STATE_CHANGED', 'TOOL_RESULT', 'CALCULATION', 'PRE_ACTION_CHECKS', 'LOGIN', 'LOGOUT']);
const FINDING_TYPES = new Set(['FINDING_PRODUCED', 'FINDING_RECEIVED', 'RULE_TRIGGERED', 'RULE_CLEARED', 'APPROVAL_REQUESTED', 'HUMAN_DECISION', 'ACTION_EXECUTED', 'ACTION_FAILED', 'WORK_ITEM_CREATED']);
function activityKind(ev) {
  if (ev.category === 'data') return ['Data update', 'sync'];
  if (FINDING_TYPES.has(ev.type)) return ['Finding', 'spark'];
  if (ev.category === 'action') return ['Decision', 'decision'];
  if (ev.category === 'security') return ['Sign-in & access', 'key'];
  return ['Work', 'activity'];
}
export function visibleEvents(ctx) {
  return ctx.events.filter((ev) => ctx.technical || !QUIET.has(ev.type) || (ev.type === 'STATE_CHANGED' && ['NEEDS_HUMAN_REVIEW', 'FAILED'].includes(ev.status)));
}
function activityRows(ctx, list) {
  return list.map((ev) => {
    const [kind, ic] = activityKind(ev);
    const text = sentence(ev, ctx.boot);
    return `<li${ev.work_id ? ` data-href="#/task/${encodeURIComponent(ev.work_id)}" data-work="${esc(ev.work_id)}"` : ''} class="${ctx.freshSeq && ev.seq > ctx.freshSeq ? 'new' : ''}" title="${esc(ctx.technical ? ev.msg : text)}"><time>${esc(ctx.clock.time(ev.ts))}</time><span class="ico">${icon(ic, 13)}</span><span>${esc(text)}${tech(ctx, `<span class="ref">#${ev.seq} · ${esc(ev.type)}</span>`)}</span><span class="kind">${esc(kind)}</span></li>`;
  }).join('');
}
function activityFilter(ctx, list) {
  const tab = ctx.activityTab || 'all';
  return list.filter((ev) => tab === 'all' || (tab === 'findings' ? activityKind(ev)[0] === 'Finding' : activityKind(ev)[0] === 'Data update'));
}
function activityTabs(ctx, list) {
  const tab = ctx.activityTab || 'all';
  const n = (k) => list.filter((ev) => activityKind(ev)[0] === k).length;
  return `<div class="seg" role="group" aria-label="Filter activity"><button data-activity-tab="all" aria-pressed="${tab === 'all'}">All<span class="n">${list.length}</span></button><button data-activity-tab="findings" aria-pressed="${tab === 'findings'}">Findings<span class="n">${n('Finding')}</span></button><button data-activity-tab="data" aria-pressed="${tab === 'data'}">Data updates<span class="n">${n('Data update')}</span></button></div>`;
}
function activityCard(ctx, max) {
  const all = visibleEvents(ctx).slice(0, 60);
  const list = activityFilter(ctx, all).slice(0, max);
  return card('Live activity', list.length ? `<ul class="activity">${activityRows(ctx, list)}</ul>` : '<div class="empty">Nothing has happened yet.</div>',
    { right: `<span class="pill ${ctx.live ? 'p-green' : 'p-warn'}">${ctx.live ? 'Live' : 'Reconnecting'}</span>${activityTabs(ctx, all)}`, foot: '<span>Newest first</span><a href="#/activity">View all activity</a>' });
}
export function activityPage(ctx) {
  const all = visibleEvents(ctx);
  const list = activityFilter(ctx, all);
  return head('Overview', 'Activity', 'Everything your AI team did, newest first. Click a line to open its task.', activityTabs(ctx, all)) +
    card('All activity', list.length ? `<ul class="activity">${activityRows(ctx, list)}</ul>` : '<div class="empty">Nothing yet.</div>', { count: list.length, countCls: 'grey' });
}

// ---------------------------------------------------------------- tasks
const ago = (h) => (h < 1 ? 'under an hour' : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} days`);
export function tasksPage(ctx) {
  const v = ctx.workView;
  const b = ctx.board();
  const seg = `<div class="seg" role="group" aria-label="Which tasks"><button data-board="queue" aria-pressed="${v.view === 'queue'}">To do & in progress</button><button data-board="investigations" aria-pressed="${v.view === 'investigations'}">Finished</button></div>`;
  let h = head('Overview', 'Tasks', 'Everything your AI team is waiting on, working on and has finished.', seg);
  const emps = Object.entries(ctx.boot.employees);
  h += `<form class="form-row" id="boardFilter" style="margin:0 0 14px"><input id="boardSearch" type="search" maxlength="100" value="${esc(v.q)}" placeholder="Search tasks, e.g. HM-204, courier or invoice"><select id="boardEmp" aria-label="AI employee" style="max-width:240px"><option value="">Every AI employee</option>${emps.map(([id, e]) => `<option value="${esc(id)}" ${v.employee === id ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select><button class="btn" type="submit">Search</button></form>`;
  if (!b) return h + loading;
  const c = b.counts;
  h += `<div class="kpis">${[['Waiting for you', c.waiting_for_you, 'decision', c.waiting_for_you ? 'warn' : ''], ['Being worked on', c.working, 'activity', ''], ['Waiting to start', c.waiting_to_start, 'clock', ''], ['Could not finish (7 days)', c.failed_7d, 'alert', c.failed_7d ? 'bad' : '']]
    .map(([l, n, ic, tone]) => `<div class="kpi"><div class="top"><span>${l}</span><span class="ico ${tone}">${icon(ic, 14)}</span></div><b>${n}</b><small>${l === 'Being worked on' && c.retrying ? `${c.retrying} retrying shortly` : '&nbsp;'}</small></div>`).join('')}</div>`;
  const rows = b.items.map((w) => `<tr class="clickable" data-href="#/task/${encodeURIComponent(w.id)}"><td><div class="issue"><span class="ico">${icon(taskIcon(w.type))}</span><div><b>${esc(shortTitle(w))}</b>${w.code ? `<div class="code">${esc([w.code, w.party].filter(Boolean).join(' · '))}${w.more ? ` and ${w.more} more` : ''}</div>` : ''}${w.question ? `<p>“${esc(w.question)}”</p>` : w.headline && w.headline !== w.title ? `<p>${esc(w.headline)}</p>` : ''}${techRef(ctx, w.id)}</div></div></td>
    <td>${owner(ctx, w.employee)}</td><td>${pill(STATUS, w.status)}${w.good_news ? ' <span class="pill p-green">Good news</span>' : ''}</td><td>${pill(PRIORITY, w.priority)}</td>
    <td class="num muted">${v.view === 'queue' ? `${esc(ago(w.age_hours))} old` : esc(ctx.clock.stamp(w.completed_at))}</td></tr>`).join('');
  const body = b.items.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Task</th><th>Owner</th><th>Status</th><th>Importance</th><th class="num">${v.view === 'queue' ? 'Age' : 'Finished'}</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="empty">${v.q ? 'No task matches your search.' : v.view === 'queue' ? 'Nothing waiting or in progress.' : 'No finished tasks yet.'}</div>`;
  return h + card(v.view === 'queue' ? 'To do & in progress' : 'Finished', body, { count: b.total, countCls: 'grey', foot: b.total > b.items.length ? `<span>Showing ${b.items.length} of ${b.total}. Search to narrow it down.</span>` : '' });
}

// ---------------------------------------------------------------- task page
function proofNumbers(detail) {
  const map = {};
  (detail.evidence || []).forEach((e, i) => { map[e.ref] = i + 1; });
  return map;
}
const proofRefs = (ctx, refs, nums) => ((refs || []).length ? `<span class="ref">${ctx.technical ? esc(refs.join(', ')) : `proof ${refs.map((r) => nums[r] || r).join(', ')}`}</span>` : '');

function findingCard(ctx, f, detail) {
  if (!f) return `<section class="card"><div class="card-body"><div class="eyebrow">What we found</div><p class="finding-head">Nothing written up yet.</p><p class="note">${esc(plainNext(ctx, detail.item.next) || 'The AI employee is still working on it.')}</p></div></section>`;
  const nums = proofNumbers(detail);
  const [label, cls, meaning] = CERTAINTY[f.evidence_quality] || [humanize(f.evidence_quality), 'p-grey', ''];
  const tone = { 'p-green': '', 'p-blue': 'blue', 'p-violet': 'blue', 'p-red': 'red', 'p-warn': 'warn' }[cls] ?? 'grey';
  let h = `<section class="card"><div class="card-body"><div class="eyebrow">What we found</div><p class="finding-head">${esc(f.headline)}</p>
    <div class="certainty ${tone}">${certaintyPill(f.evidence_quality)}<span>${esc(meaning)}</span></div>`;
  if ((f.quality_basis || []).length) h += `<details class="why" data-keep="why"><summary>Why this rating</summary><ul>${f.quality_basis.map((b) => `<li>${esc(b)}</li>`).join('')}</ul></details>`;
  if (f.owner) h += `<p class="note" style="margin-top:12px"><b>Who fixes it:</b> ${esc(f.owner)}. This closes on its own once the data comes through.</p>`;
  if (f.impact) h += `<p style="margin:12px 0 0"><b>Why it matters:</b> ${esc(f.impact)}</p>`;
  for (const k of ['FACT', 'OBSERVATION', 'INFERENCE', 'HYPOTHESIS']) {
    const list = (f.statements || []).filter((s) => s.kind === k);
    if (!list.length) continue;
    h += `<div class="stmt-group"><h4>${esc(KIND[k][0])} <span>· ${esc(KIND[k][1])}</span></h4><ul>${list.map((s) => `<li class="${s.withheld ? 'withheld' : ''}">${esc(s.text)}${proofRefs(ctx, s.refs, nums)}</li>`).join('')}</ul></div>`;
  }
  if (f.recommendation) h += `<div class="stmt-group"><h4>${esc(KIND.RECOMMENDATION[0])} <span>· ${esc(KIND.RECOMMENDATION[1])}</span></h4><p style="margin:4px 0"><b>${esc(f.recommendation.summary)}</b></p>${f.recommendation.rationale ? `<p class="note">${esc(f.recommendation.rationale)}</p>` : ''}</div>`;
  if (f.recommendation_held) h += `<p class="note" style="margin-top:12px">A suggestion was held back: <b>${esc(f.recommendation_held.summary)}</b>. The evidence is not strong enough to ask you to approve it.</p>`;
  if ((f.unknowns || []).length) h += `<div class="stmt-group"><h4>What we couldn't check</h4><ul>${f.unknowns.map((u) => `<li>${esc(ctx.technical ? u : u.replace(/ \([^)]*\)\.$/, '.'))}</li>`).join('')}</ul></div>`;
  if ((f.validation_notes || []).length) h += `<p class="note" style="margin-top:12px">Our safety check removed ${f.validation_notes.length === 1 ? 'a statement' : `${f.validation_notes.length} statements`} that your data didn't support, so only backed-up statements are shown.${tech(ctx, `<br>${esc(f.validation_notes.join(' · '))}`)}</p>`;
  h += tech(ctx, `<p class="ref" style="margin:12px 0 0;display:block">outcome ${esc(f.outcome)} · quality ${esc(f.evidence_quality)}${f.model ? ` · model ${esc(f.model)}` : ''}</p>`);
  return h + '</div></section>';
}

function proofsCard(ctx, detail) {
  const rows = detail.evidence || [];
  if (!rows.length) return '';
  const by = (e) => (e.kind === 'calc' ? 'Worked out from the figures above' : e.kind === 'metric' ? 'From your business figures' : e.withheld ? 'Withheld' : toolName(ctx.boot, e.name));
  const body = rows.map((e, i) => {
    if (e.withheld) return `<div class="proof"><div class="label"><b>Proof ${i + 1}</b> · Withheld</div><p class="note" style="margin-top:8px">Gathered by ${esc(empName(ctx, e.employee))}. It uses figures from an area you do not cover.</p></div>`;
    const lines = readable(e, ctx.boot);
    const metrics = e.result && e.result.metrics ? Object.values(e.result.metrics) : [];
    const r = e.result || {};
    const single = metrics.length === 1 && 'value' in metrics[0] ? metrics[0]
      : (!r.rows && !r.metrics && 'value' in r ? { label: r.label || toolName(ctx.boot, e.name), value: r.value, unit: r.unit } : null);
    const open = ctx.openRecords === `${detail.root.id}:${e.ref}`;
    const source = String(e.source || '').replace(/ · synced .*$/, '').replace(/^Metric registry/, 'Your business figures') || '—';
    return `<div class="proof"><div class="label"><b>Proof ${i + 1}</b> · ${esc(by(e))}${techRef(ctx, `${e.ref} · ${e.name}`)}</div>
      ${single ? `<div class="main"><span>${esc(single.label || 'Figure')}</span><b>${esc(value(single.value, single.unit, ctx.boot.tenant.currency))}</b></div>` : `<ul class="lines">${lines.map((l) => `<li>${esc(l)}</li>`).join('')}</ul>`}
      <div class="meta"><div><span>Source</span>${esc(source)}</div><div><span>Freshness</span>${pill(FRESH, e.freshness)}</div><div><span>Checked by</span>${esc(e.employee ? empName(ctx, e.employee) : '—')}</div></div>
      ${e.records ? `<div class="records-link"><button class="link" data-records="${esc(e.ref)}" data-work="${esc(detail.root.id)}" aria-expanded="${open}">${open ? 'Hide the records' : `See the ${e.records.toLocaleString('en-IN')} record${e.records === 1 ? '' : 's'} behind this`} ${icon('arrow-right', 14)}</button></div>` : ''}
      ${open ? recordsHTML(ctx, detail.root.id, e.ref) : ''}
      ${tech(ctx, `<p class="ref" style="display:block;white-space:normal;margin-top:10px">${esc(JSON.stringify(e.args))} → ${esc(JSON.stringify(e.result).slice(0, 400))}</p>`)}</div>`;
  }).join('');
  return `<section class="card"><div class="card-head"><h2>Evidence</h2><div class="right"><span class="muted" style="font-size:12.5px">${plural(rows.length, 'proof')}</span></div></div>${body}</section>`;
}

function recordsHTML(ctx, workId, ref) {
  const r = ctx.records(workId, ref);
  if (!r) return '<div class="records"><p class="note">Loading the records…</p></div>';
  if (!r.groups.length) return '<div class="records"><p class="note">These records are no longer stored.</p></div>';
  let h = `<div class="records"><p class="note">${r.capped ? `Showing ${r.shown.toLocaleString('en-IN')} of ${r.total.toLocaleString('en-IN')} records. ` : ''}Exactly as your systems sent them${r.groups.some((g) => g.masked.length) ? '; personal details are hidden' : ''}.</p>`;
  for (const g of r.groups) {
    const cols = g.fields.slice(0, 7), rows = g.rows.slice(0, 50);
    h += `<div class="table-wrap"><table><thead><tr>${cols.map((c) => `<th>${esc(humanize(c))}</th>`).join('')}</tr></thead><tbody>${rows.map((row) =>
      `<tr${row.changed_since ? ' class="changed" title="Updated after this proof was made"' : ''}>${cols.map((c) => `<td>${esc(row.data[c] ?? '')}</td>`).join('')}</tr>${tech(ctx, `<tr><td colspan="${cols.length}" class="raw">received: ${esc(JSON.stringify(row.raw))}</td></tr>`)}`).join('')}</tbody></table></div>${g.rows.length > rows.length ? `<p class="note">…and ${g.rows.length - rows.length} more</p>` : ''}`;
  }
  return h + '</div>';
}

function coverageCard(root) {
  const groups = (root.context && root.context.groups) || [];
  if (groups.length < 2) return '';
  const done = new Set(root.context.cleared || []);
  const left = groups.filter((g) => !done.has(String(g)));
  const share = Math.round((100 * done.size) / groups.length);
  return card('What this covers', `<div class="card-body"><p class="note">${done.size ? `${done.size} of ${groups.length} are back to normal. This task closes by itself when all are.` : `${groups.length} items, none back to normal yet.`}</p>
    <div class="meter" role="img" aria-label="${share}% back to normal"><span style="width:${share}%"></span></div>
    <ul class="chips">${left.map((g) => `<li>${esc(String(g))}</li>`).join('')}${[...done].map((g) => `<li class="done" title="Back to normal">${esc(g)}</li>`).join('')}</ul></div>`);
}

function historyCard(ctx, detail) {
  const rows = detail.history || [];
  if (!rows.length) return '';
  return `<details class="card history" data-keep="history"><summary><h2>Full history</h2><span class="muted">${plural(rows.length, 'step')} · kept for audit</span>${icon('chevron-down')}</summary><ul class="audit">${rows.map((e) =>
    `<li><time>${esc(ctx.clock.time(e.ts))}</time><span>${esc(sentence(e, ctx.boot))}${tech(ctx, `<span class="t">#${e.seq} · ${esc(e.type)} · ${esc(e.msg)}</span>`)}</span></li>`).join('')}</ul></details>`;
}

const IMPORTANCE = { CRITICAL: 'Urgent', HIGH: 'High', MEDIUM: 'Normal', LOW: 'Low' };
const OPEN_STATES = ['QUEUED', 'RUNNING', 'INVESTIGATING', 'VALIDATING', 'NEEDS_RETRY', 'WAITING_FOR_DATA'];
export function taskPage(ctx, id) {
  const detail = ctx.detail(id);
  const crumbs = (title) => `<div class="crumbs"><a href="#/">Home</a><span class="sep">/</span><a href="#/tasks">Tasks</a><span class="sep">/</span><span>${esc(title)}</span></div>`;
  if (detail === undefined) return crumbs('Not found') + card('This task could not be opened', '<div class="empty">It does not exist, or it belongs to an area you do not cover.</div>');
  if (!detail) return crumbs('…') + loading;
  const w = detail.item, root = detail.root, f = root.finding;
  const e = ctx.boot.employees[w.employee] || { name: w.employee };
  const area = (ctx.boot.layout.areas[e.area] || {}).name || '';
  const pending = detail.approvals.find((a) => a.status === 'PENDING');
  const code = (root.context && (root.context.group || (root.context.check || {}).invoice)) || (root.context && root.context.groups ? root.context.groups[0] : '');
  let h = crumbs(code || w.title);
  const explain = !w.parent && w.status === 'COMPLETED' && f && f.explain_available;
  const menuItems = [
    OPEN_STATES.includes(w.status) && !w.stop_requested ? `<button data-stop="${esc(w.id)}">Stop this task</button>` : '',
    `<a href="#/team/${encodeURIComponent(w.employee)}">About ${esc(e.name)}</a>`,
    `<a href="#/floor" data-floor-emp="${esc(w.employee)}">Show on the live floor</a>`,
    isOwner(ctx) && (detail.reviews || []).some((r) => r.verdict === 'WRONG') ? `<a href="/api/work/${encodeURIComponent(root.id)}/test-draft" download>Download test draft</a>` : '',
  ].filter(Boolean).join('');
  h += `<div class="task-title"><span class="ico">${icon(taskIcon(w.type), 22)}</span><div style="flex:1;min-width:0"><div class="kick">${w.parent ? 'Help request' : 'Task'}${area ? ` · ${esc(area)}` : ''}</div><h1>${esc(w.title)}</h1>
    <div class="badges">${pill(STATUS, w.status)}${w.outcome ? `<span class="pill p-blue">${esc(word(OUTCOME, w.outcome))}</span>` : ''}<span class="pill plain-pill">${esc(IMPORTANCE[w.priority] || 'Normal')}${w.priority === 'CRITICAL' ? '' : ' importance'}</span>${techRef(ctx, `${w.id} · ${w.type}`)}</div></div>
    <div class="actions">${explain ? `<button class="btn" data-explain="${esc(w.id)}">${icon('message', 14)} Explain why <span class="tag">Uses AI</span></button>` : ''}
      <div class="user"><button class="icon-btn" data-menu="taskMenu" aria-label="More actions" aria-expanded="${ctx.openMenu === 'taskMenu'}">${icon('more')}</button><div class="menu" ${ctx.openMenu === 'taskMenu' ? '' : 'hidden'}>${menuItems}</div></div></div></div>`;
  h += errorNote(ctx, 'task');
  h += '<div class="layout-task"><div class="stack">';
  if (root.context && root.context.question) h += `<section class="card"><div class="card-body"><div class="eyebrow">Your question</div><p class="finding-head" style="margin-bottom:0">“${esc(root.context.question)}”</p></div></section>`;
  if (pending && !w.parent) h += decisionCard(ctx, pending, detail);
  if (w.stop_requested && OPEN_STATES.includes(w.status)) h += '<section class="card"><div class="card-body note">Stopping: it stops at its next step and keeps nothing half-done.</div></section>';
  if (w.status === 'NEEDS_HUMAN_REVIEW' && !pending && !w.parent) h += reviewNeededCard(ctx, w);
  h += findingCard(ctx, f, detail);
  h += coverageCard(root);
  h += proofsCard(ctx, detail);
  if (f && f.ai === false && !w.parent) {
    h += `<section class="card callout"><span class="ico">${icon('alert')}</span><div><h3>Cause not analysed yet</h3><p>${explain ? 'AI explanations don\'t run automatically for tasks of this importance. This page shows your numbers only — no AI reasons or suggestions have been added.' : 'This page shows your numbers only. The AI explanation was not available, so no reasons or suggestions were added.'}</p></div>${explain ? `<button class="btn primary act" data-explain="${esc(w.id)}">Explain why</button>` : ''}</section>`;
  }
  if ((detail.actions || []).length) h += card('What was done', `<ul class="rows">${detail.actions.map((a) => `<li><span class="grow"><b>${esc((ctx.boot.actions[a.action] || {}).label || humanize(a.action))}${a.ref ? ` · reference ${esc(a.ref)}` : ''}</b><small class="${a.status === 'FAILED' ? 'bad' : ''}">${esc(a.status === 'FAILED' ? `It did not go through: ${a.detail}` : 'Done and confirmed.')}</small></span>${pill(a.status === 'FAILED' ? { F: ['Failed', 'p-red'] } : { F: ['Done', 'p-green'] }, 'F')}</li>`).join('')}</ul>`);
  if (!pending && !w.parent) for (const ap of detail.approvals.slice(-1)) h += decisionCard(ctx, ap, detail);
  h += historyCard(ctx, detail);
  h += '</div><div class="stack">';
  const kv = [['Status', esc(word(STATUS, w.status)), w.status === 'COMPLETED' ? 'ok' : ''], ['Outcome', esc(w.outcome ? word(OUTCOME, w.outcome) : '—'), ''],
    ['Importance', esc(IMPORTANCE[w.priority] || 'Normal'), ''], ['Handled by', `<a href="#/team/${encodeURIComponent(w.employee)}">${esc(e.name)}</a>`, ''],
    ['Next step', esc(plainNext(ctx, w.next) || '—'), '']];
  if (w.parent) kv.push(['Part of', `<a href="#/task/${encodeURIComponent(w.parent)}">${esc(root.title)}</a>`, '']);
  if (detail.children.length && !w.parent) {
    const by = new Map();
    for (const c of detail.children) by.set(c.employee, [...(by.get(c.employee) || []), c]);
    kv.push(['Asked for help', [...by].map(([emp, list]) => `<a href="#/task/${encodeURIComponent(list[0].id)}">${esc(empName(ctx, emp))}</a>${list.length > 1 ? ` <span class="muted">×${list.length}</span>` : ''}`).join(', '), '']);
  }
  if (w.error && ctx.technical) kv.push(['Last error', esc(w.error), '']);
  h += card('Details', `<div class="card-body" style="padding-top:4px;padding-bottom:4px"><dl class="kv">${kv.map(([k, v, cls]) => `<div><dt>${k}</dt><dd class="${cls}">${v}</dd></div>`).join('')}</dl></div>`);
  h += feedbackCard(ctx, detail);
  return h + '</div></div>';
}

function reviewNeededCard(ctx, w) {
  const f = w.finding || {};
  const why = w.type === 'DATA_ISSUE' ? 'A data feed needs fixing. Close this once you have dealt with it, or leave it: it closes on its own when the data comes through.'
    : (f.quality_basis || []).slice(-1)[0] || 'The AI could not reach a safe conclusion on its own.';
  return `<section class="card decision-card"><div class="card-head"><span class="eyebrow">A person needs to check this</span></div><div class="card-body"><p style="margin:0 0 10px">${esc(why)}</p>
    <textarea id="resolveNote" maxlength="1000" placeholder="${w.type === 'DATA_ISSUE' ? 'What was done? (optional)' : 'What should it look at again? (optional)'}"></textarea>
    <div class="form-row"><button class="btn go" data-resolve="close" data-work="${esc(w.id)}">Checked, close it</button>${['DATA_ISSUE', 'RULE_REVIEW'].includes(w.type) ? '' : `<button class="btn" data-resolve="reinvestigate" data-work="${esc(w.id)}">Look into it again</button>`}</div></div></section>`;
}

function feedbackCard(ctx, detail) {
  const f = detail.root.finding;
  if (!f || detail.item.parent || detail.root.type === 'DATA_ISSUE') return '';
  const human = (detail.reviews || []).find((r) => r.source === 'human');
  const auto = (detail.reviews || []).find((r) => r.source === 'auto');
  const chosen = (ctx.feedback || {})[detail.root.id] || (human && human.verdict);
  let h = '<div class="card-body"><p class="note" style="color:var(--ink-2)">Tell us once you know what really happened. It builds each AI employee\'s accuracy score.</p>';
  if (auto) h += `<p class="note" style="margin-top:8px">Checked automatically later: <b>${esc(word(VERDICT, auto.verdict))}</b></p>`;
  if (human) h += `<p class="note" style="margin-top:8px">Your team said: <b>${esc(word(VERDICT, human.verdict))}</b>${human.note ? ` · “${esc(human.note)}”` : ''}</p>`;
  h += `<div class="choice" role="group" aria-label="Was this right?">${['CORRECT', 'PARTLY', 'WRONG'].map((v) => `<button class="btn${chosen === v ? ' selected' : ''}" data-verdict="${v}" data-work="${esc(detail.root.id)}" aria-pressed="${chosen === v}">${esc(VERDICT[v])}</button>`).join('')}</div>
    <label class="field" for="reviewNote">What actually happened? <span>(optional)</span></label><textarea id="reviewNote" maxlength="1000" placeholder="Add a short note for the team"></textarea>
    <button class="btn primary block" style="margin-top:12px" data-save-feedback="${esc(detail.root.id)}" ${chosen ? '' : 'disabled'}>Save feedback</button>${errorNote(ctx, 'feedback')}</div>`;
  return card('Was this right?', h);
}

// ---------------------------------------------------------------- decisions
export function canApprove(ctx, ap) {
  const u = ctx.boot.user;
  return ap.approvers.some((r) => r.role === u.role && (r.max_amount == null || (ap.amount || 0) <= r.max_amount) &&
    (u.role !== 'manager' || ((u.areas || []).includes((ctx.boot.actions[ap.action] || {}).area) && (u.approval_limit == null || (ap.amount || 0) <= u.approval_limit))));
}
const whoCanApprove = (ctx, ap) => ap.approvers.map((r) => `${ROLE[r.role] || r.role}${r.max_amount != null ? ` (up to ${money(ctx.boot.tenant.currency, r.max_amount)})` : ''}`).join(' or ');

export function decisionCard(ctx, ap, detail) {
  const cur = ctx.boot.tenant.currency;
  const spec = (ctx.boot.actions[ap.action] || {}).params || {};
  const params = ap.final_params || ap.params;
  const f = detail && detail.root ? detail.root.finding : null;
  const decided = ap.status !== 'PENDING';
  const allowed = canApprove(ctx, ap);
  const numbers = Object.entries(params).filter(([k]) => spec[k] && spec[k].type !== 'string');
  const strings = Object.entries(params).filter(([k]) => spec[k] && spec[k].type === 'string');
  const fm = ctx.formMode && ctx.formMode.ap === ap.id ? ctx.formMode : null;
  let b = `<p class="finding-head" style="margin-top:0">${esc(ap.title)}</p>`;
  b += `<div class="nums">${numbers.slice(0, 3).map(([k, v]) => `<div><b>${esc(Number(v).toLocaleString('en-IN'))}</b><span>${esc(humanize(k))}</span></div>`).join('')}${ap.amount != null ? `<div><b>${esc(money(cur, ap.amount))}</b><span>Total amount</span></div>` : ''}</div>`;
  const short = strings.filter(([, v]) => String(v).length <= 70), long = strings.filter(([, v]) => String(v).length > 70);
  if (short.length) b += `<dl class="kv">${short.map(([k, v]) => `<div><dt>${esc(humanize(k))}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`;
  for (const [k, v] of long) b += `<div class="stmt-group"><h4>${esc(humanize(k))}</h4><p class="quote">${esc(v)}</p></div>`;
  if (ap.rationale) b += `<div class="stmt-group"><h4>Why the AI suggests this</h4><p style="margin:0">${esc(ap.rationale)}</p></div>`;
  if (f) {
    const facts = (f.statements || []).filter((s) => s.kind === 'FACT' && !s.withheld).slice(0, 4);
    if (facts.length) b += `<div class="stmt-group"><h4>The numbers behind it</h4><ul class="facts">${facts.map((s) => `<li>${esc(s.text)}</li>`).join('')}</ul></div>`;
    b += `<p style="margin:10px 0 0">How sure: ${certaintyPill(f.evidence_quality)}</p>`;
  }
  if (!decided && ap.assigned_to_name) b += `<p class="note" style="margin-top:10px">Handed to <b>${esc(ap.assigned_to_name)}</b> to decide.</p>`;
  if (decided) {
    b += `<p style="margin:12px 0 0"><b>${esc(word(DECISION, ap.status))}</b> on ${esc(ctx.clock.stamp(ap.decided_at))}${ap.reason ? ` · “${esc(ap.reason)}”` : ''}</p>`;
    if (detail && (detail.actions || []).some((a) => a.status === 'FAILED') && allowed) b += `<div class="form-row"><button class="btn primary" data-act="retry" data-ap="${esc(ap.id)}">Try the action again</button></div>`;
  } else if (!fm || fm.mode === 'error') {
    if (!allowed) b += '<p class="note" style="margin-top:12px">You can read this, but it is above what your role can approve. Someone with the right authority will decide.</p>';
    b += `<div class="form-row">${allowed ? `<button class="btn go" data-act="approve" data-ap="${esc(ap.id)}">Approve</button>${numbers.some(([k]) => !spec[k].locked) ? `<button class="btn" data-act="modify" data-ap="${esc(ap.id)}">Change the numbers</button>` : ''}` : ''}<button class="btn danger" data-act="reject" data-ap="${esc(ap.id)}">Say no</button><button class="btn" data-act="more" data-ap="${esc(ap.id)}">Ask the AI to check more</button><button class="btn" data-act="assign" data-ap="${esc(ap.id)}">Hand to someone ${allowed ? 'else' : 'who can'}</button></div>`;
  } else if (fm.mode === 'modify') {
    b += `<div class="stmt-group"><h4>Change the numbers</h4>${numbers.map(([k, v]) => { const s = spec[k], lo = s.minimum, hi = s.maximum;
        const range = lo != null && hi != null ? `between ${lo} and ${hi}` : lo != null ? `at least ${lo}` : hi != null ? `at most ${hi}` : '';
        return `<label class="field" for="mod_${esc(k)}">${esc(humanize(k))}${s.locked ? ' <span>(fixed by the evidence)</span>' : range ? ` <span>(${esc(range)})</span>` : ''}</label><input id="mod_${esc(k)}" data-param="${esc(k)}" data-label="${esc(humanize(k).toLowerCase())}" type="number" step="${s.type === 'integer' ? 1 : 'any'}"${lo != null ? ` min="${esc(lo)}"` : ''}${hi != null ? ` max="${esc(hi)}"` : ''} value="${esc(v)}"${s.locked ? ' readonly' : ''}>`; }).join('')}
      <div class="form-row"><button class="btn go" data-act="modify-confirm" data-ap="${esc(ap.id)}">Approve with these numbers</button><button class="btn" data-act="cancel">Cancel</button></div></div>`;
  } else if (fm.mode === 'reject' || fm.mode === 'more') {
    const reject = fm.mode === 'reject';
    b += `<label class="field" for="decisionReason">${reject ? 'Why not?' : 'What should it check?'} <span>(optional)</span></label><textarea id="decisionReason" maxlength="1000" placeholder="${reject ? 'A short reason for the team' : 'e.g. is the Amazon sale causing the extra demand?'}"></textarea>
      <div class="form-row"><button class="btn ${reject ? 'danger' : 'primary'}" data-act="${fm.mode}-confirm" data-ap="${esc(ap.id)}">${reject ? 'Confirm: say no' : 'Send back to check'}</button><button class="btn" data-act="cancel">Cancel</button></div>`;
  } else if (fm.mode === 'assign') {
    const people = ctx.assignees(ap.id);
    if (!people) b += '<p class="note" style="margin-top:12px">Finding who can decide…</p>';
    else if (!people.length) b += '<p class="note" style="margin-top:12px">Nobody else is allowed to make this decision.</p><div class="form-row"><button class="btn" data-act="cancel">Close</button></div>';
    else b += `<label class="field" for="assignTo">Hand to</label><select id="assignTo">${people.map((p) => `<option value="${p.id}">${esc(p.name)} (${esc(ROLE[p.role] || p.role)})</option>`).join('')}</select>
      <label class="field" for="assignNote">A note for them <span>(optional)</span></label><input id="assignNote" type="text" maxlength="500">
      <div class="form-row"><button class="btn primary" data-act="assign-confirm" data-ap="${esc(ap.id)}">Hand over</button><button class="btn" data-act="cancel">Cancel</button></div><p class="note" style="margin-top:8px">Only people allowed to approve this amount are listed. They get an alert.</p>`;
  }
  if (fm && fm.error) b += `<p class="err-msg">${esc(fm.error)}</p>`;
  b += `<p class="rule-note">Who can approve: ${esc(whoCanApprove(ctx, ap))}${ap.level === 3 ? '. Your team carries this out themselves after you approve.' : ''}. The AI can never approve its own suggestion; every decision is saved with who made it.${techRef(ctx, ap.id)}</p>`;
  return `<section class="card ${decided ? '' : 'decision-card'}"><div class="card-head"><span class="eyebrow">${decided ? 'Decision made' : 'Your decision is needed'}${ap.round > 1 ? ' · asked again with new information' : ''}</span></div><div class="card-body">${b}</div></section>`;
}

export function decisionsPage(ctx) {
  const aps = ctx.state.approvals || [];
  let h = head('Overview', 'Decisions', 'Suggestions from your AI team that need a person. Nothing happens until someone with the right authority decides.');
  if (!aps.length) return h + card('Waiting for your decision', '<div class="empty">No decisions waiting. When an AI employee suggests something, such as buying stock, it waits here with the numbers behind it.</div>', { count: 0, countCls: 'grey' });
  return h + `<div class="stack">${aps.map((ap) => `${decisionCard(ctx, ap, ctx.detail(ap.work_id))}<p class="note" style="margin:-8px 0 0 4px"><a href="#/task/${encodeURIComponent(ap.work_id)}">Open the full task and its evidence</a></p>`).join('')}</div>`;
}

// ---------------------------------------------------------------- alerts
export function alertsPage(ctx) {
  const list = ctx.notifications();
  let h = head('Overview', 'Alerts', 'You get an alert for urgent findings, decisions waiting for you, failed actions and broken data feeds.');
  if (!list) return h + loading;
  if (!list.length) return h + card('Alerts', '<div class="empty">Nothing needs your attention right now.</div>', { count: 0, countCls: 'grey' });
  return h + card('Alerts', `<ul class="rows">${list.map((n) => `<li${n.unread ? ' style="background:#FFFCF5"' : ''}><span>${pill(PRIORITY, n.severity)}</span><span class="grow"><b>${esc(n.title.replace(/^[A-Z]{2,5}-\d{4}-\d{5} · /, ''))}</b><small>${esc(n.text)}</small></span><span class="muted mono">${esc(ctx.clock.stamp(n.created_at))}</span>${n.work_id ? `<a class="btn sm" href="#/task/${encodeURIComponent(n.work_id)}">Open</a>` : ''}</li>`).join('')}</ul>`, { count: list.length, countCls: 'grey' });
}

// ---------------------------------------------------------------- reports
const BRIEF = { daily: 'Daily brief', weekly: 'Weekly brief', monthly: 'Monthly brief' };
export function reportsPage(ctx) {
  const list = ctx.reports;
  const right = isOwner(ctx) ? ['daily', 'weekly', 'monthly'].map((k) => `<button class="btn sm" data-gen-report="${k}">New ${k} brief</button>`).join('') : '';
  let h = head('Overview', 'Reports', 'Business briefs written from checked findings. Every number comes from your own data.', right) + errorNote(ctx, 'report');
  if (!list) return h + loading;
  if (!list.length) return h + card('Briefs', '<div class="empty">No brief yet. The first daily brief is written each morning.</div>');
  return h + card('Briefs', `<ul class="rows">${list.map((r) => `<li><span class="ico" style="color:var(--muted)">${icon('report')}</span><span class="grow"><b>${esc(BRIEF[r.kind] || 'Brief')} · ${esc(r.date)}</b><small>Covers ${esc(ctx.clock.stamp(r.period_start))} – ${esc(ctx.clock.stamp(r.period_end))}</small></span><a class="btn sm" href="#/reports/${encodeURIComponent(r.id)}">Open</a></li>`).join('')}</ul>`, { count: list.length, countCls: 'grey' });
}
export function reportPage(ctx, id) {
  const r = ctx.report(id);
  const crumbs = `<div class="crumbs"><a href="#/">Home</a><span class="sep">/</span><a href="#/reports">Reports</a><span class="sep">/</span><span>${r ? esc(r.date) : '…'}</span></div>`;
  if (!r) return crumbs + loading;
  let h = crumbs + head(r.date, BRIEF[r.kind] || 'Brief', `Covers ${esc(ctx.clock.stamp(r.period_start))} – ${esc(ctx.clock.stamp(r.period_end))}`);
  if (r.overview) h += `<section class="card"><div class="card-body"><div class="eyebrow">In short</div><p class="brief-overview" style="margin-top:6px">${esc(r.overview)}</p></div></section>`;
  const names = { attention: 'Needs your attention', waiting: 'Waiting for your decision', decided: 'Decisions made', other: 'Other findings', data: 'Data problems', workforce: 'AI team problems' };
  for (const sec of r.sections) {
    if (!sec.items.length && !['attention', 'waiting'].includes(sec.key)) continue;
    h += card(names[sec.key] || sec.title, `<div class="card-body" style="padding-top:4px;padding-bottom:4px">${sec.items.length ? sec.items.map((i) => `<div class="brief-item"><span>${esc(i.text)}${i.text !== i.verified ? tech(ctx, `<span class="ref">Original: ${esc(i.verified)}</span>`) : ''}</span><span class="actions">${i.quality ? certaintyPill(i.quality) : ''}${i.work_id ? `<a href="#/task/${encodeURIComponent(i.work_id)}">Open</a>` : ''}</span></div>`).join('') : '<p class="note" style="padding:10px 0">Nothing.</p>'}</div>`, { count: sec.items.length, countCls: 'grey' });
  }
  if (r.ai_usage && r.ai_usage.length && isOwner(ctx)) {
    const total = r.ai_usage.reduce((a, u) => a + (u.cost_usd || 0), 0);
    h += card('AI cost in this period', `<div class="card-body"><p style="margin:0">${usd(total)}${r.ai_usage.some((u) => u.cost_usd == null) ? ' (plus models without a price set)' : ''}</p></div>`);
  }
  if (r.wording) h += `<p class="note" style="margin-top:14px">${r.wording.verified_only ? 'Every line is taken directly from the checked findings.' : 'Lines were rewritten in plain words by AI, and every number was checked against the original findings.'}</p>`;
  return h;
}

// ---------------------------------------------------------------- AI team
export function teamPage(ctx) {
  const hd = ctx.health();
  let h = head('Team', 'AI team', 'One AI employee per part of the business. Each watches its own data, investigates problems and brings you the evidence.');
  if (!hd) return h + loading;
  const rows = Object.entries(hd.employees).map(([id, x]) => {
    const e = ctx.boot.employees[id] || {};
    const [cls, label] = empStatus(ctx, id);
    return `<tr class="clickable" data-href="#/team/${encodeURIComponent(id)}"><td><span class="owner"><span class="initials">${esc(badge(ctx, id))}</span><span><b>${esc(e.name)}</b><br><small class="muted">${esc(e.title || '')}</small></span></span></td>
      <td><span class="owner"><i class="st-dot ${cls}"></i>${esc(label)}</span></td><td>${pill(HEALTH, x.status)}</td><td class="num">${x.queue}</td><td class="num">${x.accuracy ? pct(x.accuracy.score) : '—'}</td><td class="num muted">${esc(ctx.clock.stamp(x.last_success))}</td></tr>`;
  }).join('');
  return h + card('Your AI employees', `<div class="table-wrap"><table class="table"><thead><tr><th>AI employee</th><th>Right now</th><th>Health</th><th class="num">Waiting</th><th class="num">Accuracy</th><th class="num">Last finished</th></tr></thead><tbody>${rows}</tbody></table></div>`,
    { count: Object.keys(hd.employees).length, countCls: 'grey', foot: '<span>Accuracy comes from findings your team marked right or wrong</span><a href="#/floor">Open live floor</a>' });
}

export function employeePage(ctx, id) {
  const e = ctx.boot.employees[id];
  const crumbs = `<div class="crumbs"><a href="#/">Home</a><span class="sep">/</span><a href="#/team">AI team</a><span class="sep">/</span><span>${e ? esc(e.name) : 'Unknown'}</span></div>`;
  if (!e) return crumbs + card('Not found', '<div class="empty">This AI employee does not exist or is outside your areas.</div>');
  const hd = ctx.health();
  const st = (ctx.state.employees || {})[id] || {};
  const [cls, label] = empStatus(ctx, id);
  let h = crumbs + `<div class="task-title"><span class="ico" style="font-family:var(--f-mono);font-weight:700">${esc(badge(ctx, id))}</span><div><div class="kick">${esc(e.title || '')} · ${esc((ctx.boot.layout.areas[e.area] || {}).name || '')}</div><h1>${esc(e.name)}</h1><div class="badges"><span class="pill p-grey"><i class="st-dot ${cls}"></i>${esc(label)}</span></div></div></div>`;
  h += `<div class="layout-task"><div class="stack">${card('What it does', `<div class="card-body"><p style="margin:0">${esc(e.role || '')}</p></div>`)}`;
  if (st.current || st.last) {
    const wid = st.current || st.last, d = ctx.detail(wid);
    h += card(st.current ? 'Working on' : 'Last task', `<div class="card-body">${d ? `<p style="margin:0 0 10px"><b>${esc(d.root.title)}</b></p><p class="note">${esc((d.root.finding || {}).headline || plainNext(ctx, d.item.next) || '')}</p><div class="form-row"><a class="btn sm" href="#/task/${encodeURIComponent(d.root.id)}">Open the task</a><a class="btn sm" href="#/floor" data-floor-emp="${esc(id)}">Show on the live floor</a></div>` : LOADING}</div>`);
  }
  h += '</div><div class="stack">';
  if (!hd) return h + loading + '</div></div>';
  const x = hd.employees[id];
  if (!x) return h + '</div></div>';
  const acc = x.accuracy, ev = x.eval;
  const kv = [['Health', pill(HEALTH, x.status)], ['Tasks waiting', x.queue], ['Finished this week', x.completed_7d],
    ['Problems today', x.failed_24h ? `${x.failed_24h} could not finish` : 'None'], ['Accuracy', acc ? `${pct(acc.score)} of ${acc.reviewed} checked` : 'Not enough history'],
    ['Practice tests', ev ? `${pct(ev.pass_rate)} passed` : 'Not run yet'], ['Last finished', esc(ctx.clock.stamp(x.last_success))]];
  if (isOwner(ctx)) kv.push(['AI cost today', usd(x.cost_24h_usd)]);
  h += card('Health', `<div class="card-body" style="padding-top:4px;padding-bottom:4px"><dl class="kv">${kv.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>${x.reasons.length ? `<ul class="facts">${x.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}</div>`);
  if (isOwner(ctx)) {
    h += x.paused
      ? card('Paused', `<div class="card-body"><p class="note">Reason: ${esc(x.paused_reason || 'none given')}. It takes no new tasks and does nothing until you restart it.</p><div class="form-row"><button class="btn go" data-resume="${esc(id)}">Restart</button></div>${errorNote(ctx, 'employee')}</div>`)
      : card('Pause this AI employee', `<div class="card-body"><p class="note">It stops straight away: no new tasks and no actions. Whatever it was doing waits until you restart it.</p><input id="pauseReason" type="text" maxlength="500" placeholder="Why? e.g. checking a wrong suggestion" style="margin-top:10px"><div class="form-row"><button class="btn danger" data-pause="${esc(id)}">Pause</button></div>${errorNote(ctx, 'employee')}</div>`);
  }
  return h + '</div></div>';
}

// ---------------------------------------------------------------- data sources
export function dataPage(ctx) {
  const srcs = ctx.state.sources || ctx.boot.sources || [];
  const fresh = srcs.filter((s) => s.freshness === 'CURRENT').length;
  let h = head('Settings', 'Data sources', 'Where your AI team gets its numbers. Read-only: nothing here changes your systems.', isOwner(ctx) ? '<a class="btn sm" href="#/controls">Pause a feed</a>' : '');
  return h + card('Data feeds', `<div class="table-wrap"><table class="table"><thead><tr><th>Feed</th><th>What it contains</th><th>Status</th><th class="num">Last updated</th></tr></thead><tbody>${srcs.map((s) =>
    `<tr><td><b>${esc(s.label)}</b>${techRef(ctx, s.id)}</td><td class="muted">${esc(s.datasets.join(', '))}${s.error && (ctx.technical || s.freshness !== 'CURRENT') ? `<div class="err-msg" style="margin-top:4px">${esc(s.error)}</div>` : ''}</td><td>${pill(FRESH, s.freshness)}</td><td class="num mono">${esc(ctx.clock.stamp(s.last_success))}</td></tr>`).join('')}</tbody></table></div>`,
  { count: `${fresh}/${srcs.length}`, countCls: fresh === srcs.length ? 'green' : '', foot: '<span>A feed that stops updating is marked out of date, and no alerts are raised from it until it recovers.</span>' });
}

// ---------------------------------------------------------------- owner settings
export function controlsPage(ctx) {
  const c = ctx.controls();
  let h = head('Settings', 'Pause switches', 'Stop one kind of action or one data feed without stopping everything. To pause an AI employee, open it from AI team.') + errorNote(ctx, 'controls');
  if (!c) return h + loading;
  const row = (kind, x, sub) => `<li class="${x.paused ? 'off' : ''}"><span class="grow"><b>${esc(x.label)}</b>${sub ? `<small>${esc(sub)}</small>` : ''}${x.paused ? `<small class="bad">Paused${x.reason ? `: ${esc(x.reason)}` : ''}</small>` : ''}</span>${x.paused
    ? `<button class="btn go sm" data-control="${kind}" data-target="${esc(x.id)}" data-paused="0">Restart</button>`
    : `<button class="btn danger sm" data-control="${kind}" data-target="${esc(x.id)}" data-paused="1">Pause</button>`}</li>`;
  h += `<section class="card"><div class="card-body"><label class="field" for="controlReason" style="margin-top:0">Reason for pausing <span>(needed)</span></label><input id="controlReason" type="text" maxlength="500" placeholder="e.g. supplier audit this week"></div></section>`;
  h += card('Actions the AI can suggest', `<ul class="rows">${c.actions.map((x) => row('action', x, (ctx.boot.layout.areas[x.area] || {}).name)).join('') || '<li>None set up.</li>'}</ul>`, { foot: '<span>A paused action cannot be approved or carried out, not even automatically. Suggestions still arrive so nothing is missed.</span>' });
  h += card('Data feeds', `<ul class="rows">${c.sources.map((x) => row('source', x, x.owner ? `Fixed by: ${x.owner}` : '')).join('')}</ul>`, { foot: '<span>A paused feed is not read. Its figures are marked out of date until you restart it.</span>' });
  return h;
}

export function peoplePage(ctx) {
  const t = ctx.team();
  let h = head('Team', 'People & access', 'Who can sign in, what they can see and approve, and temporary access. Only founders and managers have accounts.') + errorNote(ctx, 'people');
  if (!t) return h + loading;
  const areas = ctx.boot.layout.areas;
  const areaNames = (list) => (list || []).map((a) => (areas[a] || {}).name || a).join(', ');
  h += card('People', `<div class="table-wrap"><table class="table"><thead><tr><th>Person</th><th>Can see</th><th class="num">Can approve up to</th></tr></thead><tbody>${t.people.map((p) =>
    `<tr><td><span class="owner"><span class="avatar" style="width:28px;height:28px;font-size:12px">${esc(initials(p.name))}</span><span><b>${esc(p.name)}</b><br><small class="muted">${esc(ROLE[p.role] || p.role)}${p.active ? '' : ' · disabled'}</small></span></span></td><td>${p.role === 'owner' ? 'Everything' : esc(areaNames(p.areas) || 'Nothing yet')}</td><td class="num">${p.role === 'owner' ? '—' : p.approval_limit != null ? esc(money(ctx.boot.tenant.currency, p.approval_limit)) : 'Role limit'}</td></tr>`).join('')}</tbody></table></div>`,
  { count: t.people.length, countCls: 'grey', foot: '<span>New accounts are added by your administrator. Every change of access is saved in the audit log.</span>' });
  const managers = t.people.filter((p) => p.role === 'manager' && p.active);
  if (managers.length) {
    h += card('Give temporary access', `<div class="card-body"><p class="note">Let a manager see more areas for a while, for example to cover for a colleague. It ends on its own and does not change what they may approve.</p>
      <div class="form-row"><select id="grantUser" aria-label="Who">${managers.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select><select id="grantHours" aria-label="For how long">${[[4, '4 hours'], [8, '8 hours'], [24, '1 day'], [72, '3 days'], [168, '1 week']].map(([v, l]) => `<option value="${v}" ${v === 24 ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <fieldset style="border:1px solid var(--line);border-radius:8px;margin:12px 0 0;padding:10px 12px"><legend class="note" style="padding:0 6px">Areas</legend>${Object.entries(areas).filter(([, a]) => a.building !== 'pavilion').map(([aid, a]) => `<label class="switch" style="margin:4px 14px 4px 0"><input type="checkbox" name="grantArea" value="${esc(aid)}"> ${esc(a.name)}</label>`).join('')}</fieldset>
      <div class="form-row"><input id="grantReason" type="text" maxlength="500" placeholder="Why? e.g. covering for the warehouse head on leave"><button class="btn primary" data-grant="1">Give access</button></div></div>`);
  }
  const active = t.grants.filter((g) => g.active), past = t.grants.filter((g) => !g.active);
  h += card('Temporary access now', active.length ? `<ul class="rows">${active.map((g) => `<li><span class="grow"><b>${esc(g.user)}: ${esc(areaNames(g.areas))}</b><small>Until ${esc(ctx.clock.stamp(g.expires_at))} · given by ${esc(g.granted_by)} · ${esc(g.reason)}</small></span><button class="btn danger sm" data-revoke="${g.id}">End now</button></li>`).join('')}</ul>` : '<div class="empty">Nobody has temporary access.</div>');
  if (past.length) h += card('Earlier temporary access', `<ul class="rows">${past.map((g) => `<li><span class="grow"><b>${esc(g.user)}: ${esc(areaNames(g.areas))}</b><small>${g.revoked_at ? `Ended early ${esc(ctx.clock.stamp(g.revoked_at))}` : `Ended ${esc(ctx.clock.stamp(g.expires_at))}`} · ${esc(g.reason)}</small></span></li>`).join('')}</ul>`, { count: past.length, countCls: 'grey' });
  return h;
}

export const SUGGESTED = {
  anthropic: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'],
  gemini: ['gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'],
};
const EFFORT = [['low', 'Light (fastest, cheapest)'], ['medium', 'Normal'], ['high', 'Thorough'], ['xhigh', 'Very thorough'], ['max', 'Maximum (slowest, most expensive)']];
const KEY_STATE = { panel: 'Key saved here', environment: 'Key set by your administrator', not_needed: 'No key needed', none: 'No key yet' };
export function aiPage(ctx) {
  const m = ctx.aiSettings();
  let h = head('Settings', 'AI models & keys', 'Choose which AI company and model your AI team uses, and add the keys that connect them. Any provider works: Claude, ChatGPT, Gemini, DeepSeek and others.') + errorNote(ctx, 'ai');
  if (!m) return h + loading;
  const provs = m.providers;
  const tier = (t, title, sub) => {
    const x = m.tiers[t], test = (ctx.aiTest || {})[t];
    return card(title, `<div class="card-body"><p class="note">${sub}</p>
      <label class="field" for="prov_${t}">AI company</label><select id="prov_${t}" data-prov-tier="${t}">${provs.map((p) => `<option value="${esc(p.id)}" ${p.id === x.provider ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}</select>
      <label class="field" for="model_${t}">Model</label><input id="model_${t}" type="text" maxlength="100" list="models_${t}" value="${esc(x.model)}" placeholder="Model name, exactly as the AI company lists it"><datalist id="models_${t}">${(SUGGESTED[x.provider] || []).map((v) => `<option value="${esc(v)}">`).join('')}</datalist>
      <label class="field" for="effort_${t}">How hard it thinks</label><select id="effort_${t}">${EFFORT.map(([v, l]) => `<option value="${v}" ${v === x.effort ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <details class="why" data-keep="price-${t}"><summary>Price (for cost tracking with non-Claude models)</summary><div class="form-row"><input id="pin_${t}" type="number" min="0" step="any" value="${x.price ? esc(x.price.input) : ''}" placeholder="Input $ per million"><input id="pout_${t}" type="number" min="0" step="any" value="${x.price ? esc(x.price.output) : ''}" placeholder="Output $ per million"></div></details>
      <div class="form-row"><button class="btn primary" data-route-save="${t}">Save</button><button class="btn" data-ai-test="${t}">Test connection</button>${x.changed_in_panel ? `<button class="btn" data-route-reset="${t}">Back to company default (${esc(x.default.provider)}: ${esc(x.default.model)})</button>` : ''}</div>
      ${test ? `<p class="${test.ok ? 'note' : 'err-msg'}" style="margin-top:10px;${test.ok ? 'color:var(--ok)' : ''}">${test.ok ? `Connected: ${esc(test.model)} answered.` : `Not working: ${esc(test.message)}`}</p>` : ''}
      ${x.changed_in_panel ? `<p class="note" style="margin-top:8px">Changed by ${esc(x.changed_by || 'an owner')} on ${esc(ctx.clock.stamp(x.changed_at))}.</p>` : ''}</div>`,
    { right: x.changed_in_panel ? '<span class="pill p-blue">Changed here</span>' : '<span class="pill p-grey">Company default</span>' });
  };
  h += `<div class="layout-2" style="grid-template-columns:1fr 1fr"><div>${tier('strong', 'Careful AI', 'Investigates problems, explains causes and makes suggestions. Pick a strong model: quality matters more than price here.')}</div><div>${tier('cheap', 'Quick AI', 'Writes briefs and alerts in plain words and answers simple questions. Anything it cannot back with your data goes to the careful AI.')}</div></div>`;
  let keys = '<div class="card-body"><p class="note">A key connects the platform to an AI company\'s account (you get it from that company\'s website, and they bill you). Keys are stored encrypted and never shown again: only the last 4 characters, so you can recognise them.</p>';
  if (!m.can_store_keys) keys += '<p class="err-msg">Keys cannot be saved here yet: your administrator must first set a server secret (WORKFORCE_SECRET_KEY). Keys set on the server still work.</p>';
  keys += '</div>';
  h += `<div style="margin-top:16px">${card('Keys', keys + `<ul class="rows">${provs.map((p) => `<li><span class="grow"><b>${esc(p.label)}</b><small>${esc(KEY_STATE[p.key] || p.key)}${p.hint ? ` · ending ${esc(p.hint)}` : ''}${p.set_by ? ` · by ${esc(p.set_by)}, ${esc(ctx.clock.stamp(p.set_at))}` : ''}</small>${p.unreadable ? '<small class="bad">A saved key can no longer be unlocked (the server secret changed). Enter it again.</small>' : ''}</span>
    ${m.can_store_keys ? `<input id="key_${esc(p.id)}" type="password" autocomplete="off" maxlength="500" placeholder="Paste key" style="max-width:220px"><button class="btn sm" data-key-save="${esc(p.id)}">Save</button>${p.key === 'panel' || p.unreadable ? `<button class="btn danger sm" data-key-remove="${esc(p.id)}">Remove</button>` : ''}` : ''}</li>`).join('')}</ul>`,
  { foot: '<span>Every change of model or key is saved in the audit log (never the key itself). Changes apply to the next task.</span>' })}</div>`;
  return h;
}

export function usagePage(ctx) {
  const u = ctx.usage();
  let h = head('Settings', 'AI cost & quality', 'What the AI costs and how well it does on practice tests with known answers.');
  if (!u) return h + loading;
  const used = { investigation: 'Investigating problems', question: 'Answering questions', brief: 'Writing briefs', notification: 'Writing alerts', connection_test: 'Connection tests' };
  const model = (r) => esc(r.split(':')[1] || r);
  h += '<div class="kpis">' + [['Careful AI', model(u.routes.strong), 'Investigations and suggestions'], ['Quick AI', model(u.routes.cheap), 'Briefs, alerts, simple questions'],
    ['Spent today', usd(u.budget.cost_24h_usd), u.budget.cost_budget_usd != null ? `of a ${usd(u.budget.cost_budget_usd)} daily limit` : 'No daily limit'],
    ['Tokens today', u.budget.tokens_24h.toLocaleString('en-IN'), `of ${u.budget.token_budget.toLocaleString('en-IN')} allowed`]]
    .map(([l, v, s]) => `<div class="kpi"><div class="top"><span>${l}</span><span class="ico">${icon('gauge', 14)}</span></div><b style="font-size:18px">${v}</b><small>${esc(s)}</small></div>`).join('') + '</div>';
  const rows = u.last_7d;
  h += card('Last 7 days', rows.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Used for</th><th>AI</th><th class="num">Times</th><th class="num">Cost</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(used[r.task] || humanize(r.task))}</td><td>${esc(r.model)}${tech(ctx, `<br><small class="muted">${esc(r.provider)} · ${esc(r.tier)} · ${r.input_tokens.toLocaleString()} in / ${r.output_tokens.toLocaleString()} out</small>`)}</td><td class="num">${r.calls}</td><td class="num">${usd(r.cost_usd)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">The AI has not been used yet.</div>');
  h += card('Practice tests: which AI does best?', u.eval_comparison.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>AI setup</th><th class="num">Score</th><th class="num">Cost per test</th></tr></thead><tbody>${u.eval_comparison.map((c) => `<tr><td>${model(c.strong)} + ${model(c.cheap)}</td><td class="num">${pct(c.pass_rate)}${c.skipped ? `<br><small class="muted">${c.skipped} not scored (AI was off)</small>` : ''}</td><td class="num">${usd(c.avg_cost_usd)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No practice tests have been run yet. Your administrator can run them with one command.</div>');
  if ((u.needs_test || []).length) h += card('Mistakes to turn into tests', `<ul class="rows">${u.needs_test.map((t) => `<li><span class="grow"><b>${esc(empName(ctx, t.employee))}</b><small>${t.note ? `“${esc(t.note)}”` : 'Marked wrong'}</small></span><a class="btn sm" href="#/task/${encodeURIComponent(t.work_id)}">Open</a><a class="btn sm" href="/api/work/${encodeURIComponent(t.work_id)}/test-draft" download>Test draft</a></li>`).join('')}</ul>`, { count: u.needs_test.length });
  return h;
}

// ---------------------------------------------------------------- help
export function helpPage(ctx) {
  const certainty = Object.values(CERTAINTY).map(([w, cls, m]) => `<li><span class="pill ${cls}">${esc(w)}</span> ${esc(m)}</li>`).join('');
  const kinds = Object.values(KIND).map(([w, m]) => `<li><b>${esc(w)}</b>: ${esc(m)}</li>`).join('');
  const c = (t, b) => `<section class="card"><div class="card-body"><h3 style="font-size:15px;margin-bottom:6px">${t}</h3>${b}</div></section>`;
  return head('Guide', 'How this works', `A short guide for ${esc(ctx.boot.tenant.name)}`) + `<div class="help-grid">
    ${c('Your AI team', '<p style="margin:0">Each AI employee looks after one part of the business, such as stock, sales or the factory. They watch your data all day. When something looks wrong they look into it, find the reason and tell you.</p>')}
    ${c('Where the numbers come from', '<p style="margin:0">Only from your own systems. The AI never makes up a number: every figure has <b>proof</b> you can open, showing which system it came from and when it was last updated.</p>')}
    ${c('Reading a finding', `<ul>${kinds}</ul>`)}
    ${c('How sure is it?', `<ul style="list-style:none;padding:0">${certainty}</ul>`)}
    ${c('You stay in charge', '<p style="margin:0">When an AI employee suggests something, such as buying stock, it waits in <b>Decisions</b>. You can approve, change the numbers, say no, or ask it to check more. It can never approve its own suggestion.</p>')}
    ${c('Your daily view', '<p style="margin:0"><b>Home</b> shows key figures, what needs you, good news and live activity. <b>Tasks</b> lists everything waiting, in progress and finished. <b>Reports</b> holds the daily, weekly and monthly briefs.</p>')}
    ${c('Ask a question', '<p style="margin:0">Type a question in the box at the top, for example “Which products might run out this week?”. Questions that are not about your business are politely declined.</p>')}
    ${c('When the evidence is weak', '<p style="margin:0">How sure an answer is depends on the evidence, not on the AI\'s opinion. Open <b>Why this rating</b> on a finding to see the reasons, and <b>See the records</b> on any proof to see the rows from your own systems.</p>')}
  </div>`;
}

// ---------------------------------------------------------------- live floor side panel
export function floorSide(ctx) {
  const { boot, state } = ctx, sel = ctx.floorSel || {};
  if (sel.kind === 'worker' && boot.employees[sel.id]) {
    const e = boot.employees[sel.id], st = (state.employees || {})[sel.id] || {};
    const [cls, label] = empStatus(ctx, sel.id);
    const wid = st.current || st.last, d = wid ? ctx.detail(wid) : null;
    return `<div class="eyebrow">AI employee</div><h2 style="font-size:17px;margin:4px 0 2px">${esc(e.name)}</h2><p class="note">${esc(e.title || '')}</p>
      <dl class="kv" style="margin-top:10px"><div><dt>Right now</dt><dd><span class="owner"><i class="st-dot ${cls}"></i>${esc(label)}</span></dd></div><div><dt>Where</dt><dd>${esc(ctx.world.whereIs(sel.id))}</dd></div><div><dt>Tasks waiting</dt><dd>${st.queue || 0}</dd></div>
      <div><dt>${st.current ? 'Working on' : 'Last task'}</dt><dd>${d ? `<a href="#/task/${encodeURIComponent(d.root.id)}">${esc(d.root.title)}</a>` : wid ? '…' : '—'}</dd></div></dl>
      <div class="form-row"><button class="btn sm" data-follow="${esc(sel.id)}" aria-pressed="${ctx.world.follow === sel.id}">${ctx.world.follow === sel.id ? 'Following' : 'Follow'}</button><a class="btn sm" href="#/team/${encodeURIComponent(sel.id)}">About ${esc(e.name)}</a></div>`;
  }
  if (sel.kind === 'area' && boot.layout.areas[sel.id]) {
    const a = boot.layout.areas[sel.id];
    const emps = Object.entries(boot.employees).filter(([, x]) => x.area === sel.id);
    let h = `<div class="eyebrow">Place</div><h2 style="font-size:17px;margin:4px 0 2px">${esc(a.name)}</h2><p class="note">${esc(a.subtitle || '')}</p>`;
    if (a.building === 'pavilion') {
      const aps = state.approvals || [];
      h += `<div class="stmt-group"><h4>Decisions waiting</h4>${aps.length ? `<ul class="mini-list">${aps.map((ap) => `<li><div><b><a href="#/task/${encodeURIComponent(ap.work_id)}">${esc(ap.title)}</a></b><small>${esc(empName(ctx, ap.employee))}</small></div></li>`).join('')}</ul>` : '<p class="note">None right now.</p>'}</div>`;
    }
    if (emps.length) h += `<div class="stmt-group"><h4>Who works here</h4><ul class="mini-list">${emps.map(([id, x]) => `<li><div><b><a href="#/team/${encodeURIComponent(id)}">${esc(x.name)}</a></b><small>${esc(empStatus(ctx, id)[1])}</small></div></li>`).join('')}</ul></div>`;
    const ds = boot.area_data[sel.id] || [];
    if (ds.length) h += `<div class="stmt-group"><h4>Information used here</h4><ul class="mini-list">${ds.map((d) => `<li><div><b>${esc(boot.datasets[d].label)}</b></div></li>`).join('')}</ul></div>`;
    return h;
  }
  const active = state.active || [];
  return `<div class="eyebrow">Right now</div><h2 style="font-size:17px;margin:4px 0 10px">${active.length ? plural(active.length, 'task') + ' in progress' : 'Nothing in progress'}</h2>
    ${active.length ? `<ul class="mini-list">${active.slice(0, 12).map((w) => `<li><div><b><a href="#/task/${encodeURIComponent(w.id)}">${esc(w.title)}</a></b><small>${esc(empName(ctx, w.employee))} · ${esc(word(STATUS, w.status))}</small></div></li>`).join('')}</ul>` : '<p class="note">Click a person or a building to see what they do.</p>'}`;
}
