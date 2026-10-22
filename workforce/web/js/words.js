// Plain-language wording for people who run the business, not the software.
// Every code the system uses (statuses, labels, event types, evidence refs) is translated here, in one place.
// The technical words stay available behind "Show technical details".

export const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export const humanize = (s) => String(s || '').replace(/_/g, ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase());

// [words, pill colour]
export const STATUS = {
  QUEUED: ['Waiting to start', 'p-grey'], RUNNING: ['Working on it', 'p-blue'], INVESTIGATING: ['Looking into it', 'p-blue'],
  VALIDATING: ['Double-checking the numbers', 'p-violet'], WAITING_FOR_DATA: ['Waiting for data', 'p-warn'],
  NEEDS_HUMAN_REVIEW: ['Waiting for your decision', 'p-orange'], COMPLETED: ['Done', 'p-green'], FAILED: ["Couldn't finish", 'p-red'],
  NEEDS_RETRY: ['Will try again shortly', 'p-warn'], ESCALATED: ['Passed to a person', 'p-red'],
};
export const EMPLOYEE = {
  IDLE: ['Free', 'p-grey'], QUEUED: ['Has work waiting', 'p-blue'], WORKING: ['Working', 'p-blue'], WAITING_APPROVAL: ['Waiting for you', 'p-orange'],
};
// How a statement in a finding should be read
export const KIND = {
  FACT: ['From your data', 'Your numbers show this directly.'],
  OBSERVATION: ['What we noticed', 'A pattern in your numbers.'],
  INFERENCE: ['What it likely means', 'A conclusion drawn from the numbers above.'],
  HYPOTHESIS: ['Possible reason', 'Not checked yet. Treat it as a guess.'],
  RECOMMENDATION: ['Suggested next step', 'Nothing happens until a person decides.'],
};
export const CERTAINTY = {
  CONFIRMED: ['Certain', 'p-green', 'Your data shows this clearly.'],
  LIKELY: ['Very likely', 'p-blue', 'Strong signs in your data, with a little uncertainty.'],
  POSSIBLE: ['Possible', 'p-violet', 'Some signs in your data, but not enough to be sure.'],
  INSUFFICIENT_EVIDENCE: ['Not enough information', 'p-grey', 'There was not enough data to answer reliably.'],
  DATA_CONFLICT: ['Systems disagree', 'p-red', 'Two of your systems show different numbers for the same thing.'],
  DATA_STALE: ['Data out of date', 'p-warn', 'Some of the data used has not been updated recently.'],
};
export const OUTCOME = {
  FINDING: 'Answer found', INSUFFICIENT_EVIDENCE: 'Not enough information', DATA_CONFLICT: 'Systems disagree', DATA_STALE: 'Data out of date',
  OUT_OF_SCOPE: 'Not a business question', ACTION_TAKEN: 'Action done', REJECTED: 'You said no', HUMAN_ACTION: 'Handed to your team',
  EXPIRED: 'Decision expired', ACTION_FAILED: 'Action failed', HUMAN_REVIEW_REQUIRED: 'Needs a person to look', INVESTIGATION_FAILED: "Couldn't finish",
  ANSWERED: 'Answered', STOPPED: 'Stopped by a person', REVIEWED: 'Checked and closed by a person', RESOLVED: 'Fixed',
  CONDITION_CLEARED: 'Problem went away',
};
export const PRIORITY = { CRITICAL: ['Urgent', 'p-red'], HIGH: ['Important', 'p-orange'], MEDIUM: ['Normal', 'p-blue'], LOW: ['Low', 'p-grey'] };
export const FRESH = { CURRENT: ['Up to date', 'p-green'], STALE: ['Out of date', 'p-warn'], NEVER_SYNCED: ['Not connected yet', 'p-grey'] };
export const HEALTH = {
  HEALTHY: ['Working well', 'p-green'], DEGRADED: ['Having problems', 'p-warn'], FAILED: ['Stopped working', 'p-red'],
  PAUSED: ['Paused', 'p-grey'], NEEDS_ATTENTION: ['Needs attention', 'p-orange'],
};
export const DECISION = {
  PENDING: 'Waiting for a decision', APPROVED: 'Approved', MODIFIED: 'Approved with changes', REJECTED: 'Rejected',
  MORE_INVESTIGATION: 'Sent back to check more', EXPIRED: 'Expired before anyone decided',
};
export const VERDICT = { CORRECT: 'Right', PARTLY: 'Partly right', WRONG: 'Wrong' };
export const ROLE = { owner: 'Owner', manager: 'Manager' };

export function word(map, key) {
  const v = map[key];
  return v ? (Array.isArray(v) ? v[0] : v) : humanize(key);
}
export function pill(map, key) {
  const v = map[key];
  const [t, cls] = Array.isArray(v) ? v : [v || humanize(key), 'p-grey'];
  return `<span class="pill ${cls}">${esc(t)}</span>`;
}

