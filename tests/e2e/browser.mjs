// Real-browser checks of the ways real people broke the web app: two tabs, an ended session, no internet, bad numbers,
// typing while the page updates. Drives headless Chrome over the DevTools protocol with Node's own WebSocket, so it
// needs nothing installed. Run by tests/test_e2e_browser.py, which starts the server and passes the setup as JSON:
//   node tests/e2e/browser.mjs '{"base": "http://127.0.0.1:PORT", "password": "...", ...}'
// Prints one line per check and exits non-zero if any failed.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const cfg = JSON.parse(process.argv[2]);
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- a small DevTools client
async function launch() {
  const profile = mkdtempSync(join(tmpdir(), 'wf-e2e-'));
  const proc = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run',
    '--no-default-browser-check', '--disable-gpu', '--window-size=1400,900', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = '';
    const t = setTimeout(() => reject(new Error('Chrome did not start')), 20000);
    proc.stderr.on('data', (d) => {
      buf += d;
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(t); resolve(m[1]); }
    });
  });
  const ws = new WebSocket(wsUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) {
      const { resolve, reject } = waiting.get(msg.id);
      waiting.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
    }
  };
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const n = ++id;
    waiting.set(n, { resolve, reject });
    ws.send(JSON.stringify({ id: n, method, params, sessionId }));
  });
  const close = async () => {
    try { ws.close(); } catch { /* gone */ }
    const exited = new Promise((r) => proc.once('exit', r));
    proc.kill('SIGKILL');
    await exited;
    try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch { /* a temp folder; the OS clears it */ }
  };
  return { send, close };
}

async function newTab(browser, url) {
  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
  const tab = {
    send: (m, p) => browser.send(m, p, sessionId),
    async eval(expr) {
      const r = await browser.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }, sessionId);
      if (r.exceptionDetails) throw new Error(`${r.exceptionDetails.text} ${(r.exceptionDetails.exception || {}).description || ''}`);
      return r.result.value;
    },
    async waitFor(expr, what, ms = 10000) {
      const end = Date.now() + ms;
      let last;
      while (Date.now() < end) {
        try { last = await tab.eval(expr); if (last) return last; } catch (e) { last = e.message; }
        await sleep(150);
      }
      throw new Error(`timed out waiting for ${what} (last: ${JSON.stringify(last)})`);
    },
    async go(path) {
      await tab.send('Page.navigate', { url: cfg.base + path });
      await tab.waitFor("document.readyState === 'complete'", 'page load');
    },
    text: () => tab.eval("document.querySelector('#app').hidden ? document.querySelector('#login').innerText : document.querySelector('#content').innerText"),
  };
  await tab.send('Page.enable');
  await tab.send('Runtime.enable');
  if (url) await tab.go(url);
  return tab;
}

// ---------------------------------------------------------------- page helpers
const q = JSON.stringify;
async function signIn(tab, email) {
  await tab.waitFor("!document.querySelector('#login').hidden", 'sign-in screen');
  await tab.eval(`(() => { document.querySelector('#email').value = ${q(email)}; document.querySelector('#password').value = ${q(cfg.password)};
    document.querySelector('#loginForm').requestSubmit(); })()`);
  await tab.waitFor("!document.querySelector('#app').hidden && document.querySelector('#userName').textContent.length > 0", `signed in as ${email}`);
}
const content = "document.querySelector('#content')";
const card = (ap) => `[...${content}.querySelectorAll('[data-ap=${q(ap)}]')][0]`;
const clickSel = (sel) => `(() => { const el = ${content}.querySelector(${q(sel)}) || document.querySelector(${q(sel)}); if (!el) return false; el.focus(); el.click(); return true; })()`;  // focus first, as a mouse click does
const clearToast = "document.querySelector('#toast').hidden = true";
const toast = "(() => { const t = document.querySelector('#toast'); return t && !t.hidden ? t.textContent : ''; })()";

// ---------------------------------------------------------------- checks
const results = [];
let A = null;
async function check(name, fn) {
  try { await fn(); results.push([true, name]); console.log(`PASS ${name}`); } catch (e) {
    results.push([false, name]);
    let seen = '';
    try { seen = String(await A.text()).replace(/\s+/g, ' ').slice(0, 300); } catch { /* page gone */ }
    console.log(`FAIL ${name}: ${e.message}\n     the page showed: ${seen}`);
  }
}
function expect(cond, msg) { if (!cond) throw new Error(msg); }

