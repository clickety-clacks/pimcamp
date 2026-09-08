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
    googleHelperAvailable: true,
    googleSetupOutcome: 'saved',
    googleOutcome: 'authorized',
    outcomes: { incoming: 'pass', outgoing: 'pass', save: 'pass' },
  });
  const demo = DEMO_DEFAULTS();
  // Public identifier only. Fixtures never hold a client secret or an imported client file.
  const DEMO_CLIENT_ID = '000000000000-demo.apps.googleusercontent.com';

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

  /**
   * Preflight for a Google client file. Looks only at the shape, returns a
   * user-facing message, and never echoes any value from the file. The
   * installation validates again before storing anything.
   */
  function inspectClientJson(text) {
    const notClient = 'That file isn’t a Google client file. Choose the JSON file Google offered when you created the Desktop app client.';
    let data;
    try { data = JSON.parse(text); } catch (_) { return { ok: false, code: 'not-json', message: notClient }; }
    if (!data || typeof data !== 'object' || Array.isArray(data)) return { ok: false, code: 'not-json', message: notClient };
    if ('web' in data) return { ok: false, code: 'web-client', message: 'This file describes a Web application client. Pimcamp needs a Desktop app client. Go back a part, create one, and download its file.' };
    const c = data.installed;
    if (!c || typeof c !== 'object') return { ok: false, code: 'not-client', message: notClient };
    if (typeof c.client_id !== 'string' || !/^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$/.test(c.client_id) || typeof c.client_secret !== 'string' || !c.client_secret || c.client_secret.length > 4096 || c.client_secret.includes('\0')) return { ok: false, code: 'incomplete', message: 'This file is missing the client details Pimcamp needs. Download the client file again from Google Auth Platform and choose that one.' };
    return { ok: true, clientId: c.client_id.trim() };
  }

  /* ---------------------------------------------------------------------
   * 4. Service boundary
   *    The real implementation talks to the onboarding service. Every
   *    method returns plain data; nothing here touches the DOM.
   * ------------------------------------------------------------------- */
  function createDemoService(settings) {
    let accounts = [];
    let configuredClientId = null; // public identifier only; set when the demo "saves" a registration
    const lat = (f) => Math.max(0, Number(settings.latency) || 0) * f;
    const publicError = (message, code) => { const e = new Error(message); e.publicMessage = message; if (code) e.code = code; return e; };
    // Same shape as the installation's real vault failure: a thrown, user-facing message with no traceback.
    const VAULT_LOCKED = 'Could not save to the encrypted password vault on this host. Unlock it or check that Secret Service is running.';

    const api = {
      isDemo: true,
      reset() { accounts = fixtureAccounts(settings.accountsScenario); configuredClientId = null; },

      /** { configured, helperAvailable, clientId? }. clientId is Google's public identifier; no secret is ever returned. */
      async googleApplicationStatus() {
        await delay(lat(0.4));
        const configured = !!settings.oauthClientConfigured;
        const status = { configured, helperAvailable: settings.googleHelperAvailable !== false };
        if (configured) status.clientId = configuredClientId || DEMO_CLIENT_ID;
        return status;
      },

      /**
       * Validates the shape of a Desktop app client file and "stores" it. The text is
       * inspected and dropped; only the public client ID is remembered. Any failure is
       * thrown with a user-facing message, so the caller treats it as not confirmed.
       */
      async configureGoogleApplication({ credentialsJson }) {
        await delay(lat(1.2));
        const check = inspectClientJson(credentialsJson);
        if (!check.ok) throw publicError(check.message);
        if (settings.googleSetupOutcome === 'unconfirmed') await new Promise(() => {}); // never settles; the presentation's timeout takes over
        if (settings.googleSetupOutcome === 'rejected') throw publicError('This file doesn’t describe a Desktop app client this installation can use. Download the client file again from Google Auth Platform and choose that one.');
        if (settings.googleSetupOutcome === 'save-failed') throw publicError(VAULT_LOCKED, 'storage');
        settings.oauthClientConfigured = true;
        settings.googleHelperAvailable = true;
        configuredClientId = check.clientId;
        return { configured: true, clientId: check.clientId };
      },

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
          case 'failed': throw publicError('Google authorization could not be verified. Try again or check the installation’s Google application settings.');
          default: return { status: 'cancelled', byUser: false };
        }
      },

      async checkIncoming(draft, signal) {
        await delay(lat(1.4), signal);
        const o = settings.outcomes.incoming;
        if (o === 'storage') throw publicError(VAULT_LOCKED, 'storage'); // the vault refused before any sign-in was tried
        if (draft.method === 'google') return { ok: true, message: 'Google accepted the authorization for reading mail.' };
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
        if (settings.outcomes.save === 'expired') throw publicError('This setup expired. Start account setup again.', 'expired');
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

      /** Kept on the boundary for callers outside setup. The setup screens no longer call it: a
       *  saved account is complete, and new-mail delivery is only ever reported as not tested here. */
      async observationReadiness(accountId, signal) {
        await delay(lat(0.9), signal);
        return { state: 'unavailable', message: 'Mail watching is configured but a new-mail event has not been verified. Reading and sending have separate connection checks.' };
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
  const STEP_OF = { choose: 0, details: 1, imap: 2, google: 2, 'google-setup': 2, review: 3, connect: 4, done: 5 };

  /* Parts of the one-time Google registration. The intro and the saved summary sit outside the count. */
  const GSETUP_PARTS = [
    { key: 'project', label: 'Google Cloud project' },
    { key: 'platform', label: 'App and client' },
    { key: 'import', label: 'Client file' },
  ];
  const GSETUP_ORDER = ['intro', 'project', 'platform', 'import', 'saved'];
  function newGoogleSetup() {
    return { step: 'intro', app: null, statusError: null, submitting: false, error: null, token: null, refreshFailed: false, alreadyConfigured: false };
  }

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
    const row = () => ({ state: 'waiting', message: '', code: null });
    // Three steps decide the outcome. Mail watching is not a setup step: nothing here tests notifications.
    return { running: false, cancellable: false, cancelled: false, saved: false, accountId: null, controller: null,
      problem: null, // { kind: 'storage'|'expired', message } for a screen-level notice that is not about the entered details
      rows: { incoming: row(), outgoing: row(), save: row() } };
  }

  /** Classifies a failed result or thrown error. Explicit `code` wins; otherwise the user-facing message decides. */
  const STORAGE_RE = /keyring|vault|secret service|protected storage|credential stor/i;
  const EXPIRED_RE = /setup (attempt )?(has )?expired|start account setup again/i;
  function failureCode(source) {
    if (!source) return null;
    if (source.code === 'credential_storage') return 'storage';
    if (source.code === 'setup_expired' || source.code === 'google_signin_required') return 'expired';
    if (source.code === 'setup_unavailable') return 'unavailable';
    if (['auth', 'unreachable', 'storage', 'expired'].includes(source.code)) return source.code;
    const text = source.publicMessage || source.message || '';
    if (STORAGE_RE.test(text)) return 'storage';
    if (EXPIRED_RE.test(text)) return 'expired';
    return null;
  }

  /** The shared vault explanation. Names the mail host when the installation reports one. */
  function vaultHelp({ open = false } = {}) {
    const d = tpl('tpl-vault-help');
    slot(d, 'vault-host').textContent = state.installation.host ? `the mail host ${state.installation.host}` : 'the mail host, where this installation runs';
    if (open) d.open = true;
    return d;
  }

  const state = {
    screen: 'accounts',
    accounts: [],
    accountsLoaded: false,
    installation: { host: null, remote: false, oauthClientConfigured: true },
    draft: newDraft(),
    oauth: { status: 'idle', identity: null, controller: null },
    connect: newChecks(),
    googleSetup: newGoogleSetup(), // one-time registration of this installation with Google
    busy: {}, // per-account busy flags
  };

  const stage = $('#stage');
  const rail = $('#rail');

  function go(screen, { focus = true } = {}) {
    if (state.oauth.controller) { state.oauth.controller.abort(); state.oauth.controller = null; if (state.oauth.status === 'in-progress') state.oauth.status = 'idle'; }
    if (state.screen === 'google-setup' && screen !== 'google-setup') leaveGoogleSetup();
    state.screen = screen;
    render({ focus });
  }

  function backTarget() {
    const d = state.draft;
    switch (state.screen) {
      case 'choose': return 'accounts';
      case 'details': return d.reconnectId ? 'accounts' : 'choose';
      case 'imap': case 'google': return d.reconnectId ? 'accounts' : 'details';
      case 'google-setup': return 'google';
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
    if (state.screen === 'google-setup') {
      const i = GSETUP_PARTS.findIndex(p => p.key === state.googleSetup.step);
      announce(`${titleFor()}. Google sign-in setup${i >= 0 ? `, part ${i + 1} of ${GSETUP_PARTS.length}` : ''}.`);
    }
    else if (typeof step === 'number' && step < STEPS.length) announce(`${titleFor()}. Step ${step + 1} of ${STEPS.length}.`);
    else announce(titleFor());
  }
  function renderLoading(text) {
    stage.replaceChildren();
    const n = tpl('tpl-loading');
    slot(n, 'text').textContent = text;
    stage.append(n);
  }

  /** Whole-screen failure of a service call. Entered details stay in `state.draft`; `retry` and `back` keep the user in the flow. */
  function showServiceError(error, { retry = null, back = null } = {}) {
    const message = error.publicMessage || 'Could not reach account setup. Check that it is still running, then retry.';
    const kept = state.draft.email ? ` Your details for ${state.draft.email} are kept.` : '';
    const actions = el('div', { class: 'actions' });
    if (back) actions.append(btn('Back', { kind: 'ghost', iconName: 'arrow-left', onclick: back }));
    actions.append(btn('Try again', { kind: 'primary', iconName: 'refresh', onclick: retry || (() => { state.accountsLoaded = false; go('accounts'); }) }));
    stage.replaceChildren(el('h1', {class: 'screen__title', tabindex: '-1', text: 'Setup needs attention'}),
      el('p', {class: 'screen__lede', text: message + kept}), actions);
    focusTitle(); alertNow(message);
  }

  function renderRail() {
    rail.replaceChildren();
    if (state.screen === 'accounts') { rail.append(tpl('tpl-rail-intro')); return; }
    const n = tpl('tpl-rail-steps');
    const current = STEP_OF[state.screen];
    slot(n, 'eyebrow').textContent = state.screen === 'google-setup' ? 'Set up Google sign-in' : state.draft.reconnectId ? 'Reconnect account' : 'Add account';
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
    accountDetails(r, a);
    return r;
  }

  /** Per-account facts from `listAccounts`. Notifications are described as tested only when the record says so explicitly. */
  function accountDetails(r, a) {
    slot(r, 'details-for').textContent = ` for ${a.address}`;
    const dl = slot(r, 'detail-list');
    const row = (t, v, small) => { const x = tpl('tpl-summary-row'); slot(x, 'term').textContent = t; const dd = slot(x, 'value'); if (typeof v === 'string') dd.textContent = v; else dd.append(v); if (small) dd.append(el('small', { text: small })); dl.append(x); };
    const secLabel = (s) => s === 'starttls' ? 'STARTTLS' : 'TLS';
    const server = (s) => [s.host, s.port ? `port ${s.port}` : null, s.security ? secLabel(s.security) : null].filter(Boolean).join(' · ');
    row('Sign-in', a.method === 'google' ? 'Google' : 'IMAP & SMTP with a password',
      a.method === 'google' ? 'Pimcamp keeps the permission Google granted, not a password.' : 'The password is kept in the encrypted vault on the host, never here.');
    if (a.incoming && a.incoming.host) row('Incoming server', server(a.incoming), a.incoming.username ? `Username ${a.incoming.username}` : null);
    if (a.outgoing && a.outgoing.host) row('Outgoing server', server(a.outgoing), a.outgoing.sameLogin === false && a.outgoing.username ? `Username ${a.outgoing.username}` : null);
    if (state.installation.host) row('Saved on', state.installation.host);
    row('Sign-in checks', a.status === 'connected' ? 'Passed' : a.status === 'needs-reconnect' ? 'Failing' : 'Not checked yet',
      a.note || (a.status === 'connected' ? 'Incoming and outgoing sign-in checks passed. No mail was sent.' : null));
    const obs = a.observation || null;
    const verified = !!(obs && obs.state === 'verified');
    const pill = el('span', { class: 'status', 'data-tone': verified ? 'success' : '', text: verified ? 'Verified' : 'Not tested' });
    row('New-mail notifications', pill, verified ? (obs.message || 'A real new-mail event reached Pimcamp for this account.')
      : (obs && obs.message) || 'Setup checks sign-in only and sends no test mail. Whether Pimcamp notices new mail in this mailbox hasn’t been confirmed yet.');
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
    const oldDetails = slot(old, 'details');
    if (oldDetails && oldDetails.open) slot(fresh, 'details').open = true; // an expanded details area survives a re-check
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
        panel.append(head('accent', 'spinner', 'Waiting for Google', `Finish signing in as ${email} and approving access in the Google window. This page updates on its own when Google sends you back.`));
        panel.append(el('p', { class: 'panel__text', text: 'If the Google window shows an error instead of asking for permission, this page won’t change by itself: choose Cancel here, then open “Trouble with Google sign-in?” below. Nothing has been saved yet.' }));
        actions.append(secondary('Cancel', cancelOAuth));
        break;
      case 'cancelled':
        panel.append(head('warning', 'alert', 'Google sign-in was cancelled', s.byUser ? 'You cancelled before Google finished. Nothing was changed.' : 'The Google window was closed, or Google reported that access was declined, before sign-in finished. Nothing was changed. If Google showed an error page instead of asking for permission, the help below covers the usual causes.'));
        actions.append(primary('Try again', beginOAuth, 'refresh'));
        break;
      case 'failed':
        panel.append(head('danger', 'alert', 'Google sign-in could not finish',
          `${s.message || 'Google’s answer could not be verified for this attempt.'} Nothing was saved. If the Google window ended on an error, see the help below before trying again.`));
        actions.append(primary('Try again', beginOAuth, 'refresh'));
        break;
      case 'expired':
        panel.append(head('warning', 'alert', 'The sign-in request expired', 'Google sign-in requests only stay valid for a few minutes. Start again and finish the approval in one go. If Google stopped at an error, the help below explains what to fix first.'));
        actions.append(primary('Start again', beginOAuth, 'refresh'));
        break;
      case 'denied-policy':
        // This backend status is reserved for explicit organization-policy errors.
        panel.append(head('danger', 'x', `Google denied access for ${email}`,
          'Google reported an organization-policy restriction. Ask the Google Workspace administrator to allow this app. Nothing was changed. If the Google window shows a different error, the help below explains the other common causes.'));
        actions.append(useImap(), primary('Try again', beginOAuth, 'refresh'));
        break;
      case 'missing-client': {
        const started = state.googleSetup.step !== 'intro';
        const guided = typeof service.googleApplicationStatus === 'function' && typeof service.configureGoogleApplication === 'function';
        panel.append(head('warning', 'lock', 'Google sign-in isn’t set up on this installation yet',
          'Before any Google account can be connected here, this copy of Pimcamp has to be registered with Google once. That takes about ten minutes on Google’s own pages, and it is separate from choosing which mailbox to connect.'));
        panel.append(el('p', { class: 'panel__text', text: guided
          ? (started ? `You’ve already started this. Your details for ${email} are kept while you finish.` : `Your details for ${email} are kept while you do it. If your provider allows app passwords, IMAP & SMTP works without this step.`)
          : 'Guided setup isn’t available from this version of account setup. Whoever administers the installation can register the app through its supported configuration, then choose Check again. If your provider allows app passwords, IMAP & SMTP works now.' }));
        actions.append(useImap(), secondary('Check again', recheckInstallation));
        if (guided) actions.append(primary(started ? 'Continue setting up Google sign-in' : 'Set up Google sign-in', startGoogleSetup, 'arrow-right'));
        break;
      }
      case 'authorized': {
        const match = s.identity.toLowerCase() === email.toLowerCase();
        if (match) {
          panel.append(head('success', 'check', 'Google authorized this account', 'Google confirmed access for the address you entered. Continue to review the settings.'));
          panel.append(identityCard(s.identity, 'Authorized by Google'));
          d.google.identity = s.identity;
          actions.append(primary('Continue', () => go('review'), 'arrow-right'));
        } else {
          // The mismatch guard: an identity that differs from the entered address is never resolved silently.
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
    // Recovery help stays reachable in every state; it opens on its own when Google refused or never called back.
    const help = slot(n, 'help');
    $$('[data-slot="help-email"]', help).forEach(x => { x.textContent = email; });
    if (s.status === 'denied-policy' || s.status === 'failed' || s.status === 'expired' || (s.status === 'cancelled' && !s.byUser)) help.open = true;
    $('[data-action="back"]', n).addEventListener('click', back);
    stage.append(n);
  }

  function showNotice(root, tone, title, text) {
    const existing = $('.notice--inline', root);
    if (existing) existing.remove();
    const nt = el('div', { class: `notice notice--inline notice--${tone}`, role: 'status' }, [icon(tone === 'danger' ? 'alert' : 'info'), el('div', { class: 'notice__body' }, [el('p', { class: 'notice__title', text: title }), el('p', { text })])]);
    slot(root, 'state').after(nt);
  }

  /** Re-reads installation facts for the current setup attempt (a reconnect keeps its account id). */
  async function refreshInstallation() {
    const setup = await boundedSetupRequest(service.beginSetup(state.draft.reconnectId || undefined));
    state.installation = setup.installation;
  }
  async function recheckInstallation() {
    renderLoading('Checking installation…');
    try { await refreshInstallation(); go('google'); }
    catch (error) { showServiceError(error, { retry: recheckInstallation, back: () => go('google') }); }
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

  /* ---------- Google sign-in setup: one-time registration of this installation with Google ----------
   * Separate from choosing a mailbox. Registration happens on Google's own pages (opened in a new
   * tab); here the user only reads guidance and, at the end, picks the client file Google offered.
   * The file's text exists in JS only between "Save" and the service's answer, and is never shown. */
  const GSETUP_TIMEOUT = 45000;
  async function boundedSetupRequest(request) {
    let timer;
    try {
      return await Promise.race([request, new Promise((_, reject) => {
        timer = setTimeout(() => reject(Object.assign(new Error('Setup did not answer'), {
          publicMessage: 'Account setup did not answer in time. Your saved account is unchanged; check the connection and try again.'
        })), GSETUP_TIMEOUT);
      })]);
    } finally { clearTimeout(timer); }
  }
  const GSETUP_COPY = {
    intro: {
      title: 'Set up Google sign-in for this installation',
      lede: 'Before any Google account can be connected here, Google needs to know about this copy of Pimcamp. You do this once, on Google’s own pages, in about ten minutes. It is not the same as choosing which mailbox to connect; that comes afterwards.',
    },
    project: {
      title: 'Create a Google Cloud project and turn on Gmail',
      lede: 'Sign in to Google Cloud console with any Google account you control. A project is just the container Google uses for this registration.',
    },
    platform: {
      title: 'Describe the app and create its client',
      lede: 'In the same project, open Google Auth Platform. Google asks four things; take them in order. Nothing here touches a mailbox yet.',
    },
    import: {
      title: 'Bring the client file to this installation',
      lede: 'Choose the file Google offered to download. Pimcamp sends it once to this installation, which checks it, keeps the secret part in protected storage, and remembers only the public client ID.',
    },
    saved: {
      title: 'Google sign-in is set up on this installation',
      lede: () => (state.googleSetup.alreadyConfigured
        ? 'This installation already has a registration saved, so there is nothing more to set up here. No Google account is connected yet; that happens on Google’s site when you choose Continue with Google.'
        : 'Pimcamp saved the registration. No Google account is connected yet; that happens on Google’s site when you choose Continue with Google.'),
    },
  };

  function normalizeApp(app) {
    return {
      configured: !!(app && app.configured),
      helperAvailable: !!(app && app.helperAvailable === true),
      clientId: app && typeof app.clientId === 'string' && app.clientId ? app.clientId : null,
    };
  }
  const formatSize = (bytes) => bytes < 1024 ? `${bytes} bytes` : `${Math.round(bytes / 1024)} KB`;

  /** Entry from the Google screen. Re-reads the installation's registration status every time. */
  function startGoogleSetup() {
    const gs = state.googleSetup;
    gs.app = null; gs.statusError = null; gs.error = null; gs.refreshFailed = false; gs.alreadyConfigured = false;
    go('google-setup');
  }
  /** Called by go() when navigating anywhere else. The file input leaves with the screen; the draft stays. */
  function leaveGoogleSetup() {
    const gs = state.googleSetup;
    gs.token = null; gs.submitting = false; gs.error = null; gs.statusError = null;
  }

  const gsBackBtn = () => btn('Back', { kind: 'ghost', iconName: 'arrow-left', 'data-action': 'setup-back', onclick: setupBack });
  const gsCancelBtn = () => btn('Cancel', { kind: 'ghost', 'data-action': 'setup-cancel', onclick: () => go('google') });
  const gsNextBtn = (label = 'Next') => btn(label, { kind: 'primary', iconName: 'arrow-right', 'data-action': 'setup-next', onclick: setupNext });
  const gsSaveBtn = () => btn(state.installation.remote && state.installation.host ? `Save on ${state.installation.host}` : 'Save to this installation',
    { kind: 'primary', type: 'submit', form: 'gsetup-import-form', 'data-action': 'setup-save' });

  async function renderGoogleSetup({ focus = true } = {}) {
    const gs = state.googleSetup;
    if (!gs.app && !gs.statusError) {
      renderLoading('Checking this installation…');
      try {
        const app = normalizeApp(await boundedSetupRequest(service.googleApplicationStatus()));
        if (state.screen !== 'google-setup') return;
        gs.app = app;
        if (app.configured) {
          gs.step = 'saved'; gs.alreadyConfigured = true;
          if (!state.installation.oauthClientConfigured) { try { await refreshInstallation(); } catch (_) { gs.refreshFailed = true; } }
          if (state.screen !== 'google-setup') return;
        }
      } catch (error) {
        if (state.screen !== 'google-setup') return;
        gs.statusError = error.publicMessage || 'Could not reach account setup. Check that it is still running, then retry.';
      }
      stage.replaceChildren();
    }

    const n = tpl('tpl-google-setup');
    const title = slot(n, 'title'), lede = slot(n, 'lede'), body = slot(n, 'body'), actions = slot(n, 'actions');

    if (gs.statusError) {
      title.textContent = 'Couldn’t check this installation';
      lede.textContent = gs.statusError;
      body.append(el('p', { class: 'help' }, [icon('info'), el('span', { text: 'Nothing was changed and your account details are kept. Try again, or go back to the Google sign-in page.' })]));
      actions.append(gsBackBtn(), btn('Try again', { kind: 'primary', iconName: 'refresh', onclick: startGoogleSetup }));
      stage.append(n); afterRender(focus);
      if (focus) alertNow(gs.statusError);
      return;
    }

    const step = gs.step;
    const copy = GSETUP_COPY[step];
    title.textContent = copy.title;
    lede.textContent = typeof copy.lede === 'function' ? copy.lede() : copy.lede;
    if (step !== 'intro') renderStepper(slot(n, 'stepper'), step);
    slot(n, 'demo-notice').hidden = !(service.isDemo && (step === 'saved' || gs.error));

    switch (step) {
      case 'intro': {
        const part = tpl('tpl-gsetup-intro');
        slot(part, 'storage').hidden = gs.app.helperAvailable;
        slot(part, 'keep').textContent = state.draft.email ? `Your details for ${state.draft.email} are kept while you do this.` : '';
        bindRecheckStorage(part);
        body.append(part);
        actions.append(gsBackBtn(), gsNextBtn('Start'));
        break;
      }
      case 'project': body.append(tpl('tpl-gsetup-project')); actions.append(gsBackBtn(), gsCancelBtn(), gsNextBtn()); break;
      case 'platform': {
        const part = tpl('tpl-gsetup-platform');
        // The test-user step names the address being connected so it can be matched against Google's list.
        $$('[data-slot="test-user-email"]', part).forEach(x => { x.textContent = state.draft.email || 'the address you will connect'; });
        body.append(part); actions.append(gsBackBtn(), gsCancelBtn(), gsNextBtn());
        break;
      }
      case 'import': renderImportPart(body); buildImportActions(actions); break;
      case 'saved': renderSavedPart(body, actions); break;
    }
    stage.append(n);
    afterRender(focus);
  }

  function renderStepper(list, step) {
    list.hidden = false;
    const idx = step === 'saved' ? GSETUP_PARTS.length : GSETUP_PARTS.findIndex(p => p.key === step);
    GSETUP_PARTS.forEach((p, i) => {
      const st = i < idx ? 'done' : i === idx ? 'current' : 'todo';
      list.append(el('li', { class: 'stepper__item', 'data-state': st, 'aria-current': st === 'current' ? 'step' : null }, [
        el('span', { class: 'stepper__bar', 'aria-hidden': 'true' }),
        el('span', { class: 'stepper__label' }, [
          st === 'done' ? icon('check') : null,
          el('span', { text: `${i + 1}. ${p.label}` }),
          el('span', { class: 'visually-hidden', text: st === 'done' ? ' (completed)' : st === 'current' ? ' (current part)' : '' }),
        ]),
      ]));
    });
  }

  function setupBack() {
    const gs = state.googleSetup;
    if (gs.submitting) return;
    const i = GSETUP_ORDER.indexOf(gs.step);
    if (gs.statusError || i <= 0) { go('google'); return; }
    gs.error = null;
    gs.step = GSETUP_ORDER[i - 1];
    render();
  }
  function setupNext() {
    const gs = state.googleSetup;
    const i = GSETUP_ORDER.indexOf(gs.step);
    if (i < 0 || GSETUP_ORDER[i + 1] === undefined || GSETUP_ORDER[i + 1] === 'saved') return; // saving goes through the form
    gs.error = null;
    gs.step = GSETUP_ORDER[i + 1];
    render();
  }

  /** "Check again" inside the storage notice. Updates in place so a chosen file is not lost. */
  function bindRecheckStorage(root) {
    const button = $('[data-action="recheck-storage"]', root);
    if (!button) return;
    const idle = () => { button.disabled = false; button.replaceChildren(icon('refresh'), 'Check again'); };
    button.addEventListener('click', async () => {
      const gs = state.googleSetup;
      if (gs.submitting) return;
      button.disabled = true; button.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Checking…');
      announce('Checking the Google sign-in helper…');
      try {
        const app = normalizeApp(await boundedSetupRequest(service.googleApplicationStatus()));
        if (state.screen !== 'google-setup') return;
        gs.app = app;
        if (app.configured && gs.step !== 'saved') {
          gs.step = 'saved'; gs.alreadyConfigured = true; gs.error = null;
          try { await refreshInstallation(); } catch (_) { gs.refreshFailed = true; }
          if (state.screen === 'google-setup') render();
          return;
        }
        if (app.helperAvailable) {
          slot(root, 'storage').hidden = true;
          announce('The Google sign-in helper is available. Password storage will be checked when saving.');
          const next = $('#f-client-file', root) || $('[data-action="setup-next"]', stage);
          if (next) next.focus();
        } else { idle(); alertNow('The Google sign-in helper still isn’t available. Nothing was sent.'); }
      } catch (error) {
        if (state.screen !== 'google-setup') return;
        idle(); alertNow(error.publicMessage || 'Could not reach account setup to check. Try again in a moment.');
      }
    });
  }

  function renderImportPart(body) {
    const gs = state.googleSetup;
    const part = tpl('tpl-gsetup-import');
    slot(part, 'storage').hidden = gs.app.helperAvailable;
    bindRecheckStorage(part);
    const input = $('#f-client-file', part);
    const meta = slot(part, 'file-meta');
    input.disabled = gs.submitting;
    input.addEventListener('change', () => {
      const f = input.files && input.files[0];
      meta.hidden = !f;
      if (f) slot(part, 'file-name').textContent = `${f.name} · ${formatSize(f.size)}`;
      clearErrors(part);
    });
    $('[data-action="check-app-status"]', part).addEventListener('click', checkAppStatus);
    slot(part, 'vault').replaceWith(vaultHelp({ open: !!(gs.error && gs.error.kind === 'storage') }));
    renderSaveError(part);
    part.addEventListener('submit', (e) => { e.preventDefault(); submitClientFile(part); });
    body.append(part);
  }
  function buildImportActions(actions) {
    const gs = state.googleSetup;
    actions.replaceChildren();
    if (gs.submitting) {
      actions.append(
        el('span', { class: 'help' }, [icon('lock'), 'Saving to protected storage. This only takes a moment and can’t be cancelled.']),
        el('button', { type: 'button', class: 'btn btn--primary', disabled: true }, [el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Saving…']),
      );
    } else {
      actions.append(gsBackBtn(), gsCancelBtn(), gsSaveBtn());
    }
  }
  /** Updates the import part in place (busy state, error notice) so the chosen file survives a failed attempt. */
  function refreshImportChrome() {
    if (state.screen !== 'google-setup') return;
    const n = $('[data-screen="google-setup"]', stage);
    const form = n && $('#gsetup-import-form', n);
    if (!form) return;
    const gs = state.googleSetup;
    $('#f-client-file', form).disabled = gs.submitting;
    renderSaveError(form);
    buildImportActions(slot(n, 'actions'));
    slot(n, 'demo-notice').hidden = !(service.isDemo && gs.error);
  }
  function renderSaveError(form) {
    const gs = state.googleSetup;
    const box = slot(form, 'save-error');
    if (!gs.error) { box.hidden = true; return; }
    const kind = gs.error.kind;
    slot(form, 'save-error-title').textContent = kind === 'unconfirmed' ? 'Saving could not be confirmed'
      : kind === 'storage' ? 'The password vault couldn’t store the client secret' : 'The registration wasn’t saved';
    slot(form, 'save-error-text').textContent = `${gs.error.message} ${kind === 'unconfirmed'
      ? 'Check what the installation has saved before trying again.'
      : kind === 'storage'
        ? 'The file you chose is kept here. Ask the agent or operator to unlock or repair the vault on the host (see “About the encrypted password vault” below), then choose Save again. Nothing about the file is wrong.'
        : 'You can choose the file again and retry, or check what the installation has saved.'}`;
    box.hidden = false;
    const vault = slot(form, 'vault-help');
    if (vault && kind === 'storage') vault.open = true;
  }

  async function submitClientFile(form) {
    const gs = state.googleSetup;
    if (gs.submitting) return;
    const input = $('#f-client-file', form);
    const file = input.files && input.files[0];
    clearErrors(form);
    if (!file) { applyErrors(form, { 'f-client-file': 'Choose the client file first. It’s usually in your Downloads folder and its name starts with “client_secret”.' }); return; }
    if (file.size > 32 * 1024) { applyErrors(form, { 'f-client-file': 'That file is too large to be a Google client file. Choose the JSON file Google offered when you created the Desktop app client.' }); return; }

    if (!gs.app.helperAvailable) {
      // Recheck the helper before refusing; protected storage is checked separately on save.
      try { gs.app = normalizeApp(await boundedSetupRequest(service.googleApplicationStatus())); } catch (_) { /* keep the last known state */ }
      if (state.screen !== 'google-setup') return;
      if (!gs.app.helperAvailable) {
        slot(form, 'storage').hidden = false;
        alertNow('The Google sign-in helper is missing, so the file wasn’t sent. Choose Check again after it is installed.');
        $('[data-action="recheck-storage"]', form).focus();
        return;
      }
      slot(form, 'storage').hidden = true;
    }

    // Claim the attempt before asynchronous file reading so double-submit cannot race.
    if (gs.submitting) return;
    gs.submitting = true;
    refreshImportChrome();
    let text;
    try { text = await file.text(); }
    catch (_) { gs.submitting = false; refreshImportChrome(); applyErrors(form, { 'f-client-file': 'Pimcamp couldn’t read that file. Choose it again.' }); return; }
    if (state.screen !== 'google-setup' || state.googleSetup !== gs || !gs.submitting) { text = null; return; }
    const check = inspectClientJson(text);
    if (!check.ok) { text = null; gs.submitting = false; refreshImportChrome(); applyErrors(form, { 'f-client-file': check.message }); return; }

    const token = {};
    gs.token = token; gs.submitting = true; gs.error = null;
    refreshImportChrome();
    announce('Saving the registration to this installation…');

    // The request keeps running past the timeout; a late answer is still applied if this attempt is current.
    let settled = false;
    let request;
    try { request = Promise.resolve(service.configureGoogleApplication({ credentialsJson: text })); }
    catch (error) { request = Promise.reject(error); }
    request = request.finally(() => { settled = true; });
    text = null;
    const timeout = new Promise((_, reject) => setTimeout(() => { if (!settled) { const e = new Error('Timed out'); e.name = 'TimeoutError'; reject(e); } }, GSETUP_TIMEOUT));
    let result, failure = null;
    try { result = await Promise.race([request, timeout]); }
    catch (error) { failure = error; }

    if (failure && failure.name === 'TimeoutError') {
      request.then(late => { if (gs.token === token && !gs.submitting) finishConfigure(token, late, null); }, () => { /* the visible state already says unconfirmed */ });
    }
    finishConfigure(token, result, failure);
  }

  /** Applies the outcome of one save attempt. `token` identifies it; stale answers are ignored. */
  async function finishConfigure(token, result, failure) {
    const gs = state.googleSetup;
    const current = gs.token === token;
    if (!current) return;
    if (!failure && result && result.configured === true) {
      gs.submitting = false; gs.error = null; gs.step = 'saved'; gs.refreshFailed = false; gs.alreadyConfigured = false;
      gs.app = { configured: true, helperAvailable: true, clientId: typeof result.clientId === 'string' && result.clientId ? result.clientId : null };
      if (service.isDemo) syncDemoControls();
      if (!current || state.screen !== 'google-setup') return; // the user moved on; "Check again" on the Google screen picks this up
      renderLoading('Saved. Refreshing setup…'); // drops the file input straight away
      try { await refreshInstallation(); } catch (_) { gs.refreshFailed = true; }
      if (gs.token !== token || state.screen !== 'google-setup') return;
      gs.token = null;
      render();
      announce('The registration is saved on this installation. No Google account is connected yet.');
      return;
    }
    if (!current) return;
    gs.submitting = false;
    if (failure && failure.name === 'TimeoutError') {
      gs.error = { kind: 'unconfirmed', message: 'The installation didn’t confirm the save within a minute. It may still be finishing, or the request was lost.' };
    } else if (failure && failureCode(failure) === 'storage') {
      gs.error = { kind: 'storage', message: failure.publicMessage };
    } else if (failure) {
      gs.error = { kind: 'unconfirmed', message: failure.publicMessage || 'The connection ended before the installation confirmed the save.' };
    } else {
      gs.error = { kind: 'unconfirmed', message: 'The installation answered without confirming that the registration was saved.' };
    }
    refreshImportChrome();
    alertNow(`${gs.error.kind === 'unconfirmed' ? 'Saving could not be confirmed' : gs.error.kind === 'storage' ? 'The password vault couldn’t store the client secret' : 'The registration wasn’t saved'}. ${gs.error.message}`);
    const retry = $('[data-action="setup-save"]', stage);
    if (retry) retry.focus();
  }

  /** "Check what was saved": asks the installation instead of guessing after an unconfirmed attempt. */
  async function checkAppStatus(event) {
    const gs = state.googleSetup;
    if (gs.submitting) return;
    const button = event && event.currentTarget;
    if (button) { button.disabled = true; button.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Checking…'); }
    announce('Checking what this installation has saved…');
    let app = null, message = null;
    try { app = normalizeApp(await boundedSetupRequest(service.googleApplicationStatus())); }
    catch (error) { message = error.publicMessage || 'Could not reach account setup to check. Try again in a moment.'; }
    if (state.screen !== 'google-setup') return;
    if (button) { button.disabled = false; button.replaceChildren(icon('refresh'), 'Check what was saved'); }
    if (app && app.configured) {
      gs.app = app; gs.error = null; gs.step = 'saved'; gs.alreadyConfigured = false;
      if (service.isDemo) syncDemoControls();
      renderLoading('Saved. Refreshing setup…');
      try { await refreshInstallation(); } catch (_) { gs.refreshFailed = true; }
      if (state.screen !== 'google-setup') return;
      render();
      announce('The registration is saved on this installation.');
      return;
    }
    if (app) { gs.app = app; message = 'This installation has no Google registration saved yet. Choose the file and save again.'; }
    gs.error = { kind: gs.error ? gs.error.kind : 'save', message };
    refreshImportChrome();
    alertNow(message);
  }

  function renderSavedPart(body, actions) {
    const gs = state.googleSetup;
    const part = tpl('tpl-gsetup-saved');
    const dl = slot(part, 'summary');
    const row = (t, v, small, mono) => {
      const r = tpl('tpl-summary-row'); slot(r, 'term').textContent = t;
      const dd = slot(r, 'value');
      if (mono) dd.append(el('code', { text: v })); else dd.textContent = v;
      if (small) dd.append(el('small', { text: small }));
      dl.append(r);
    };
    const where = state.installation.remote ? (state.installation.host || 'the remote installation') : 'this installation';
    row('Registration', `${gs.alreadyConfigured ? 'Already saved' : 'Saved'} on ${where}`, 'Shared by every Google account connected through it.');
    if (gs.app.clientId) row('Client ID', gs.app.clientId, 'Google’s public identifier for this registration. It isn’t a secret.', true);
    else row('Client ID', 'Not reported by the installation');
    row('Client secret', 'In protected storage', 'Never shown here and not kept in this browser.');
    row('Google account', 'Not connected yet', state.draft.email ? `Next, you approve access for ${state.draft.email} on Google’s site.` : 'Next, you approve access on Google’s site.');
    slot(part, 'refresh-note').hidden = !gs.refreshFailed;
    body.append(part);
    actions.append(btn('Continue to Google sign-in', { kind: 'primary', iconName: 'arrow-right', 'data-action': 'setup-continue-google', onclick: () => go('google') }));
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
      sec('Password vault');
      row('Temporary only', 'This installation’s vault does not survive logout or reboot, so you may need to reconnect afterwards.');
    }
    if (d.reconnectId) { sec('Reconnect'); row('Existing account', 'Will be updated in place', 'Nothing is replaced until the checks pass.'); }
    slot(n, 'vault').replaceWith(vaultHelp());

    $('[data-action="back"]', n).addEventListener('click', back);
    $('[data-action="connect"]', n).addEventListener('click', () => { state.connect = newChecks(); go('connect'); runConnect(); });
    stage.append(n);
  }

  /* ---------- Connect (checks) ---------- */
  const CHECK_ROWS = [
    { key: 'incoming', label: 'Incoming mail sign-in' },
    { key: 'outgoing', label: 'Outgoing mail sign-in' },
    { key: 'save', label: 'Save settings' },
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
    const problem = c.running ? null : c.problem; // storage or expiry: the entered details are not the issue
    const failedAuth = ['incoming', 'outgoing'].some(k => c.rows[k].state === 'failed');
    const saveFailed = c.rows.save.state === 'failed';
    const host = state.installation.host || 'the host';

    let title, lede;
    if (c.running) { title = `Connecting ${d.email}`; lede = c.cancellable ? 'Signing in to check your settings. Nothing is saved until both sign-ins pass.' : 'Saving the settings. This only takes a moment.'; }
    else if (c.cancelled) { title = 'Checks cancelled'; lede = 'Nothing was saved. Your settings are kept, so you can run the checks again whenever you like.'; }
    else if (c.saved) { title = 'Settings saved'; lede = `${d.email} is connected. Continue to see what this account can do.`; }
    else if (problem && problem.kind === 'storage') { title = 'Your password couldn’t be stored yet'; lede = `The encrypted password vault on ${host} refused to keep the password. That says nothing about whether the password is right, and everything you entered is kept.`; }
    else if (problem && problem.kind === 'expired') { title = 'This setup attempt expired'; lede = d.method === 'google' ? 'Your account details are kept. Sign in with Google again to renew permission, then review and connect.' : 'Everything you entered is kept; run the checks again to renew this unfinished attempt.'; }
    else if (problem && problem.kind === 'unavailable') { title = 'Check whether this account was saved'; lede = 'The earlier setup is no longer available. Check Accounts before starting again; a previous save may have completed.'; }
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

    // Screen-level notice for failures that are about the host, not the entered details.
    const notice = slot(n, 'notice');
    notice.replaceChildren();
    notice.hidden = !problem;
    if (problem) {
      notice.className = 'notice notice--danger';
      notice.setAttribute('role', 'status');
      const body = el('div', { class: 'notice__body' });
      if (problem.kind === 'storage') {
        body.append(
          el('p', { class: 'notice__title', text: `The password vault on ${host} needs attention` }),
          el('p', { text: problem.message }),
          el('p', { text: 'Ask the agent or operator to unlock or repair the vault on the host; this page can’t do that, and nobody should ask you for the vault password in chat. You don’t need to enter your email password again. Once the vault is ready, choose Try again.' }),
          vaultHelp({ open: true }),
        );
      } else {
        body.append(el('p', { class: 'notice__title', text: 'The attempt is no longer open' }), el('p', { text: problem.message }));
      }
      notice.append(icon('alert'), body);
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
    } else if (problem && problem.kind === 'storage') {
      actions.append(btn('Back', { kind: 'ghost', iconName: 'arrow-left', onclick: back }), btn('Try again', { kind: 'primary', iconName: 'refresh', onclick: () => runConnect({ retry: true }) }));
    } else if (problem && problem.kind === 'expired') {
      actions.append(btn('Back', { kind: 'ghost', iconName: 'arrow-left', onclick: back }), d.method === 'google'
        ? btn('Sign in with Google again', { kind: 'primary', onclick: () => { state.oauth.status = 'idle'; d.google.identity = null; go('google'); } })
        : btn('Run checks again', { kind: 'primary', iconName: 'refresh', onclick: () => runConnect() }));
    } else if (problem && problem.kind === 'unavailable') {
      actions.append(btn('Check Accounts', { kind: 'primary', onclick: () => { state.accountsLoaded = false; go('accounts'); } }));
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
    // A storage or expiry failure is not about the entered password, so it gets no "edit" shortcut here.
    const editable = r.state === 'failed' && (def.key === 'incoming' || def.key === 'outgoing') && r.code !== 'storage' && r.code !== 'expired';
    if (!state.connect.running && editable) {
      acts.hidden = false;
      if (d.method === 'google') acts.append(btn('Sign in with Google again', { size: 'small', onclick: () => { state.oauth.status = 'idle'; d.google.identity = null; go('google'); } }));
      else acts.append(btn(def.key === 'incoming' ? 'Edit incoming settings' : 'Edit outgoing settings', { size: 'small', onclick: () => { go('imap'); const target = def.key === 'incoming' ? '#f-in-pass' : (d.outgoing.sameLogin ? '#f-in-pass' : '#f-out-pass'); const f = $(target, stage); if (f) f.focus(); } }));
    }
    return row;
  }
  function waitingText(key) {
    return { incoming: 'Signs in to the incoming server. Nothing is downloaded.', outgoing: 'Signs in to the outgoing server. No mail is sent.', save: 'Writes the settings for this account only.' }[key];
  }
  function updateRow(key, st, message, code = null) {
    state.connect.rows[key] = { state: st, message: message || '', code };
    const def = CHECK_ROWS.find(r => r.key === key);
    const old = $(`.check-row[data-key="${key}"]`, stage);
    if (old) old.replaceWith(checkRow(def, state.connect.rows[key]));
    if (st !== 'checking') announce(`${def.label}: ${STATE_LABEL[st][1]}. ${message || ''}`);
  }
  /** Records a host-level problem (vault, expiry) so the connect screen explains it instead of blaming the password. */
  function noteProblem(code, message) {
    state.connect.problem = ['storage', 'expired', 'unavailable'].includes(code) ? { kind: code, message } : null;
  }

  async function runConnect({ retry = false } = {}) {
    const c = state.connect;
    if (c.running) return;
    const d = state.draft;
    const controller = new AbortController();
    c.controller = controller; c.running = true; c.cancelled = false; c.problem = null;
    const todo = (k) => !retry || c.rows[k].state !== 'passed';
    for (const k of Object.keys(c.rows)) if (todo(k) && c.rows[k].state !== 'passed') c.rows[k] = { state: 'waiting', message: '', code: null };
    c.cancellable = !c.saved;
    refreshConnectChrome();
    announce(retry ? 'Retrying failed steps.' : `Connecting ${d.email}. Checking incoming mail sign-in.`);

    try {
      if (!c.saved) {
        for (const k of ['incoming', 'outgoing']) {
          if (!todo(k)) continue;
          updateRow(k, 'checking', k === 'incoming' ? 'Signing in to incoming mail…' : 'Signing in to outgoing mail…');
          const r = await (k === 'incoming' ? service.checkIncoming(d, controller.signal) : service.checkOutgoing(d, controller.signal));
          const code = r.ok ? null : failureCode(r);
          updateRow(k, r.ok ? 'passed' : 'failed', r.message, code);
          if (!r.ok) noteProblem(code, r.message);
        }
        if (c.rows.incoming.state !== 'passed' || c.rows.outgoing.state !== 'passed') {
          finishConnect(); alertNow(c.problem ? `${$('.screen__title', stage).textContent}. Nothing was saved.` : 'A sign-in check failed. Nothing was saved.'); return;
        }
        c.cancellable = false; refreshConnectChrome();
        updateRow('save', 'checking', 'Saving settings…');
        const s = await service.commitAccount(d, null);
        if (!s.ok) { const code = failureCode(s); updateRow('save', 'failed', s.message, code); noteProblem(code, s.message); finishConnect(); alertNow('Settings couldn’t be saved. Nothing was changed.'); return; }
        c.saved = true; c.accountId = s.accountId;
        d.incoming.password = ''; d.outgoing.password = '';
        state.accountsLoaded = false; // list will be reloaded from the service
        updateRow('save', 'passed', s.message);
      }
      // A saved account is a finished setup. Mail watching is not checked here and never delays this.
      finishConnect();
      go('done');
      announce(`Account connected. ${d.email} is saved. Setup is complete.`);
    } catch (e) {
      if (isAbort(e)) {
        for (const k of Object.keys(c.rows)) if (c.rows[k].state === 'checking') c.rows[k] = { state: 'waiting', message: '', code: null };
        c.cancelled = true;
        finishConnect();
        announce('Checks cancelled. Nothing was saved.');
        return;
      }
      // A thrown failure: never leave the screen stuck. Mark the in-flight step failed with the service's plain message.
      const code = failureCode(e);
      const message = e.publicMessage || 'This step could not finish. Check the connection and try again.';
      for (const k of Object.keys(c.rows)) if (c.rows[k].state === 'checking') c.rows[k] = { state: 'failed', message, code };
      noteProblem(code, message);
      finishConnect();
      alertNow(c.problem ? `${$('.screen__title', stage).textContent}. ${message}` : 'A step failed unexpectedly. You can try again.');
    }
  }
  function finishConnect() { const c = state.connect; c.running = false; c.controller = null; c.cancellable = false; refreshConnectChrome(); }
  function cancelConnect() { const c = state.connect; if (c.running && c.cancellable && c.controller) c.controller.abort(); }

  /* ---------- Done ---------- */
  /** Shown only after a confirmed save. Address and host are always visible; nothing here is a warning. */
  function renderDone() {
    const n = tpl('tpl-done');
    const d = state.draft;
    const host = state.installation.host || 'this installation';
    slot(n, 'title').textContent = 'Account connected';
    slot(n, 'lede').textContent = `${d.email} is connected${d.reconnectId ? ' again' : ''}. Pimcamp saved its settings on ${host}${state.installation.remote ? ', the installation you are connected to' : ''}.`;

    const dl = slot(n, 'summary');
    const row = (t, v, small) => { const r = tpl('tpl-summary-row'); slot(r, 'term').textContent = t; const dd = slot(r, 'value'); dd.textContent = v; if (small) dd.append(el('small', { text: small })); dl.append(r); };
    row('Email address', d.email);
    if (d.name.trim()) row('Account name', d.name.trim(), 'Only shown inside Pimcamp.');
    if (d.method === 'google') row('Authorized by Google', d.google.identity || d.email, 'Pimcamp keeps the permission Google granted, not a password.');
    else row('Sign-in', 'IMAP & SMTP with a password', 'The password is in the encrypted vault on the host, never in this browser.');
    row('Saved on', host, state.installation.remote ? 'A remote installation. Settings live there, not on the computer running this browser.' : 'The installation running account setup.');

    const caps = slot(n, 'caps');
    const cap = (label, desc) => {
      const r = tpl('tpl-cap-row'); r.dataset.state = 'ready';
      slot(r, 'label').textContent = label;
      const st = slot(r, 'state'); st.dataset.tone = 'success'; st.textContent = 'Ready';
      slot(r, 'desc').textContent = desc; caps.append(r);
    };
    cap('Read mail', 'Pimcamp can list and open messages in this mailbox. The incoming sign-in passed.');
    cap('Send mail', 'Pimcamp can send on your behalf, and only when you ask it to. The outgoing sign-in passed; no mail was sent.');

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

  const SCREENS = { accounts: renderAccounts, choose: renderChoose, details: renderDetails, imap: renderImap, google: renderGoogle, 'google-setup': renderGoogleSetup, review: renderReview, connect: renderConnect, done: renderDone };

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
    if (key === 'googleHelperAvailable' && state.screen === 'google-setup' && state.googleSetup.app) {
      state.googleSetup.app.helperAvailable = !!demo.googleHelperAvailable;
      if (!state.googleSetup.submitting) render({ focus: false });
    }
  });
  $('[data-action="demo-reset"]').addEventListener('click', () => {
    Object.assign(demo, DEMO_DEFAULTS());
    syncDemoControls(); service.reset();
    state.accountsLoaded = false; state.draft = newDraft(); state.oauth = { status: 'idle', identity: null, controller: null }; state.connect = newChecks();
    state.googleSetup = newGoogleSetup();
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
  const R = (st, message) => ({ state: st, message, code: null });

  function gallery(key) {
    if (!service.isDemo) return;
    setDemoOpen(false);
    state.installation = { host: demo.installation.host.trim() || null, remote: !!demo.installation.remote, oauthClientConfigured: true };
    state.oauth = { status: 'idle', identity: null, controller: null };
    state.connect = newChecks();
    state.googleSetup = newGoogleSetup();
    const imapErrors = () => { const d = state.draft; d.incoming.host = 'imaps://imap.example.com'; d.incoming.port = '99999'; d.incoming.password = ''; d.outgoing.host = ''; };
    // Google sign-in setup states: the installation has no client yet; the status call is already answered.
    const gsetup = (step, { helper = true, configured = false, clientId = null } = {}) => {
      state.draft = sampleDraft('google');
      state.installation.oauthClientConfigured = configured; demo.oauthClientConfigured = configured;
      demo.googleHelperAvailable = helper; syncDemoControls();
      const gs = state.googleSetup;
      gs.step = step; gs.app = { configured, helperAvailable: helper, clientId };
      return gs;
    };
    switch (key) {
      case 'accounts-empty': demo.accountsScenario = 'empty'; syncDemoControls(); service.reset(); state.accountsLoaded = false; go('accounts'); break;
      case 'accounts-list': demo.accountsScenario = 'both'; syncDemoControls(); service.reset(); state.accountsLoaded = false; go('accounts'); break;
      case 'accounts-details': {
        demo.accountsScenario = 'both'; syncDemoControls(); service.reset();
        state.accounts = fixtureAccounts('both'); state.accountsLoaded = true; // pre-answered, so the details can open synchronously
        go('accounts');
        const first = $('.account [data-slot="details"]', stage); if (first) first.open = true;
        break;
      }
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
      case 'google-failed': state.draft = sampleDraft('google'); state.oauth = { status: 'failed', identity: null, controller: null, message: 'Google authorization could not be verified. Try again or check the installation’s Google application settings.' }; go('google'); break;
      case 'google-denied': state.draft = sampleDraft('google'); state.oauth = { status: 'denied-policy', identity: null, controller: null }; go('google'); break;
      case 'google-authorized': state.draft = sampleDraft('google'); state.oauth = { status: 'authorized', identity: 'demo@example.com', controller: null }; go('google'); break;
      case 'google-mismatch': state.draft = sampleDraft('google'); state.oauth = { status: 'authorized', identity: 'different.person@example.com', controller: null }; go('google'); break;
      case 'google-setup-intro': gsetup('intro'); go('google-setup'); break;
      case 'google-setup-storage-missing': gsetup('import', { helper: false }); go('google-setup'); break;
      case 'google-setup-unavailable': { const gs = gsetup('intro'); gs.app = null; gs.statusError = 'Could not reach account setup. Check that it is still running, then retry.'; go('google-setup'); break; }
      case 'google-setup-project': gsetup('project'); go('google-setup'); break;
      case 'google-setup-platform': gsetup('platform'); go('google-setup'); break;
      case 'google-setup-import': gsetup('import'); go('google-setup'); break;
      case 'google-setup-import-invalid': { gsetup('import'); go('google-setup'); const form = $('#gsetup-import-form', stage); if (form) applyErrors(form, { 'f-client-file': inspectClientJson('{"web":{}}').message }); break; }
      case 'google-setup-saving': { const gs = gsetup('import'); gs.submitting = true; gs.token = {}; go('google-setup'); break; }
      case 'google-setup-save-failed': { const gs = gsetup('import'); gs.error = { kind: 'unconfirmed', message: 'The installation didn’t confirm the save. It may still be finishing, or the request was lost.' }; go('google-setup'); break; }
      case 'google-setup-saved': gsetup('saved', { configured: true, clientId: DEMO_CLIENT_ID }); go('google-setup'); break;
      case 'review-imap': state.draft = sampleDraft('imap'); go('review'); break;
      case 'review-google-remote': state.draft = sampleDraft('google'); state.draft.google.identity = 'demo@example.com'; state.installation = { host: demo.installation.host.trim() || 'demo-host.example', remote: true, oauthClientConfigured: true }; go('review'); break;
      case 'connect-checking': state.draft = sampleDraft('imap'); go('connect'); runConnect(); break;
      case 'connect-incoming-failed': state.draft = sampleDraft('imap'); setRows({ incoming: R('failed', 'We couldn’t sign in to incoming mail. Check your username and password.'), outgoing: R('passed', 'Signed in to smtp.example.com. No mail was sent.') }); go('connect'); break;
      case 'connect-storage-failed': {
        state.draft = sampleDraft('imap');
        const message = 'Could not save to the encrypted password vault on this host. Unlock it or check that Secret Service is running.';
        setRows({ incoming: { state: 'failed', message, code: 'storage' } });
        state.connect.problem = { kind: 'storage', message };
        go('connect'); break;
      }
      case 'connect-save-failed': state.draft = sampleDraft('imap'); setRows({ incoming: R('passed', 'Signed in to imap.example.com as demo@example.com.'), outgoing: R('passed', 'Signed in to smtp.example.com. No mail was sent.'), save: R('failed', 'The settings couldn’t be saved. Nothing was changed. Try again; if it keeps failing, the installation may be out of disk space or read-only.') }); go('connect'); break;
      case 'connect-saved': state.draft = sampleDraft('imap'); state.connect.saved = true; state.connect.accountId = 'preview-gallery'; setRows({ incoming: R('passed', 'Signed in to imap.example.com as demo@example.com.'), outgoing: R('passed', 'Signed in to smtp.example.com. No mail was sent.'), save: R('passed', 'Settings saved for demo@example.com.') }); go('connect'); break;
      case 'connect-cancelled': state.draft = sampleDraft('imap'); state.connect.cancelled = true; setRows({ incoming: R('passed', 'Signed in to imap.example.com as demo@example.com.') }); go('connect'); break;
      case 'done': state.draft = sampleDraft('imap'); state.connect.saved = true; setRows({ incoming: R('passed', ''), outgoing: R('passed', ''), save: R('passed', '') }); go('done'); break;
      case 'done-google': state.draft = sampleDraft('google'); state.draft.google.identity = 'demo@example.com'; state.connect.saved = true; setRows({ incoming: R('passed', ''), outgoing: R('passed', ''), save: R('passed', '') }); go('done'); break;
      case 'done-remote': state.draft = sampleDraft('imap'); state.installation = { host: demo.installation.host.trim() || 'demo-host.example', remote: true, oauthClientConfigured: true }; state.connect.saved = true; setRows({ incoming: R('passed', ''), outgoing: R('passed', ''), save: R('passed', '') }); go('done'); break;
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