// ---------------------------------------------------------------- numbers

export function money(currency, v) {
  if (v === null || v === undefined) return '—';
  try { return new Intl.NumberFormat('en-IN', { style: 'currency', currency, maximumFractionDigits: 0 }).format(v); }
  catch { return `${currency} ${Math.round(v).toLocaleString()}`; }
}

/** Show a metric the way a person would say it: shares as %, money with its currency, "x" as times. */
export function value(v, unit, currency) {
  if (v === null || v === undefined) return 'not known';
  if (typeof v !== 'number') return String(v);
  const u = (unit || '').toLowerCase();
  if (u.startsWith('share')) return `${(v * 100).toLocaleString('en-IN', { maximumFractionDigits: 1 })}%`;
  if (['inr', 'usd', 'gbp', 'eur'].includes(u)) return money(unit.toUpperCase(), v);
  if (u === 'x') return `${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}×`;
  const n = v.toLocaleString('en-IN', { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 1 });
  return u ? `${n} ${unit}` : n;
}

/** Turn a piece of evidence (tool result, calculation, metric) into short readable lines. */
export function readable(ev, boot) {
  const r = ev.result || {}, cur = boot.tenant.currency;
  if (ev.kind === 'calc') return [`${r.label || 'Calculation'}: ${value(r.value)}`];
  if (r.conflict || ev.name === 'source_conflict') {
    return [Object.entries(r).filter(([k]) => boot.datasets[k]).map(([k, v]) => `${boot.datasets[k].label}: ${v}`).join(' · ')];
  }
  if (r.metrics) {
    return Object.values(r.metrics).map((m) => {
      if (m.rows) return `${m.label}: ${m.rows.slice(0, 5).map((row) => `${Object.entries(row).filter(([k]) => k !== 'value').map(([, x]) => x).join(' ')} ${value(row.value, m.unit, cur)}`).join(', ')}`;
      const by = Object.keys(m).find((k) => k.startsWith('by_'));
      if (by) return `${m.label}: ${Object.entries(m[by]).map(([k, x]) => `${k} ${value(x, m.unit, cur)}`).join(', ')}`;
      return `${m.label}: ${value(m.value, m.unit, cur)}`;
    });
  }
  if (r.rows) {
    if (!r.rows.length) return ['Nothing found.'];
    const lines = r.rows.slice(0, 5).map((row) => Object.entries(row).filter(([, x]) => x !== null && x !== '').map(([k, x]) => `${humanize(k)}: ${x}`).join(' · '));
    if (r.total_matches > 5) lines.push(`…and ${r.total_matches - 5} more`);
    return lines;
  }
  if ('value' in r) return [`${r.label || humanize(ev.name)}: ${value(r.value, r.unit, cur)}`];
  return [JSON.stringify(r)];
}

// ---------------------------------------------------------------- what to call things

export const toolName = (boot, name) => {
  const base = String(name || '').replace(/^ask_[a-z0-9_]+__/, '');
  const t = boot.tools[base];
  return t ? (t.label || t.description.replace(/\.$/, '')) : humanize(base);
};
const emp = (boot, id) => (boot.employees[id] || {}).name || (id ? humanize(id) : 'The system');
const sourceName = (boot, id) => ((boot.sources || []).find((s) => s.id === id) || {}).label || humanize(id);

const AI_OFF = [
  [/no credentials/, "The AI service isn't connected yet, so employees report the numbers without explanations. Owners can add a key in AI models & keys."],
  [/switched off/, 'AI explanations are switched off for this company.'],
  [/budget/, "Today's AI spending limit has been reached. Explanations resume tomorrow."],
];
export function aiOffReason(reason) {
  const hit = AI_OFF.find(([re]) => re.test(reason || ''));
  return hit ? hit[1] : 'AI explanations are temporarily unavailable.';
}