const browser = await launch();
try {
  A = await newTab(browser, '/');
  await signIn(A, cfg.owner);

  await check('changing the numbers shows the allowed range and refuses a bad number in the browser', async () => {
    await A.go('/#/decisions');
    await A.waitFor(`${card(cfg.purchase)}`, 'the purchase request');
    expect(await A.eval(clickSel(`[data-act=modify][data-ap=${q(cfg.purchase)}]`)), 'no "Change the numbers" button');
    await A.waitFor(`${content}.querySelector('#mod_quantity')`, 'the quantity box');
    const label = await A.eval(`${content}.querySelector('label[for=mod_quantity]').textContent`);
    expect(/at least 1/.test(label), `no range shown: "${label}"`);
    expect(await A.eval(`${content}.querySelector('#mod_unit_price').readOnly`), 'the locked price can be edited');
    await A.eval(`(() => { const i = ${content}.querySelector('#mod_quantity'); i.focus(); i.value = '0'; })()`);
    await A.eval(clickSel(`[data-act=modify-confirm][data-ap=${q(cfg.purchase)}]`));
    const err = await A.waitFor(`(${content}.querySelector('.err-msg') || {}).textContent`, 'an error message');
    expect(/at least 1/.test(err), `unexpected message "${err}"`);
    expect(await A.eval(`${content}.querySelector('#mod_quantity').value`) === '0', 'what the person typed was wiped');
    await A.eval(clickSel('[data-act=cancel]'));
  });

  await check('a suggestion with no numbers has no "Change the numbers" button', async () => {
    await A.waitFor(`${card(cfg.wordsOnly)}`, 'the carrier escalation');
    expect(!(await A.eval(`!!${content}.querySelector('[data-act=modify][data-ap=${q(cfg.wordsOnly)}]')`)), 'the button is shown');
  });

  await check('a note being typed survives choosing an answer', async () => {
    await A.go(`/#/task/${encodeURIComponent(cfg.task)}`);
    await A.waitFor(`${content}.querySelector('#reviewNote')`, 'the feedback box');
    await A.eval(`(() => { const t = ${content}.querySelector('#reviewNote'); t.focus(); t.value = 'the supplier called us'; })()`);
    await A.eval(clickSel('[data-verdict=PARTLY]'));
    await A.waitFor(`${content}.querySelector('[data-verdict=PARTLY].selected')`, 'the chosen answer');
    const v = await A.eval(`${content}.querySelector('#reviewNote').value`);
    expect(v === 'the supplier called us', `note became "${v}"`);
  });

  await check('the next step is written without technical model names', async () => {
    const t = await A.eval(`${content}.textContent`);
    expect(t.includes(cfg.nextPlain), `next step "${cfg.nextPlain}" not shown`);
    expect(!/[a-z]+:[a-z0-9.-]+-(lite|flash|pro|mini)/i.test(t) && !t.includes(cfg.model), 'a model name is shown');
  });

  await check('a link to something that does not exist ends in a message, not "Loading…" for ever', async () => {
    await A.go('/#/reports/NO-SUCH-REPORT');
    await A.waitFor(`/could not|not be found|Try again/i.test(${content}.innerText)`, 'a message', 8000);
    await A.go('/#/task/NO-SUCH-TASK');
    await A.waitFor(`!/Loading…/.test(${content}.innerText) && ${content}.innerText.length > 0`, 'the task page to settle', 8000);
  });

  await check('server error details are shown as words, never [object Object]', async () => {
    const m = await A.eval(`import('/static/js/api.js').then((m) => m.messageFor(422, { detail: [{ type: 'string_too_long', loc: ['body', 'reason'], msg: 'String should have at most 500 characters', ctx: { max_length: 500 } }] }))`);
    expect(m === 'The reason is too long: at most 500 characters.', `got "${m}"`);
    const r = await A.eval(`import('/static/js/api.js').then((m) => m.messageFor(422, { detail: [{ loc: ['body', 'params', 'quantity'], msg: 'Value error, must be a number' }] }))`);
    expect(!/object/i.test(r) && /quantity/.test(r), `got "${r}"`);
  });

  await check('without internet, deciding says so plainly and changes nothing', async () => {
    await A.go('/#/decisions');
    await A.waitFor(`${card(cfg.offline)}`, 'the decision');
    await A.eval(clickSel(`[data-act=reject][data-ap=${q(cfg.offline)}]`));
    await A.waitFor(`${content}.querySelector('#decisionReason')`, 'the reason box');
    await A.send('Network.enable');
    await A.send('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await A.eval(clearToast);
    await A.eval(clickSel(`[data-act=reject-confirm][data-ap=${q(cfg.offline)}]`));
    const t = await A.waitFor(toast, 'a message');
    await A.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    expect(/can't reach/i.test(t) && /nothing was changed/i.test(t), `message was "${t}"`);
    expect(!/failed to fetch/i.test(t), 'the raw browser error is shown');
  });

  await check('approving in a tab that is out of date says it was already decided', async () => {
    // Tab A's live feed is held back, so it really is out of date when tab B decides.
    await A.send('Fetch.enable', { patterns: [{ urlPattern: '*/api/events/stream*' }] });
    await A.go('/#/decisions');
    await A.send('Page.reload');
    await A.waitFor("document.readyState === 'complete' && !document.querySelector('#app').hidden", 'tab A to reload');
    const B = await newTab(browser, '/#/decisions');
    await A.waitFor(`${card(cfg.stale)}`, 'the decision in tab A');
    await B.waitFor(`${card(cfg.stale)}`, 'the decision in tab B');
    expect(await B.eval(clickSel(`[data-act=approve][data-ap=${q(cfg.stale)}]`)), 'no Approve button in tab B');
    await B.waitFor(toast, 'tab B confirmation');
    await A.send('Page.bringToFront');  // the person switches back to the old tab (hidden tabs do not redraw)
    await A.eval(clearToast);
    expect(await A.eval(clickSel(`[data-act=approve][data-ap=${q(cfg.stale)}]`)), 'no Approve button in tab A');
    const t = await A.waitFor(toast, 'a message in tab A');
    expect(/already/i.test(t), `tab A said "${t}"`);
    await A.waitFor(`!${content}.querySelector('[data-act=approve][data-ap=${q(cfg.stale)}]')`, 'tab A to drop the decided card');
    await A.send('Fetch.disable');
    await browser.send('Target.closeTarget', { targetId: (await B.send('Target.getTargetInfo')).targetInfo.targetId }).catch(() => {});
  });

  await check('signing out in one tab signs the other tab out too', async () => {
    const B = await newTab(browser, '/#/tasks');
    await B.waitFor("!document.querySelector('#app').hidden", 'tab B signed in');
    await A.go('/#/tasks');
    await A.waitFor("!document.querySelector('#app').hidden", 'tab A signed in');
    await B.eval("document.querySelector('#logoutBtn').click()");
    await A.waitFor("!document.querySelector('#login').hidden", 'tab A to show sign-in', 8000);
    // signing in as someone else in tab B: tab A follows and never shows the previous person
    await signIn(B, cfg.manager);
    const who = await A.waitFor("!document.querySelector('#app').hidden && document.querySelector('#userName').textContent", 'tab A to follow', 8000);
    expect(who === cfg.managerName, `tab A shows "${who}"`);
    await browser.send('Target.closeTarget', { targetId: (await B.send('Target.getTargetInfo')).targetInfo.targetId }).catch(() => {});
  });

  await check('when the session ends, the next click shows sign-in instead of hanging', async () => {
    await A.send('Network.enable');
    await A.send('Network.clearBrowserCookies');
    await A.eval(`location.hash = '#/task/${encodeURIComponent(cfg.task)}'`);
    await A.waitFor("!document.querySelector('#login').hidden", 'the sign-in screen', 8000);
    const msg = await A.eval("document.querySelector('#loginErr').textContent");
    expect(/session has ended/i.test(msg), `message was "${msg}"`);
    await signIn(A, cfg.owner);  // and signing in again works in the same tab
  });
} finally {
  await browser.close();
}
const failed = results.filter(([ok]) => !ok).length;
console.log(`${results.length - failed}/${results.length} browser checks passed`);
process.exit(failed ? 1 : 0);
