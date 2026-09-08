/* ==========================================================================
   Pimcamp account onboarding — preview controller
   Plain JavaScript, no dependencies.

   Layout of this file
     1. DOM helpers
     2. Demo settings and fixtures (clearly marked demo data)
     3. Validation (pure functions, reusable server-side)
     4. Service boundary: createDemoService() implements the action surface
        the real onboarding service must provide (see DESIGN.md).
     5. View model (state) and navigation
     6. Rail and screen renderers
     7. Demo panel, theme, init
   ========================================================================== */
(function () {
  'use strict';

  /* ---------------------------------------------------------------------
   * 1. DOM helpers
   * ------------------------------------------------------------------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const tpl = (id) => document.getElementById(id).content.firstElementChild.cloneNode(true);
  const slot = (root, name) => root.querySelector(`[data-slot="${name}"]`);

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of [].concat(children)) {
      if (c === null || c === undefined) continue;
      node.append(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return node;
  }
  const SVG_NS = 'http://www.w3.org/2000/svg';
  function icon(name, cls = 'icon') {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', cls); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }
  const btn = (label, { kind = 'secondary', size = '', iconName = null, ...rest } = {}) =>
    el('button', { type: 'button', class: `btn btn--${kind}${size ? ' btn--' + size : ''}`, ...rest },
      [iconName ? icon(iconName) : null, label]);

  function delay(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) return reject(abortError());
      const t = setTimeout(() => { off(); resolve(); }, ms);
      const onAbort = () => { clearTimeout(t); off(); reject(abortError()); };
      const off = () => signal && signal.removeEventListener('abort', onAbort);
      if (signal) signal.addEventListener('abort', onAbort);
    });
  }
  function abortError() { const e = new Error('Cancelled'); e.name = 'AbortError'; return e; }
  const isAbort = (e) => e && e.name === 'AbortError';

  function announce(text) { const n = $('#live-status'); n.textContent = ''; setTimeout(() => { n.textContent = text; }, 30); }
  function alertNow(text) { const n = $('#live-alert'); n.textContent = ''; setTimeout(() => { n.textContent = text; }, 30); }

  const domainOf = (email) => (email.split('@')[1] || '').toLowerCase();
  const initialOf = (s) => (s || '?').trim().charAt(0) || '?';

  /* ---------------------------------------------------------------------
   * 2. Demo settings and fixtures
   *    Everything here is sample data. The UI labels it as such.
   * ------------------------------------------------------------------- */
  const DEMO_DEFAULTS = () => ({
    latency: 900,
    accountsScenario: 'empty',
    installation: { host: '', remote: false },
    oauthClientConfigured: true,
    googleOutcome: 'authorized',
    outcomes: { incoming: 'pass', outgoing: 'pass', save: 'pass', observe: 'pass' },
  });
  const demo = DEMO_DEFAULTS();

  function fixtureAccounts(scenario) {
    const connected = {
      id: 'demo-connected', demo: true,
      address: 'demo.person@example.com', name: 'Demo mailbox', method: 'imap',
      status: 'connected', note: 'Last checked a few minutes ago.',
      incoming: { host: 'imap.example.com', port: '993', security: 'tls', username: 'demo.person@example.com' },
      outgoing: { host: 'smtp.example.com', port: '465', security: 'tls', sameLogin: true, username: '' },
    };
    const reconnect = {
      id: 'demo-reconnect', demo: true,
      address: 'demo.archive@example.com', name: '', method: 'google',
      status: 'needs-reconnect', note: 'Google no longer accepts the saved permission. Reconnect to approve access again.',
    };
    return { empty: [], connected: [connected], reconnect: [reconnect], both: [connected, reconnect] }[scenario] || [];
  }

  /* ---------------------------------------------------------------------
   * 3. Validation — pure, step-scoped, returns { fieldId: message }
   * ------------------------------------------------------------------- */
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const HOST_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i;

  function validateHost(value) {
    const v = (value || '').trim();
    if (!v) return 'Enter the server name from your provider, for example imap.example.com.';
    if (v.includes('://')) return 'Leave out the “imaps://” or “https://” part. Enter only the server name.';
    if (/\s/.test(v)) return 'Server names can’t contain spaces.';
    if (!HOST_RE.test(v)) return 'Enter a full server name like imap.example.com, without a port or path.';
    return null;
  }
  function validatePort(value, security, kind) {
    const v = (value || '').trim();
    const usual = kind === 'incoming' ? (security === 'tls' ? 993 : 143) : (security === 'tls' ? 465 : 587);
    if (!/^\d+$/.test(v)) return `Enter a port number. ${security === 'tls' ? 'TLS' : 'STARTTLS'} usually uses ${usual}.`;
    const n = Number(v);
    if (n < 1 || n > 65535) return 'Port numbers go from 1 to 65535.';
    return null;
  }

  function validateStep(step, draft, ctx = {}) {
    const errors = {};
    if (step === 'details') {
      const email = draft.email.trim();
      if (!email) errors['f-email'] = 'Enter the email address you want to connect.';
      else if (!EMAIL_RE.test(email)) errors['f-email'] = 'Enter a full address, like alex@example.com.';
      else {
        const existing = (ctx.accounts || []).find(a => a.address.toLowerCase() === email.toLowerCase() && a.id !== draft.reconnectId);
        if (existing) errors['f-email'] = `${existing.address} is already connected. Go back to Accounts to check or reconnect it, or enter a different address.`;
      }
      if (draft.name.length > 80) errors['f-name'] = 'Keep the account name under 80 characters.';
    }
    if (step === 'imap') {
      const i = draft.incoming, o = draft.outgoing;
      const e1 = validateHost(i.host); if (e1) errors['f-in-host'] = e1;
      const e2 = validatePort(i.port, i.security, 'incoming'); if (e2) errors['f-in-port'] = e2;
      if (!i.username.trim()) errors['f-in-user'] = 'Enter the username your provider expects. It’s often your full email address.';
      if (!i.password) errors['f-in-pass'] = 'Enter your password, or an app password if your provider requires one.';
      const e3 = validateHost(o.host); if (e3) errors['f-out-host'] = e3.replace('imap.', 'smtp.');
      const e4 = validatePort(o.port, o.security, 'outgoing'); if (e4) errors['f-out-port'] = e4;
      if (!o.sameLogin) {
        if (!o.username.trim()) errors['f-out-user'] = 'Enter the username for outgoing mail, or tick “Use the same login for outgoing mail”.';
        if (!o.password) errors['f-out-pass'] = 'Enter the password for outgoing mail.';
      }
    }
    return errors;
  }

  /* ---------------------------------------------------------------------
   * 4. Service boundary
   *    The real implementation talks to the onboarding service. Every
   *    method returns plain data; nothing here touches the DOM.
   * ------------------------------------------------------------------- */
  function createDemoService(settings) {
    let accounts = [];
    const lat = (f) => Math.max(0, Number(settings.latency) || 0) * f;

    const api = {
      isDemo: true,
      reset() { accounts = fixtureAccounts(settings.accountsScenario); },

      async listAccounts() { await delay(lat(0.4)); return accounts.map(a => ({ ...a })); },

      async beginSetup() {
        await delay(lat(0.3));
        return {
          setupId: 'demo-setup',
          installation: {
            host: settings.installation.host.trim() || null,
            remote: !!settings.installation.remote,
            oauthClientConfigured: !!settings.oauthClientConfigured,
          },
        };
      },

      validateFields(step, draft, ctx) { return validateStep(step, draft, ctx); },

      /** Resolves to { status: 'authorized'|'cancelled'|'expired'|'denied-policy', identity? } */
      async beginOAuth({ email }, signal) {
        try { await delay(lat(2.6), signal); }
        catch (e) { if (isAbort(e)) return { status: 'cancelled', byUser: true }; throw e; }
        switch (settings.googleOutcome) {
          case 'authorized': return { status: 'authorized', identity: email };
          case 'authorized-different': return { status: 'authorized', identity: email.toLowerCase() === 'different.person@example.com' ? 'other.person@example.com' : 'different.person@example.com' };
          case 'cancelled': return { status: 'cancelled', byUser: false };
          case 'expired': return { status: 'expired' };
          case 'denied-policy': return { status: 'denied-policy' };
          default: return { status: 'cancelled', byUser: false };
        }
      },

      async checkIncoming(draft, signal) {
        await delay(lat(1.4), signal);
        if (draft.method === 'google') return { ok: true, message: 'Google accepted the authorization for reading mail.' };
        const o = settings.outcomes.incoming;
        if (o === 'fail') return { ok: false, code: 'auth', message: 'We couldn’t sign in to incoming mail. Check your username and password.' };
        if (o === 'unreachable') return { ok: false, code: 'unreachable', message: `We couldn’t reach ${draft.incoming.host} on port ${draft.incoming.port}. Check the server name, port, and connection security.` };
        return { ok: true, message: `Signed in to ${draft.incoming.host} as ${draft.incoming.username}.` };
      },

      async checkOutgoing(draft, signal) {
        await delay(lat(1.2), signal);
        if (draft.method === 'google') return { ok: true, message: 'Google accepted the authorization for sending mail. No mail was sent.' };
        if (settings.outcomes.outgoing === 'fail') return { ok: false, code: 'auth', message: 'We couldn’t sign in to outgoing mail. Check the outgoing username and password.' };
        return { ok: true, message: `Signed in to ${draft.outgoing.host}. No mail was sent.` };
      },

      /** Saves account-scoped configuration. Never overwrites another account. */
      async commitAccount(draft, signal) {
        await delay(lat(1.0), signal);
        if (settings.outcomes.save === 'fail') return { ok: false, message: 'The settings couldn’t be saved. Nothing was changed. Try again; if it keeps failing, the installation may be out of disk space or read-only.' };
        const record = {
          id: draft.reconnectId || `preview-${Date.now()}`, demo: false, preview: true,
          address: draft.email.trim(), name: draft.name.trim(), method: draft.method,
          status: 'connected', note: 'Connected just now.',
          incoming: { ...draft.incoming, password: undefined }, outgoing: { ...draft.outgoing, password: undefined },
        };
        const idx = accounts.findIndex(a => a.id === record.id);
        if (idx >= 0) accounts[idx] = { ...accounts[idx], ...record };
        else accounts.push(record);
        return { ok: true, accountId: record.id, message: `Settings saved for ${record.address}.` };
      },

      async observationReadiness(accountId, signal) {
        await delay(lat(0.9), signal);
        const o = settings.outcomes.observe;
        if (o === 'unavailable') return { state: 'unavailable', message: 'Mail watching isn’t installed on this installation yet. Reading and sending mail still work.' };
        if (o === 'fail') return { state: 'failed', message: 'Mail watching couldn’t start for this account. Reading and sending mail still work.' };
        return { state: 'passed', message: 'Pimcamp will notice new mail for this account as it arrives.' };
      },

      async checkAccount(id) {
        await delay(lat(1.3));
        const a = accounts.find(x => x.id === id);
        if (!a) return null;
        if (a.status === 'needs-reconnect') a.note = 'Still can’t sign in. Reconnect to approve access again or enter a new password.';
        else a.note = 'Checked just now. Signed in successfully.';
        return { ...a };
      },
    };
    api.reset();
    return api;
  }

  const service = document.documentElement.dataset.mode === 'live' ? window.PimcampSetupService : createDemoService(demo);
  if (!service) {
    document.querySelector('main').textContent = 'Account setup could not load. Reopen it through the Pimcamp launcher.';
    return;
  }
  if (!service.isDemo) service.validateFields = validateStep;

  /* ---------------------------------------------------------------------
   * 5. View model and navigation
   * ------------------------------------------------------------------- */
  const STEPS = [
    { key: 'choose', label: 'Connection' },
    { key: 'details', label: 'Details' },
    { key: 'settings', label: 'Settings' },
    { key: 'review', label: 'Review' },
    { key: 'connect', label: 'Connect' },
  ];
  const STEP_OF = { choose: 0, details: 1, imap: 2, google: 2, review: 3, connect: 4, done: 5 };

  function newDraft() {
    return {
      method: null, email: '', name: '',
      incoming: { host: '', port: '993', security: 'tls', username: '', password: '', usernameEdited: false },
      outgoing: { host: '', port: '465', security: 'tls', sameLogin: true, username: '', password: '' },
      google: { identity: null },
      presetNote: '',
      reconnectId: null,
    };
  }
  function newChecks() {
    const row = () => ({ state: 'waiting', message: '' });
    return { running: false, cancellable: false, cancelled: false, saved: false, accountId: null, controller: null,
      rows: { incoming: row(), outgoing: row(), save: row(), observe: row() } };
  }

  const state = {
    screen: 'accounts',
    accounts: [],
    accountsLoaded: false,
    installation: { host: null, remote: false, oauthClientConfigured: true },
    draft: newDraft(),
    oauth: { status: 'idle', identity: null, controller: null },
    connect: newChecks(),
    busy: {}, // per-account busy flags
  };

  const stage = $('#stage');
  const rail = $('#rail');

  function go(screen, { focus = true } = {}) {
    if (state.oauth.controller) { state.oauth.controller.abort(); state.oauth.controller = null; if (state.oauth.status === 'in-progress') state.oauth.status = 'idle'; }
    state.screen = screen;
    render({ focus });
  }

  function backTarget() {
    const d = state.draft;
    switch (state.screen) {
      case 'choose': return 'accounts';
      case 'details': return d.reconnectId ? 'accounts' : 'choose';
      case 'imap': case 'google': return d.reconnectId ? 'accounts' : 'details';
      case 'review': return d.method === 'google' ? 'google' : 'imap';
      case 'connect': return 'review';
      default: return 'accounts';
    }
  }
  function back() { if (state.connect.running) return; go(backTarget()); }

  async function startAdd() {
    state.draft = newDraft();
    state.oauth = { status: 'idle', identity: null, controller: null };
    state.connect = newChecks();
    renderLoading('Preparing setup…');
    const setup = await service.beginSetup().catch(error => { showServiceError(error); return null; });
    if (!setup) return;
    state.installation = setup.installation;
    go('choose');
  }

  async function startReconnect(account) {
    const d = newDraft();
    d.reconnectId = account.id; d.method = account.method; d.email = account.address; d.name = account.name || '';
    if (account.incoming) Object.assign(d.incoming, account.incoming, { password: '', usernameEdited: true });
    if (account.outgoing) Object.assign(d.outgoing, account.outgoing, { password: '' });
    state.draft = d;
    state.oauth = { status: 'idle', identity: null, controller: null };
    state.connect = newChecks();
    renderLoading(`Preparing to reconnect ${account.address}…`);
    const setup = await service.beginSetup(account.id).catch(error => { showServiceError(error); return null; });
    if (!setup) return;
    state.installation = setup.installation;
    go(d.method === 'google' ? 'google' : 'imap');
  }

  /* ---------------------------------------------------------------------
   * 6. Renderers
   * ------------------------------------------------------------------- */
  function render(opts = {}) {
    const { focus = true } = opts;
    renderRail();
    stage.replaceChildren();
    const result = SCREENS[state.screen](opts);
    if (result && typeof result.then === 'function') return; // async renderer finishes on its own
    afterRender(focus);
  }
  function afterRender(focus) {
    document.title = `${titleFor()} · Pimcamp${service.isDemo ? ' (preview)' : ''}`;
    if (focus) focusTitle();
  }
  function titleFor() {
    const h = $('.screen__title', stage);
    return h ? h.textContent.trim() : 'Pimcamp';
  }
  function focusTitle() {
    const h = $('.screen__title', stage);
    if (h) { h.focus({ preventScroll: false }); }
    const step = STEP_OF[state.screen];
    if (typeof step === 'number' && step < STEPS.length) announce(`${titleFor()}. Step ${step + 1} of ${STEPS.length}.`);
    else announce(titleFor());
  }
  function renderLoading(text) {
    stage.replaceChildren();
    const n = tpl('tpl-loading');
    slot(n, 'text').textContent = text;
    stage.append(n);
  }

  function showServiceError(error) {
    const message = error.publicMessage || 'Could not reach account setup. Check that it is still running, then retry.';
    stage.replaceChildren(el('h1', {class: 'screen__title', tabindex: '-1', text: 'Setup needs attention'}),
      el('p', {class: 'screen__lede', text: message}),
      btn('Try again', {onclick: () => { state.accountsLoaded = false; go('accounts'); }}));
    focusTitle(); alertNow(message);
  }

  function renderRail() {
    rail.replaceChildren();
    if (state.screen === 'accounts') { rail.append(tpl('tpl-rail-intro')); return; }
    const n = tpl('tpl-rail-steps');
    const current = STEP_OF[state.screen];
    slot(n, 'eyebrow').textContent = state.draft.reconnectId ? 'Reconnect account' : 'Add account';
    const list = slot(n, 'steps');
    STEPS.forEach((s, i) => {
      const st = i < current ? 'done' : i === current ? 'current' : 'todo';
      const li = el('li', { class: 'step', 'data-state': st, 'aria-current': st === 'current' ? 'step' : null }, [
        el('span', { class: 'step__marker', 'aria-hidden': 'true' }, st === 'done' ? [icon('check')] : [String(i + 1)]),
        el('span', { class: 'step__label' }, [s.label, el('span', { class: 'visually-hidden', text: st === 'done' ? ' (completed)' : st === 'current' ? ' (current step)' : '' })]),
      ]);
      list.append(li);
    });
    if (state.draft.email) { const a = slot(n, 'account'); a.hidden = false; a.textContent = state.draft.email; }
    rail.append(n);
  }

  /* ---------- Accounts ---------- */
  async function renderAccounts({ focus = true } = {}) {
    const n = tpl('tpl-accounts');
    if (!state.accountsLoaded) {
      renderLoading('Loading accounts…');
      const accounts = await service.listAccounts().catch(error => { showServiceError(error); return null; });
      if (!accounts) return;
      state.accounts = accounts;
      state.accountsLoaded = true;
      if (state.screen !== 'accounts') return;
      stage.replaceChildren();
    }
    const has = state.accounts.length > 0;
    slot(n, 'empty').hidden = has;
    slot(n, 'list').hidden = !has;
    const list = slot(n, 'accounts');
    for (const a of state.accounts) list.append(accountRow(a));
    stage.append(n);
    afterRender(focus);
  }

  function accountRow(a) {
    const r = tpl('tpl-account-row');
    r.dataset.id = a.id;
    slot(r, 'initial').textContent = initialOf(a.name || a.address);
    slot(r, 'address').textContent = a.address;
    const tag = slot(r, 'tag');
    if (a.demo) { tag.hidden = false; tag.textContent = 'Demo'; tag.classList.add('tag--demo'); }
    else if (a.preview) { tag.hidden = false; tag.textContent = 'Preview only'; tag.classList.add('tag--demo'); }
    slot(r, 'name').textContent = a.name ? `${a.name} · ${a.method === 'google' ? 'Google' : 'IMAP & SMTP'}` : (a.method === 'google' ? 'Google' : 'IMAP & SMTP');
    const status = slot(r, 'status');
    const busy = state.busy[a.id];
    const map = { connected: ['success', 'Connected'], 'needs-reconnect': ['danger', 'Needs reconnect'], unknown: ['', 'Not checked'] };
    const [tone, label] = busy ? ['accent', 'Checking…'] : (map[a.status] || map.unknown);
    status.dataset.tone = tone; status.textContent = label;
    if (a.note) { const note = slot(r, 'note'); note.hidden = false; note.textContent = a.note; }
    const checkBtn = $('[data-action="check-account"]', r);
    const reconnectBtn = $('[data-action="reconnect-account"]', r);
    if (busy) { checkBtn.disabled = true; checkBtn.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Checking…'); }
    if (a.status === 'needs-reconnect') { reconnectBtn.className = 'btn btn--primary btn--small'; }
    checkBtn.addEventListener('click', () => checkAccount(a.id));
    reconnectBtn.addEventListener('click', () => startReconnect(a));
    return r;
  }

  async function checkAccount(id) {
    if (state.busy[id]) return;
    state.busy[id] = true;
    const a = state.accounts.find(x => x.id === id);
    announce(`Checking connection for ${a.address}.`);
    refreshAccountRow(id);
    try {
      const updated = await service.checkAccount(id);
      if (updated) Object.assign(a, updated);
    } catch (error) {
      a.status = 'unknown';
      a.note = error.publicMessage || 'The connection check could not finish. Try again.';
    } finally {
      state.busy[id] = false;
      refreshAccountRow(id);
    }
    announce(`${a.address}: ${a.status === 'connected' ? 'connected' : 'needs reconnect'}. ${a.note || ''}`);
  }
  function refreshAccountRow(id) {
    const old = $(`.account[data-id="${CSS.escape(id)}"]`, stage);
    const a = state.accounts.find(x => x.id === id);
    if (!old || !a) return;
    const active = document.activeElement;
    const activeAction = active && old.contains(active) ? active.dataset.action : null;
    const fresh = accountRow(a);
    old.replaceWith(fresh);
    if (activeAction) { const again = $(`[data-action="${activeAction}"]`, fresh); if (again) again.focus(); }
  }

  /* ---------- Choose connection ---------- */
  function renderChoose() {
    const n = tpl('tpl-choose');
    $$('[data-action="choose-method"]', n).forEach(b => {
      if (b.dataset.method === state.draft.method) b.setAttribute('aria-current', 'true');
      b.addEventListener('click', () => {
        const m = b.dataset.method;
        if (m !== state.draft.method) { state.draft.google.identity = null; state.oauth.status = 'idle'; }
        state.draft.method = m;
        go('details');
      });
    });
    $('[data-action="back"]', n).addEventListener('click', back);
    stage.append(n);
  }

  /* ---------- Account details ---------- */
  function renderDetails() {
    const n = tpl('tpl-details');
    const d = state.draft;
    $('#f-email', n).value = d.email;
    $('#f-name', n).value = d.name;
    n.addEventListener('input', () => {
      d.email = $('#f-email', n).value;
      d.name = $('#f-name', n).value;
    });
    bindClearError(n);
    $('[data-action="back"]', n).addEventListener('click', back);
    n.addEventListener('submit', (e) => {
      e.preventDefault();
      d.email = $('#f-email', n).value.trim();
      d.name = $('#f-name', n).value;
      const errors = service.validateFields('details', d, { accounts: state.accounts });
      if (applyErrors(n, errors)) return;
      if (d.google.identity && d.google.identity.toLowerCase() !== d.email.toLowerCase()) { d.google.identity = null; state.oauth.status = 'idle'; }
      if (!d.incoming.usernameEdited) d.incoming.username = d.email;
      go(d.method === 'google' ? 'google' : 'imap');
    });
    stage.append(n);
  }

  /* ---------- IMAP & SMTP ---------- */
  const DEFAULT_PORT = { incoming: { tls: '993', starttls: '143' }, outgoing: { tls: '465', starttls: '587' } };

  function renderImap() {
    const n = tpl('tpl-imap');
    const d = state.draft;
    if (!d.incoming.usernameEdited && !d.incoming.username) d.incoming.username = d.email;

    const f = (id) => $('#' + id, n);
    f('f-in-host').value = d.incoming.host; f('f-in-port').value = d.incoming.port; f('f-in-security').value = d.incoming.security;
    f('f-in-user').value = d.incoming.username; f('f-in-pass').value = d.incoming.password;
    f('f-out-host').value = d.outgoing.host; f('f-out-port').value = d.outgoing.port; f('f-out-security').value = d.outgoing.security;
    f('f-same-login').checked = d.outgoing.sameLogin; f('f-out-user').value = d.outgoing.username; f('f-out-pass').value = d.outgoing.password;
    const outLogin = slot(n, 'outgoing-login');
    outLogin.hidden = d.outgoing.sameLogin;
    const presetNote = slot(n, 'preset-note');
    if (d.presetNote) { presetNote.hidden = false; presetNote.textContent = d.presetNote; }

    const sync = () => {
      d.incoming = { ...d.incoming, host: f('f-in-host').value.trim(), port: f('f-in-port').value.trim(), security: f('f-in-security').value, username: f('f-in-user').value, password: f('f-in-pass').value };
      d.outgoing = { ...d.outgoing, host: f('f-out-host').value.trim(), port: f('f-out-port').value.trim(), security: f('f-out-security').value, sameLogin: f('f-same-login').checked, username: f('f-out-user').value, password: f('f-out-pass').value };
    };
    n.addEventListener('input', sync);
    n.addEventListener('change', sync);
    f('f-in-user').addEventListener('input', () => { d.incoming.usernameEdited = true; });

    // Security ↔ port: only move the port when it still holds the previous default.
    const portFollow = (kind, secId, portId) => {
      let prev = f(secId).value;
      f(secId).addEventListener('change', () => {
        const next = f(secId).value;
        if (f(portId).value.trim() === DEFAULT_PORT[kind][prev]) f(portId).value = DEFAULT_PORT[kind][next];
        prev = next; sync();
      });
    };
    portFollow('incoming', 'f-in-security', 'f-in-port');
    portFollow('outgoing', 'f-out-security', 'f-out-port');

    f('f-same-login').addEventListener('change', () => {
      outLogin.hidden = f('f-same-login').checked;
      if (!f('f-same-login').checked && !f('f-out-user').value) { f('f-out-user').value = f('f-in-user').value; sync(); }
      if (!f('f-same-login').checked) f('f-out-user').focus();
    });

    $('[data-action="apply-preset"]', n).addEventListener('click', () => {
      const preset = f('f-preset').value;
      const dom = domainOf(d.email);
      let note = '';
      if (preset === 'domain') {
        f('f-in-host').value = `imap.${dom}`; f('f-out-host').value = `smtp.${dom}`;
        f('f-in-security').value = 'tls'; f('f-in-port').value = '993'; f('f-out-security').value = 'tls'; f('f-out-port').value = '465';
        note = `Filled imap.${dom} and smtp.${dom} with TLS. This is a guess based on your address; check your provider’s settings if sign-in fails.`;
      } else if (preset === 'namecheap') {
        f('f-in-host').value = 'mail.privateemail.com'; f('f-out-host').value = 'mail.privateemail.com';
        f('f-in-security').value = 'tls'; f('f-in-port').value = '993'; f('f-out-security').value = 'tls'; f('f-out-port').value = '465';
        if (!d.incoming.usernameEdited) f('f-in-user').value = d.email;
        note = 'Filled Namecheap Private Email values. Your username is your full email address. Everything stays editable.';
      } else {
        note = 'Choose a preset first.';
      }
      presetNote.hidden = false; presetNote.textContent = note;
      d.presetNote = preset ? note : '';
      sync();
      announce(note);
      if (preset) { clearErrors(n); f('f-in-pass').focus(); }
      else f('f-preset').focus();
    });

    $$('[data-action="toggle-password"]', n).forEach(b => b.addEventListener('click', () => togglePassword(b, n)));
    bindClearError(n);
    $('[data-action="back"]', n).addEventListener('click', back);
    n.addEventListener('submit', (e) => {
      e.preventDefault();
      sync();
      const errors = service.validateFields('imap', d, {});
      if (applyErrors(n, errors)) return;
      go('review');
    });
    stage.append(n);
  }

  function togglePassword(button, root) {
    const input = $('#' + button.dataset.target, root);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.setAttribute('aria-pressed', String(show));
    const base = button.getAttribute('aria-label').replace(/^(Show|Hide)/, '');
    button.setAttribute('aria-label', (show ? 'Hide' : 'Show') + base);
    button.replaceChildren(icon(show ? 'eye-off' : 'eye'));
    input.focus();
  }

  /* ---------- Google ---------- */
  function renderGoogle() {
    const n = tpl('tpl-google');
    const d = state.draft;
    if (!state.installation.oauthClientConfigured) state.oauth.status = 'missing-client';
    else if (state.oauth.status === 'missing-client') state.oauth.status = 'idle';
    if (state.oauth.status === 'idle' && d.google.identity) { state.oauth.status = 'authorized'; state.oauth.identity = d.google.identity; }

    const panel = slot(n, 'state');
    const actions = slot(n, 'primary-actions');
    const admin = slot(n, 'admin');
    const email = d.email;
    const s = state.oauth;

    const head = (tone, iconName, title, text) => el('div', { class: 'panel__head' }, [
      el('span', { class: 'panel__icon', 'data-tone': tone, 'aria-hidden': 'true' }, [iconName === 'spinner' ? el('span', { class: 'spinner' }) : icon(iconName, 'icon icon--lg')]),
      el('div', {}, [el('p', { class: 'panel__title', text: title }), el('p', { class: 'panel__text', text })]),
    ]);
    const identityCard = (addr, hint) => el('div', { class: 'identity' }, [
      el('span', { class: 'identity__avatar', 'aria-hidden': 'true', text: initialOf(addr) }),
      el('div', { class: 'identity__body' }, [el('span', { class: 'identity__address', text: addr }), el('span', { class: 'identity__hint', text: hint })]),
    ]);
    const primary = (label, onclick, iconName) => btn(label, { kind: 'primary', iconName, onclick });
    const secondary = (label, onclick) => btn(label, { kind: 'secondary', onclick });
    const useImap = () => btn('Use IMAP & SMTP instead', { kind: 'ghost', onclick: () => { d.method = 'imap'; if (!d.incoming.usernameEdited) d.incoming.username = d.email; go('imap'); } });

    switch (s.status) {
      case 'idle':
        panel.append(head('accent', 'google', 'Ready to continue with Google',
          `A Google sign-in page opens in your browser. Choose ${email} there and approve access for Pimcamp. When you come back, Pimcamp confirms which account Google authorized.`));
        panel.append(el('p', { class: 'panel__text', text: 'Google grants full mail access for this connection, including reading, sending, changing, and deleting mail. Pimcamp only performs mail actions you authorize.' }));
        actions.append(primary('Continue with Google', beginOAuth, 'external'));
        break;
      case 'in-progress':
        panel.append(head('accent', 'spinner', 'Waiting for Google', 'Finish signing in and approving access in the Google window. This page updates on its own when you’re done. If you closed that window, choose Cancel here to start again.'));
        panel.append(el('p', { class: 'panel__text', text: 'Nothing has been saved yet. You can cancel and nothing changes.' }));
        actions.append(secondary('Cancel', cancelOAuth));
        break;
      case 'cancelled':
        panel.append(head('warning', 'alert', 'Google sign-in was cancelled', s.byUser ? 'You cancelled before Google finished. Nothing was changed.' : 'The Google window was closed or access was declined before it finished. Nothing was changed.'));
        actions.append(primary('Try again', beginOAuth, 'refresh'));
        break;
      case 'failed':
        panel.append(head('danger', 'alert', 'Google sign-in could not finish',
          s.message || 'Try again or check the installation’s Google authorization settings. Nothing was saved.'));
        actions.append(primary('Try again', beginOAuth, 'refresh'));
        break;
      case 'expired':
        panel.append(head('warning', 'alert', 'The sign-in request expired', 'Google sign-in requests only stay valid for a few minutes. Start again and finish the approval in one go.'));
        actions.append(primary('Start again', beginOAuth, 'refresh'));
        break;
      case 'denied-policy':
        panel.append(head('danger', 'x', 'Your organization doesn’t allow this app',
          `Google reported that the administrator for ${domainOf(email) || 'this domain'} hasn’t approved Pimcamp for ${email}. Ask your Google Workspace administrator to allow it, then try again. If your organization permits app passwords, IMAP & SMTP is an alternative.`));
        actions.append(useImap(), primary('Try again', beginOAuth, 'refresh'));
        break;
      case 'missing-client':
        panel.append(head('warning', 'lock', 'Google sign-in isn’t set up on this installation yet',
          'Someone who administers this installation needs to register Pimcamp with Google once. Until then, Google accounts can’t be connected here. If your provider allows app passwords, IMAP & SMTP works now.'));
        actions.append(useImap(), secondary('Check again', async () => {
          renderLoading('Checking installation…');
          try {
            const setup = await service.beginSetup();
            state.installation = setup.installation;
            go('google');
          } catch (error) { showServiceError(error); }
        }));
        admin.hidden = false;
        slot(admin, 'callback').textContent = callbackUrl();
        break;
      case 'authorized': {
        const match = s.identity.toLowerCase() === email.toLowerCase();
        if (match) {
          panel.append(head('success', 'check', 'Google authorized this account', 'Google confirmed access for the address you entered. Continue to review the settings.'));
          panel.append(identityCard(s.identity, 'Authorized by Google'));
          d.google.identity = s.identity;
          actions.append(primary('Continue', () => go('review'), 'arrow-right'));
        } else {
          panel.append(head('warning', 'alert', 'Google authorized a different account', `You entered ${email}, but Google signed in as ${s.identity}. Pimcamp won’t guess; choose which one to connect.`));
          panel.append(identityCard(s.identity, 'Authorized by Google'));
          actions.append(
            secondary(`Try again with ${email}`, beginOAuth),
            primary(`Connect ${s.identity} instead`, () => {
              const dup = state.accounts.find(a => a.address.toLowerCase() === s.identity.toLowerCase() && a.id !== d.reconnectId);
              if (dup) { alertNow(`${s.identity} is already connected. Go back to Accounts to check or reconnect it.`); showNotice(n, 'danger', `${s.identity} is already connected.`, 'Go back to Accounts to check or reconnect it, or sign in to Google with a different account.'); return; }
              d.email = s.identity; d.google.identity = s.identity; go('review');
            }, 'arrow-right'),
          );
        }
        break;
      }
    }
    $('[data-action="back"]', n).addEventListener('click', back);
    stage.append(n);
  }

  function showNotice(root, tone, title, text) {
    const existing = $('.notice--inline', root);
    if (existing) existing.remove();
    const nt = el('div', { class: `notice notice--inline notice--${tone}`, role: 'status' }, [icon(tone === 'danger' ? 'alert' : 'info'), el('div', { class: 'notice__body' }, [el('p', { class: 'notice__title', text: title }), el('p', { text })])]);
    slot(root, 'state').after(nt);
  }

  function callbackUrl() {
    if (!service.isDemo) return state.installation.oauthCallback || 'Callback not supplied by this installation';
    const origin = (location.origin && location.origin !== 'null') ? location.origin : 'http://<setup-host>:<setup-port>';
    return `${origin}/oauth/google/callback`;
  }

  async function beginOAuth() {
    if (state.oauth.status === 'in-progress') return;
    const controller = new AbortController();
    state.oauth = { status: 'in-progress', identity: null, controller };
    render();
    announce('Waiting for Google. Finish approving access in the Google window.');
    let result;
    try {
      result = await service.beginOAuth({ email: state.draft.email }, controller.signal);
    } catch (error) {
      if (state.oauth.controller !== controller) return;
      state.oauth = { status: 'failed', identity: null, controller: null,
        message: error.publicMessage || 'Try again or check the installation’s Google authorization settings.' };
      render();
      alertNow(state.oauth.message);
      return;
    }
    if (state.oauth.controller !== controller) return; // navigated away or cancelled; ignore stale result
    state.oauth = { status: result.status, identity: result.identity || null, byUser: !!result.byUser, controller: null };
    if (state.screen === 'google') {
      render();
      if (result.status === 'authorized') announce(`Google authorized ${result.identity}.`);
      else alertNow($('.panel__title', stage).textContent + '. ' + $('.panel__text', stage).textContent);
    }
  }
  function cancelOAuth() {
    const c = state.oauth.controller;
    if (c) { state.oauth.controller = null; c.abort(); }
    state.oauth = { status: 'cancelled', identity: null, byUser: true, controller: null };
    render();
  }

  /* ---------- Review ---------- */
  function renderReview() {
    const n = tpl('tpl-review');
    const d = state.draft;
    const dl = slot(n, 'summary');
    const sec = (t) => { const r = tpl('tpl-summary-row'); r.classList.add('summary__row--section'); slot(r, 'term').textContent = t; slot(r, 'value').textContent = ''; dl.append(r); };
    const row = (t, v, small) => { const r = tpl('tpl-summary-row'); slot(r, 'term').textContent = t; const dd = slot(r, 'value'); dd.textContent = v; if (small) dd.append(el('small', { text: small })); dl.append(r); };
    const secLabel = (s) => s === 'tls' ? 'TLS' : 'STARTTLS';

    sec('Account');
    row('Email address', d.email);
    row('Account name', d.name.trim() || 'Not set', d.name.trim() ? 'Only shown inside Pimcamp.' : 'Pimcamp shows the address instead.');
    row('Sign-in method', d.method === 'google' ? 'Google' : 'IMAP & SMTP with a password');
    if (d.method === 'google') {
      row('Authorized by Google', d.google.identity || d.email, 'Pimcamp stores the permission Google granted, not a password.');
      sec('Incoming mail');
      row('Server', 'imap.gmail.com · port 993 · TLS', 'Using the Google authorization.');
      sec('Outgoing mail');
      row('Server', 'smtp.gmail.com · port 465 · TLS', 'Using the Google authorization.');
    } else {
      sec('Incoming mail');
      row('Server', `${d.incoming.host} · port ${d.incoming.port} · ${secLabel(d.incoming.security)}`);
      row('Username', d.incoming.username);
      sec('Outgoing mail');
      row('Server', `${d.outgoing.host} · port ${d.outgoing.port} · ${secLabel(d.outgoing.security)}`);
      row('Login', d.outgoing.sameLogin ? 'Same as incoming' : d.outgoing.username);
    }
    if (state.installation.remote) {
      sec('Installation');
      row('Saved on', state.installation.host || 'the installation you are connected to', 'This is a remote installation. Settings are saved there, not on the computer running this browser.');
    }
    if (state.installation.credentialStorage === 'session') {
      sec('Credential storage');
      row('Temporary login', 'This installation uses a volatile keyring. You may need to reconnect after logout or reboot.');
    }
    if (d.reconnectId) { sec('Reconnect'); row('Existing account', 'Will be updated in place', 'Nothing is replaced until the checks pass.'); }

    $('[data-action="back"]', n).addEventListener('click', back);
    $('[data-action="connect"]', n).addEventListener('click', () => { state.connect = newChecks(); go('connect'); runConnect(); });
    stage.append(n);
  }

  /* ---------- Connect (checks) ---------- */
  const CHECK_ROWS = [
    { key: 'incoming', label: 'Incoming mail sign-in' },
    { key: 'outgoing', label: 'Outgoing mail sign-in' },
    { key: 'save', label: 'Save settings' },
    { key: 'observe', label: 'Mail watching' },
  ];
  const STATE_LABEL = { waiting: ['', 'Waiting'], checking: ['accent', 'Checking…'], passed: ['success', 'Passed'], failed: ['danger', 'Failed'], unavailable: ['warning', 'Unavailable'] };

  function renderConnect() {
    const n = tpl('tpl-connect');
    slot(n, 'demo-notice').hidden = !service.isDemo;
    const list = slot(n, 'checks');
    for (const def of CHECK_ROWS) list.append(checkRow(def, state.connect.rows[def.key]));
    stage.append(n);
    refreshConnectChrome();
  }

  /** Updates title, lede, row actions, and buttons in place so focus and scroll position survive. */
  function refreshConnectChrome() {
    if (state.screen !== 'connect') return;
    const n = $('[data-screen="connect"]', stage);
    if (!n) return;
    const c = state.connect;
    const d = state.draft;
    const failedAuth = ['incoming', 'outgoing'].some(k => c.rows[k].state === 'failed');
    const saveFailed = c.rows.save.state === 'failed';

    let title, lede;
    if (c.running) { title = `Connecting ${d.email}`; lede = c.cancellable ? 'Signing in to check your settings. Nothing is saved until both sign-ins pass.' : 'Saving settings and checking mail watching. This only takes a moment.'; }
    else if (c.cancelled) { title = 'Checks cancelled'; lede = 'Nothing was saved. Your settings are kept, so you can run the checks again whenever you like.'; }
    else if (c.saved) { title = 'Settings saved'; lede = 'The account is connected. Review the results, then continue.'; }
    else if (failedAuth) { title = 'Pimcamp couldn’t connect this account yet'; lede = 'Nothing was saved and your settings are kept. Fix the failed step and try again; steps that passed won’t be repeated.'; }
    else if (saveFailed) { title = 'Saving could not be confirmed'; lede = 'The sign-ins passed, but setup could not confirm the completed save. Try saving again; the same attempt will not create a duplicate account.'; }
    else { title = `Connecting ${d.email}`; lede = 'Starting checks…'; }
    slot(n, 'title').textContent = title;
    slot(n, 'lede').textContent = lede;
    document.title = `${title} · Pimcamp${service.isDemo ? ' (preview)' : ''}`;

    // Row-level repair actions depend on whether checks are running.
    for (const def of CHECK_ROWS) {
      const old = $(`.check-row[data-key="${def.key}"]`, n);
      if (old) old.replaceWith(checkRow(def, c.rows[def.key]));
    }

    const actions = slot(n, 'actions');
    actions.replaceChildren();
    if (c.running) {
      if (c.cancellable) actions.append(btn('Cancel', { kind: 'secondary', onclick: cancelConnect }));
      else actions.append(el('span', { class: 'help' }, [icon('lock'), 'Saving can’t be cancelled, but it only takes a moment.']));
    } else if (c.saved) {
      actions.append(btn('Continue', { kind: 'primary', iconName: 'arrow-right', onclick: () => go('done') }));
    } else if (c.cancelled) {
      actions.append(btn('Back', { kind: 'ghost', iconName: 'arrow-left', onclick: back }), btn('Run checks again', { kind: 'primary', iconName: 'refresh', onclick: () => runConnect() }));
    } else if (failedAuth) {
      actions.append(btn('Back', { kind: 'ghost', iconName: 'arrow-left', onclick: back }), btn('Edit settings', { kind: 'secondary', onclick: () => go(d.method === 'google' ? 'google' : 'imap') }), btn('Retry failed checks', { kind: 'primary', iconName: 'refresh', onclick: () => runConnect({ retry: true }) }));
    } else if (saveFailed) {
      actions.append(btn('Back', { kind: 'ghost', iconName: 'arrow-left', onclick: back }), btn('Try saving again', { kind: 'primary', iconName: 'refresh', onclick: () => runConnect({ retry: true }) }));
    }
  }

  function checkRow(def, r) {
    const row = tpl('tpl-check-row');
    row.dataset.key = def.key;
    row.dataset.state = r.state;
    slot(row, 'label').textContent = def.label;
    const [tone, label] = STATE_LABEL[r.state];
    const st = slot(row, 'state'); st.dataset.tone = tone; st.textContent = label;
    slot(row, 'message').textContent = r.message || (r.state === 'waiting' ? waitingText(def.key) : '');
    const acts = slot(row, 'actions');
    const d = state.draft;
    if (!state.connect.running) {
      if (r.state === 'failed' && (def.key === 'incoming' || def.key === 'outgoing')) {
        acts.hidden = false;
        if (d.method === 'google') acts.append(btn('Sign in with Google again', { size: 'small', onclick: () => { state.oauth.status = 'idle'; d.google.identity = null; go('google'); } }));
        else acts.append(btn(def.key === 'incoming' ? 'Edit incoming settings' : 'Edit outgoing settings', { size: 'small', onclick: () => { go('imap'); const target = def.key === 'incoming' ? '#f-in-pass' : (d.outgoing.sameLogin ? '#f-in-pass' : '#f-out-pass'); const f = $(target, stage); if (f) f.focus(); } }));
      }
      if (r.state === 'failed' && def.key === 'observe' && state.connect.saved) {
        acts.hidden = false; acts.append(btn('Try again', { size: 'small', iconName: 'refresh', onclick: () => runConnect({ retry: true }) }));
      }
    }
    return row;
  }
  function waitingText(key) {
    return { incoming: 'Signs in to the incoming server. Nothing is downloaded.', outgoing: 'Signs in to the outgoing server. No mail is sent.', save: 'Writes the settings for this account only.', observe: 'Checks whether Pimcamp can notice new mail for this account.' }[key];
  }
  function updateRow(key, st, message) {
    state.connect.rows[key] = { state: st, message: message || '' };
    const def = CHECK_ROWS.find(r => r.key === key);
    const old = $(`.check-row[data-key="${key}"]`, stage);
    if (old) old.replaceWith(checkRow(def, state.connect.rows[key]));
    if (st !== 'checking') announce(`${def.label}: ${STATE_LABEL[st][1]}. ${message || ''}`);
  }

  async function runConnect({ retry = false } = {}) {
    const c = state.connect;
    if (c.running) return;
    const d = state.draft;
    const controller = new AbortController();
    c.controller = controller; c.running = true; c.cancelled = false;
    const todo = (k) => !retry || c.rows[k].state !== 'passed';
    for (const k of Object.keys(c.rows)) if (todo(k) && c.rows[k].state !== 'passed') c.rows[k] = { state: 'waiting', message: '' };
    c.cancellable = !c.saved;
    refreshConnectChrome();
    announce(retry ? 'Retrying failed steps.' : `Connecting ${d.email}. Checking incoming mail sign-in.`);

    try {
      if (!c.saved) {
        for (const k of ['incoming', 'outgoing']) {
          if (!todo(k)) continue;
          updateRow(k, 'checking', k === 'incoming' ? 'Signing in to incoming mail…' : 'Signing in to outgoing mail…');
          const r = await (k === 'incoming' ? service.checkIncoming(d, controller.signal) : service.checkOutgoing(d, controller.signal));
          updateRow(k, r.ok ? 'passed' : 'failed', r.message);
        }
        if (c.rows.incoming.state !== 'passed' || c.rows.outgoing.state !== 'passed') {
          finishConnect(); alertNow('A sign-in check failed. Nothing was saved.'); return;
        }
        c.cancellable = false; refreshConnectChrome();
        updateRow('save', 'checking', 'Saving settings…');
        const s = await service.commitAccount(d, null);
        if (!s.ok) { updateRow('save', 'failed', s.message); finishConnect(); alertNow('Settings couldn’t be saved. Nothing was changed.'); return; }
        c.saved = true; c.accountId = s.accountId;
        d.incoming.password = ''; d.outgoing.password = '';
        state.accountsLoaded = false; // list will be reloaded from the service
        updateRow('save', 'passed', s.message);
      }
      if (todo('observe')) {
        updateRow('observe', 'checking', 'Checking mail watching…');
        const o = await service.observationReadiness(c.accountId, null);
        updateRow('observe', o.state, o.message);
      }
      finishConnect();
      announce(c.rows.observe.state === 'passed' ? 'All steps passed. Choose Continue.' : 'Settings saved. Mail watching is not ready; choose Continue to see what’s left.');
      const cont = $('.btn--primary', stage); if (cont) cont.focus();
    } catch (e) {
      if (isAbort(e)) {
        for (const k of Object.keys(c.rows)) if (c.rows[k].state === 'checking') c.rows[k] = { state: 'waiting', message: '' };
        c.cancelled = true;
        finishConnect();
        announce('Checks cancelled. Nothing was saved.');
        return;
      }
      // Unexpected failure: never leave the screen stuck. Mark the in-flight step failed with a plain message.
      for (const k of Object.keys(c.rows)) if (c.rows[k].state === 'checking') c.rows[k] = { state: 'failed', message: e.publicMessage || 'This step could not finish. Check the connection and try again.' };
      finishConnect();
      alertNow('A step failed unexpectedly. You can try again.');
    }
  }
  function finishConnect() { const c = state.connect; c.running = false; c.controller = null; c.cancellable = false; refreshConnectChrome(); }
  function cancelConnect() { const c = state.connect; if (c.running && c.cancellable && c.controller) c.controller.abort(); }

  /* ---------- Done ---------- */
  function renderDone() {
    const n = tpl('tpl-done');
    const d = state.draft;
    const observe = state.connect.rows.observe;
    const partial = observe.state !== 'passed';
    const where = state.installation.remote ? (state.installation.host || 'the remote installation') : 'this installation';
    slot(n, 'title').textContent = partial ? 'Account connected, with one thing left' : 'Account connected';
    slot(n, 'lede').textContent = `${d.email} is ready. Pimcamp saved the settings on ${where}${d.reconnectId ? ' and updated the existing account' : ''}.`;
    if (partial) slot(n, 'art').dataset.tone = 'warning';

    const caps = slot(n, 'caps');
    const cap = (label, ready, stateText, desc) => {
      const r = tpl('tpl-cap-row'); r.dataset.state = ready ? 'ready' : 'unavailable';
      slot(r, 'label').textContent = label;
      const st = slot(r, 'state'); st.dataset.tone = ready ? 'success' : 'warning'; st.textContent = stateText;
      slot(r, 'desc').textContent = desc; caps.append(r);
    };
    cap('Read mail', true, 'Ready', 'Pimcamp can list and open messages in this mailbox.');
    cap('Send mail', true, 'Ready', 'Pimcamp can send on your behalf. It only sends when you ask it to.');
    cap('Notice new mail', !partial, partial ? 'Not yet' : 'Ready', partial ? observe.message : 'Pimcamp reacts to new messages as they arrive.');

    if (partial) {
      const rem = slot(n, 'remaining'); rem.hidden = false;
      const body = el('div', { class: 'notice__body' }, [el('p', { class: 'notice__title', text: observe.state === 'unavailable' ? 'New-mail notifications are not verified yet' : 'Mail watching didn’t start' }),
        el('p', { text: observe.message || 'Reading and sending have separate connection checks. Check mail watching before relying on new-mail notifications.' })]);
      const act = el('div', { class: 'check-row__actions' });
      if (observe.state === 'failed') act.append(btn('Try again', { size: 'small', iconName: 'refresh', onclick: async () => { go('connect', { focus: false }); await runConnect({ retry: true }); } }));
      else act.append(btn('Check again', { size: 'small', iconName: 'refresh', onclick: async () => { go('connect', { focus: false }); state.connect.rows.observe = { state: 'waiting', message: '' }; await runConnect({ retry: true }); } }));
      body.append(act);
      rem.append(icon('alert'), body);
    }
    $('[data-action="go-accounts"]', n).addEventListener('click', () => go('accounts'));
    $('[data-action="add-account"]', n).addEventListener('click', startAdd);
    stage.append(n);
  }

  /* ---------- Error helpers ---------- */
  function applyErrors(form, errors) {
    clearErrors(form);
    const ids = Object.keys(errors);
    for (const id of ids) {
      const input = $('#' + id, form); const msg = $('#' + id + '-error', form);
      if (!input || !msg) continue;
      msg.textContent = errors[id]; msg.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      const desc = (input.getAttribute('aria-describedby') || '').split(' ').filter(Boolean);
      if (!desc.includes(msg.id)) desc.unshift(msg.id);
      input.setAttribute('aria-describedby', desc.join(' '));
    }
    if (ids.length) {
      const first = $('#' + ids[0], form);
      if (first) { const details = first.closest('details'); if (details) details.open = true; first.focus(); }
      alertNow(`${ids.length === 1 ? 'One field needs attention' : ids.length + ' fields need attention'}. ${errors[ids[0]]}`);
      return true;
    }
    return false;
  }
  function clearErrors(form) {
    $$('.field__error', form).forEach(m => { m.hidden = true; m.textContent = ''; });
    $$('[aria-invalid="true"]', form).forEach(i => { i.removeAttribute('aria-invalid'); const desc = (i.getAttribute('aria-describedby') || '').split(' ').filter(x => x && !x.endsWith('-error')); if (desc.length) i.setAttribute('aria-describedby', desc.join(' ')); else i.removeAttribute('aria-describedby'); });
  }
  function bindClearError(form) {
    form.addEventListener('input', (e) => {
      const input = e.target; if (!input.id || input.getAttribute('aria-invalid') !== 'true') return;
      input.removeAttribute('aria-invalid');
      const msg = $('#' + input.id + '-error', form); if (msg) { msg.hidden = true; msg.textContent = ''; }
      const desc = (input.getAttribute('aria-describedby') || '').split(' ').filter(x => x && x !== input.id + '-error');
      if (desc.length) input.setAttribute('aria-describedby', desc.join(' ')); else input.removeAttribute('aria-describedby');
    });
  }

  const SCREENS = { accounts: renderAccounts, choose: renderChoose, details: renderDetails, imap: renderImap, google: renderGoogle, review: renderReview, connect: renderConnect, done: renderDone };

  /* ---------------------------------------------------------------------
   * 7. Demo panel, gallery, theme, global actions, init
   * ------------------------------------------------------------------- */
  const panel = $('#demo-panel');
  const toggleButtons = $$('[data-action="toggle-demo"]');
  function setDemoOpen(open) {
    if (!service.isDemo) return;
    const wasOpen = !panel.hidden;
    panel.hidden = !open;
    document.body.classList.toggle('demo-open', open);
    toggleButtons.forEach(b => b.setAttribute('aria-expanded', String(open)));
    if (open) $('#demo-gallery').focus();
    else if (wasOpen) $('.topbar [data-action="toggle-demo"]').focus();
  }
  toggleButtons.forEach(b => b.addEventListener('click', () => setDemoOpen(panel.hidden)));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !panel.hidden) setDemoOpen(false); });

  const getPath = (obj, path) => path.split('.').reduce((o, k) => (o ? o[k] : undefined), obj);
  const setPath = (obj, path, v) => { const ks = path.split('.'); const last = ks.pop(); ks.reduce((o, k) => o[k], obj)[last] = v; };
  function syncDemoControls() {
    $$('[data-demo]', panel).forEach(c => {
      const key = c.dataset.demo; if (key === 'gallery') return;
      const v = getPath(demo, key);
      if (c.type === 'checkbox') c.checked = !!v; else c.value = String(v);
    });
  }
  panel.addEventListener('change', (e) => {
    const c = e.target; const key = c.dataset.demo; if (!key) return;
    if (key === 'gallery') { if (c.value) gallery(c.value); c.value = ''; return; }
    const v = c.type === 'checkbox' ? c.checked : c.value;
    setPath(demo, key, key === 'latency' ? Math.max(0, parseInt(v, 10) || 0) : v);
    if (key === 'accountsScenario') { service.reset(); state.accountsLoaded = false; if (state.screen === 'accounts') render({ focus: false }); }
    if (key === 'oauthClientConfigured' || key.startsWith('installation.')) {
      state.installation = { host: demo.installation.host.trim() || null, remote: !!demo.installation.remote, oauthClientConfigured: !!demo.oauthClientConfigured };
      if (state.screen === 'google' || state.screen === 'review') render({ focus: false });
    }
  });
  $('[data-action="demo-reset"]').addEventListener('click', () => {
    Object.assign(demo, DEMO_DEFAULTS());
    syncDemoControls(); service.reset();
    state.accountsLoaded = false; state.draft = newDraft(); state.oauth = { status: 'idle', identity: null, controller: null }; state.connect = newChecks();
    state.installation = { host: null, remote: false, oauthClientConfigured: true };
    go('accounts'); announce('Demo reset.');
  });

  function sampleDraft(method) {
    const d = newDraft();
    d.method = method; d.email = 'demo@example.com'; d.name = 'Demo mailbox';
    d.incoming = { host: 'imap.example.com', port: '993', security: 'tls', username: 'demo@example.com', password: 'demo-password', usernameEdited: false };
    d.outgoing = { host: 'smtp.example.com', port: '465', security: 'tls', sameLogin: true, username: '', password: '' };
    return d;
  }
  function setRows(map) {
    for (const [k, v] of Object.entries(map)) state.connect.rows[k] = v;
  }
  const R = (st, message) => ({ state: st, message });

  function gallery(key) {
    if (!service.isDemo) return;
    setDemoOpen(false);
    state.installation = { host: demo.installation.host.trim() || null, remote: !!demo.installation.remote, oauthClientConfigured: true };
    state.oauth = { status: 'idle', identity: null, controller: null };
    state.connect = newChecks();
    const imapErrors = () => { const d = state.draft; d.incoming.host = 'imaps://imap.example.com'; d.incoming.port = '99999'; d.incoming.password = ''; d.outgoing.host = ''; };
    switch (key) {
      case 'accounts-empty': demo.accountsScenario = 'empty'; syncDemoControls(); service.reset(); state.accountsLoaded = false; go('accounts'); break;
      case 'accounts-list': demo.accountsScenario = 'both'; syncDemoControls(); service.reset(); state.accountsLoaded = false; go('accounts'); break;
      case 'choose': state.draft = newDraft(); go('choose'); break;
      case 'details': state.draft = newDraft(); state.draft.method = 'imap'; go('details'); break;
      case 'details-errors': state.draft = newDraft(); state.draft.method = 'imap'; state.draft.email = 'alex@example'; go('details'); $('form', stage).requestSubmit(); break;
      case 'imap': state.draft = sampleDraft('imap'); go('imap'); break;
      case 'imap-errors': state.draft = sampleDraft('imap'); imapErrors(); go('imap'); $('form', stage).requestSubmit(); break;
      case 'imap-separate': state.draft = sampleDraft('imap'); state.draft.outgoing.sameLogin = false; state.draft.outgoing.username = 'demo-smtp'; go('imap'); break;
      case 'google-ready': state.draft = sampleDraft('google'); go('google'); break;
      case 'google-progress': state.draft = sampleDraft('google'); state.screen = 'google'; state.oauth = { status: 'in-progress', identity: null, controller: new AbortController() }; render(); break;
      case 'google-cancelled': state.draft = sampleDraft('google'); state.oauth = { status: 'cancelled', byUser: false, identity: null, controller: null }; go('google'); break;
      case 'google-expired': state.draft = sampleDraft('google'); state.oauth = { status: 'expired', identity: null, controller: null }; go('google'); break;
      case 'google-missing': state.draft = sampleDraft('google'); state.installation.oauthClientConfigured = false; demo.oauthClientConfigured = false; syncDemoControls(); go('google'); break;
      case 'google-denied': state.draft = sampleDraft('google'); state.oauth = { status: 'denied-policy', identity: null, controller: null }; go('google'); break;
      case 'google-authorized': state.draft = sampleDraft('google'); state.oauth = { status: 'authorized', identity: 'demo@example.com', controller: null }; go('google'); break;
      case 'google-mismatch': state.draft = sampleDraft('google'); state.oauth = { status: 'authorized', identity: 'different.person@example.com', controller: null }; go('google'); break;
      case 'review-imap': state.draft = sampleDraft('imap'); go('review'); break;
      case 'review-google-remote': state.draft = sampleDraft('google'); state.draft.google.identity = 'demo@example.com'; state.installation = { host: demo.installation.host.trim() || 'demo-host.example', remote: true, oauthClientConfigured: true }; go('review'); break;
      case 'connect-checking': state.draft = sampleDraft('imap'); go('connect'); runConnect(); break;
      case 'connect-incoming-failed': state.draft = sampleDraft('imap'); setRows({ incoming: R('failed', 'We couldn’t sign in to incoming mail. Check your username and password.'), outgoing: R('passed', 'Signed in to smtp.example.com. No mail was sent.') }); go('connect'); break;
      case 'connect-save-failed': state.draft = sampleDraft('imap'); setRows({ incoming: R('passed', 'Signed in to imap.example.com as demo@example.com.'), outgoing: R('passed', 'Signed in to smtp.example.com. No mail was sent.'), save: R('failed', 'The settings couldn’t be saved. Nothing was changed. Try again; if it keeps failing, the installation may be out of disk space or read-only.') }); go('connect'); break;
      case 'connect-observe-unavailable': state.draft = sampleDraft('imap'); state.connect.saved = true; state.connect.accountId = 'preview-gallery'; setRows({ incoming: R('passed', 'Signed in to imap.example.com as demo@example.com.'), outgoing: R('passed', 'Signed in to smtp.example.com. No mail was sent.'), save: R('passed', 'Settings saved for demo@example.com.'), observe: R('unavailable', 'Mail watching isn’t installed on this installation yet. Reading and sending mail still work.') }); go('connect'); break;
      case 'connect-cancelled': state.draft = sampleDraft('imap'); state.connect.cancelled = true; setRows({ incoming: R('passed', 'Signed in to imap.example.com as demo@example.com.') }); go('connect'); break;
      case 'done': state.draft = sampleDraft('imap'); state.connect.saved = true; setRows({ incoming: R('passed', ''), outgoing: R('passed', ''), save: R('passed', ''), observe: R('passed', 'Pimcamp will notice new mail for this account as it arrives.') }); go('done'); break;
      case 'done-partial': state.draft = sampleDraft('imap'); state.connect.saved = true; state.connect.accountId = 'preview-gallery'; setRows({ incoming: R('passed', ''), outgoing: R('passed', ''), save: R('passed', ''), observe: R('unavailable', 'Mail watching isn’t installed on this installation yet. Reading and sending mail still work.') }); go('done'); break;
    }
  }

  /* Theme */
  const THEME_KEY = 'pimcamp.theme'; // preference only; never account data
  function applyTheme(pref) {
    document.documentElement.dataset.theme = pref;
    const r = $(`#theme-picker input[value="${pref}"]`); if (r) r.checked = true;
    try { localStorage.setItem(THEME_KEY, pref); } catch (_) { /* storage may be unavailable; theme still applies */ }
  }
  $('#theme-picker').addEventListener('change', (e) => { if (e.target.name === 'theme') applyTheme(e.target.value); });

  /* Global actions */
  document.addEventListener('click', (e) => {
    const a = e.target.closest('[data-action="go-accounts"]');
    if (a && a.closest('.topbar')) { e.preventDefault(); if (!state.connect.running) go('accounts'); }
    const add = e.target.closest('[data-action="add-account"]');
    if (add && add.closest('[data-screen="accounts"]')) startAdd();
  });

  /* Init */
  let saved = 'system';
  try { saved = localStorage.getItem(THEME_KEY) || 'system'; } catch (_) { /* ignore */ }
  applyTheme(['system', 'light', 'dark'].includes(saved) ? saved : 'system');
  syncDemoControls();

  // Deep links for review: index.html#demo=<gallery key>&theme=<system|light|dark>&latency=<ms>
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  if (['system', 'light', 'dark'].includes(hash.get('theme'))) applyTheme(hash.get('theme'));
  if (hash.has('latency')) { demo.latency = Math.max(0, parseInt(hash.get('latency'), 10) || 0); syncDemoControls(); }
  if (service.isDemo && hash.get('demo')) gallery(hash.get('demo'));
  else render({ focus: false });

  // Exposed for manual testing in the console; not used by the UI.
  if (service.isDemo) window.PimcampOnboardingPreview = { state, demo, service, go, gallery };
  else {
    $('.demo-banner').hidden = true;
    toggleButtons.forEach(button => { button.hidden = true; });
    document.querySelector('meta[name="description"]').content = 'Connect and configure your email accounts with Pimcamp.';
  }
})();