/** One plain sentence for an activity event. The original technical message stays available. */
export function sentence(ev, boot) {
  const who = emp(boot, ev.emp);
  const to = emp(boot, ev.to);
  switch (ev.type) {
    case 'DATA_SYNC': {
      const fresh = Object.values(ev.new_rows || {}).reduce((a, n) => a + n, 0);
      const late = / out of date /.test(ev.msg) ? ' Some of it has stopped moving, so it is marked out of date.' : '';
      return `New data arrived from ${sourceName(boot, ev.source)}${fresh ? ` (${fresh.toLocaleString('en-IN')} new record${fresh === 1 ? '' : 's'})` : ''}.${late}`;
    }
    case 'DATA_SOURCE_FAILURE': return `Couldn't get new data from ${sourceName(boot, ev.source)}. Older data from it is now marked out of date.`;
    case 'DATA_REJECTED': return `Some rows in ${(boot.datasets[ev.dataset] || {}).label || 'a data file'} had mistakes and were left out.`;
    case 'RULE_TRIGGERED': return 'A routine check spotted something worth looking into.';
    case 'RULE_CLEARED': return 'A routine check found the problem has gone, so its task closed by itself.';
    case 'WORK_ITEM_CREATED': return ev.parent ? `${who} was asked to help: ${ev.title}.` : `${who} started a new task: ${ev.title}.`;
    case 'STATE_CHANGED': return `${who}: ${word(STATUS, ev.status).toLowerCase()}.`;
    case 'TOOL_CALLED': return `${who} is checking: ${toolName(boot, ev.tool).toLowerCase()}.`;
    case 'TOOL_RESULT': return `${who} got the numbers.`;
    case 'TOOL_ERROR': return `${who} couldn't get some information. Nothing was guessed.`;
    case 'CALCULATION': return `${who} worked out a figure.`;
    case 'AI_REQUEST': return `${who} asked ${to} for help.`;
    case 'EVIDENCE_ADDED': return `${word(KIND, ev.kind)}: ${ev.msg.replace(/^[A-Z]+ · /, '')}`;
    case 'FINDING_PRODUCED': return `${who} finished and wrote up what it found.`;
    case 'HANDOFF': return `${who} passed the result to ${to}.`;
    case 'FINDING_RECEIVED': return `${who} reviewed it${/needs attention/.test(ev.msg) ? ' and flagged it for your attention' : ' and added it to the daily brief'}.`;
    case 'APPROVAL_REQUESTED': return `Your decision is needed: ${ev.msg.replace(/^Approval \S+ requested · /, '').replace(/ → .*$/, '')}.`;
    case 'HUMAN_DECISION': return `${ev.by || 'Someone'} decided: ${word(DECISION, ev.decision).toLowerCase()}.`;
    case 'PRE_ACTION_CHECKS': return 'Safety checks passed before acting.';
    case 'ACTION_EXECUTED': return `${who} carried out the approved action.`;
    case 'RESULT_VERIFIED': return 'The action was confirmed as done.';
    case 'ACTION_FAILED': return 'The approved action could not be completed. It can be retried.';
    case 'ROUTE_ESCALATED': return `${who} handed the question to a more careful AI to make sure the answer is right.`;
    case 'EMPLOYEE_PAUSED': return `${ev.by || 'An owner'} paused ${who}.`;
    case 'EMPLOYEE_RESUMED': return `${ev.by || 'An owner'} restarted ${who}.`;
    case 'REPORT_GENERATED': return 'A new business brief is ready.';
    case 'NOTIFICATION': return `Alert sent: ${ev.msg.replace(/^Notified · /, '')}.`;
    case 'FINDING_REVIEWED': return `Someone marked a finding as ${word(VERDICT, ev.verdict).toLowerCase()}.`;
    case 'OUTCOME_CHECKED': return `A past prediction was checked against what really happened: ${word(VERDICT, ev.verdict).toLowerCase()}.`;
    case 'EVAL_RUN': return 'The AI practice tests were run.';
    case 'CONTROL_CHANGED': return `${ev.by || 'An owner'} ${ev.paused ? 'paused' : 'restarted'} ${ev.control === 'source' ? 'the data feed' : 'the action'} “${ev.msg.replace(/^.*? (paused|resumed) (action |data feed )/, '').replace(/ · .*$/, '')}”.`;
    case 'WORK_STOP_REQUESTED': return `${ev.by || 'Someone'} stopped a task of ${who}.`;
    case 'REVIEW_RESOLVED': return `${ev.by || 'Someone'} ${ev.decision === 'close' ? 'checked and closed' : 'asked for another look at'} a task of ${who}.`;
    case 'APPROVAL_ASSIGNED': return `${ev.by || 'Someone'} handed a decision to ${ev.to_user || 'a colleague'}.`;
    case 'ACCESS_GRANTED': return 'An owner gave a manager temporary access to more areas.';
    case 'ACCESS_REVOKED': return 'An owner ended a temporary access early.';
    case 'AI_SETTINGS_CHANGED': return `${ev.by || 'An owner'} changed which AI model the team uses.`;
    case 'AI_KEY_SET': return `${ev.by || 'An owner'} saved an AI key.`;
    case 'AI_KEY_REMOVED': return `${ev.by || 'An owner'} removed an AI key.`;
    case 'LOGIN': return ev.msg;
    case 'LOGOUT': return ev.msg;
    case 'LOGIN_FAILED': return 'Someone failed to sign in.';
    case 'ACCESS_DENIED': return 'Someone tried to approve something they are not allowed to.';
    default: return ev.msg;
  }
}
