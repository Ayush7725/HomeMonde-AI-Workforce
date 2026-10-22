// Thin client for the workforce API. Session cookie is HttpOnly; mutating calls carry the CSRF token.
let csrf = '';
let onUnauthorized = null;

export function setCsrf(token) { csrf = token || ''; }
/** Called when the server says the session is over (signed out elsewhere, expired): the app shows sign-in again. */
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export const OFFLINE = "Can't reach the AI Workforce server. Check your internet connection and try again. Nothing was changed.";

/** A message a person can act on, from the server's reply. Never shows raw objects or codes. */
export function messageFor(status, data) {
  const detail = data && data.detail;
  if (Array.isArray(detail) && detail.length) {  // a form field the server refused
    const d = detail[0] || {};
    const field = String((d.loc || []).slice(-1)[0] || 'value').replace(/_/g, ' ');
    const max = d.ctx && (d.ctx.max_length ?? d.ctx.le);
    const min = d.ctx && (d.ctx.min_length ?? d.ctx.ge);
    if (/too_long/.test(d.type || '') && max != null) return `The ${field} is too long: at most ${max} characters.`;
    if (/too_short/.test(d.type || '') && min != null) return `The ${field} is too short: at least ${min} characters.`;
    return `Please check the ${field}: ${String(d.msg || 'it is not valid').replace(/^Value error, /, '')}.`;
  }
  if (typeof detail === 'string' && detail) return detail.charAt(0).toUpperCase() + detail.slice(1);
  if (status === 401) return 'Your session has ended. Please sign in again.';
  if (status === 403) return 'You are not allowed to do this.';
  if (status === 404) return 'This could not be found. It may have been removed, or it belongs to an area you do not cover.';
  if (status === 429) return 'Too many requests just now. Please wait a moment and try again.';
  if (status >= 500) return 'Something went wrong on the server. Nothing was changed; please try again in a moment.';
  return `The request did not go through (${status}).`;
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  } catch {
    throw new ApiError(0, OFFLINE);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    if (res.status === 401 && onUnauthorized && !path.startsWith('/api/auth/login')) onUnauthorized();
    throw new ApiError(res.status, messageFor(res.status, data));
  }
  return data;
}

// Server-sent events with automatic reconnect from the last sequence number seen.
export function subscribe(after, onEvent, onState) {
  let last = after, es = null, closed = false, retry = 1000;
  const open = () => {
    if (closed) return;
    es = new EventSource(`/api/events/stream?after=${last}`);
    es.onopen = () => { retry = 1000; onState(true); };
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.seq <= last) return;
      last = ev.seq;
      onEvent(ev);
    };
    es.onerror = () => {
      onState(false);
      es.close();
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 15000);
    };
  };
  open();
  return () => { closed = true; if (es) es.close(); };
}
