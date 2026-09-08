// Optional preview review: node tests/browser_onboarding.mjs /path/to/preview
// Uses installed Chromium and Node's WebSocket; no package downloads required.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const preview = resolve(process.argv[2] || 'ui/onboarding');
for (const name of ['index.html', 'styles.css', 'app.js']) {
  assert.ok((await readFile(join(preview, name))).length, `${name} is missing or empty`);
}
const profile = await mkdtemp(join(tmpdir(), 'pimcamp-ui-browser-'));
const output = await mkdtemp(join(tmpdir(), 'pimcamp-ui-review-'));
const browser = spawn(process.env.PIMCAMP_REVIEW_BROWSER || 'chromium', ['--headless', '--disable-gpu', '--no-first-run',
  '--no-default-browser-check', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
let socket;
try {
  const endpoint = await new Promise((resolveEndpoint, reject) => {
    const timer = setTimeout(() => reject(new Error('Chromium did not start in 20 seconds')), 20000);
    let buffer = '';
    browser.once('error', reject);
    browser.once('exit', () => reject(new Error('Chromium exited before startup')));
    browser.stderr.on('data', chunk => {
      buffer += chunk;
      const match = buffer.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) { clearTimeout(timer); resolveEndpoint(match[1]); }
    });
  });
  const base = new URL(endpoint);
  const pages = await (await fetch(`http://${base.host}/json/list`)).json();
  socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await new Promise((yes, no) => { socket.onopen = yes; socket.onerror = no; });
  let nextId = 0;
  const pending = new Map();
  const errors = [];
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
    const task = pending.get(message.id);
    if (task) {
      clearTimeout(task.timer); pending.delete(message.id);
      if (message.error) task.reject(new Error(message.error.message));
      else task.resolve(message.result);
    }
  };
  function call(method, params = {}) {
    const id = ++nextId;
    return new Promise((resolveCall, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, 30000);
      pending.set(id, { resolve: resolveCall, reject, timer });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async function evaluate(expression) {
    const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  await call('Runtime.enable');
  await call('Page.enable');
  await call('Emulation.setEmulatedMedia', { features: [{name: 'prefers-reduced-motion', value: 'reduce'}] });
  const launchFile = process.env.PIMCAMP_REVIEW_LAUNCH_FILE;
  await call('Page.navigate', { url: pathToFileURL(launchFile || join(preview, 'index.html')).href });
  if (launchFile) {
    let ready = false;
    for (let retry = 0; retry < 100 && !ready; retry++) {
      try { ready = await evaluate(`document.documentElement.dataset.mode === 'live' && Boolean(document.querySelector('[data-action="add-account"]'))`); }
      catch { /* bootstrap redirect replaces the execution context */ }
      if (!ready) await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.ok(ready, 'Owner launch handoff did not reach the live account UI');
    const result = await evaluate(`(async () => {
      const waitFor = async predicate => {
        const end = Date.now() + 5000;
        while (!predicate()) {
          if (Date.now() > end) throw Error('Live interaction timed out');
          await new Promise(resolve => setTimeout(resolve, 20));
        }
      };
      const click = selector => document.querySelector(selector).click();
      const set = (id, value) => {
        const input = document.getElementById(id); input.value = value;
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
      };
      if (window.PimcampOnboardingPreview) throw Error('Live mode exposed fixture controls');
      if (!document.querySelector('.demo-banner').hidden) throw Error('Live mode kept demo banner');
      click('[data-action="add-account"]');
      await waitFor(() => document.querySelector('[data-method="imap"]'));
      click('[data-method="imap"]');
      set('f-email', 'browser-test@custom.example');
      set('f-name', 'Browser test · work@custom.example');
      document.querySelector('main form').requestSubmit();
      set('f-in-host', 'incoming.custom.example');
      set('f-out-host', 'outgoing.custom.example');
      set('f-in-pass', 'fixture-browser-password');
      document.querySelector('main form').requestSubmit();
      if (!document.querySelector('[data-screen="review"]')) throw Error('No review screen');
      if (document.querySelector('main').textContent.includes('fixture-browser-password')) throw Error('Password appeared in summary');
      click('[data-action="connect"]');
      await waitFor(() => document.querySelector('.check-row[data-key="save"][data-state="passed"]'));
      await waitFor(() => document.querySelector('.check-row[data-key="observe"][data-state="unavailable"]'));
      const cookieVisible = document.cookie.includes('pimcamp_setup');
      if (cookieVisible) throw Error('Session cookie is readable by JavaScript');
      const accounts = await window.PimcampSetupService.listAccounts();
      if (accounts.length !== 1 || accounts[0].address !== 'browser-test@custom.example') throw Error('Account did not persist through real service');
      if (JSON.stringify(accounts).includes('fixture-browser-password')) throw Error('Account listing exposed password');
      return {ownerHandoff:true, realTransport:true, customAccountSaved:true, reviewRedacted:true,
              cookieHttpOnly:true, partialObserverStatus:true, accountListingRedacted:true};
    })()`);
    assert.deepEqual(errors, [], 'Uncaught live browser exceptions');
    await writeFile(join(output, 'live-fixture-results.json'), JSON.stringify(result, null, 2));
    console.log(`Passed ${Object.keys(result).length} browser-to-server fixture checks. No real mail or keys used. Artifacts: ${output}`);
  } else {
  await evaluate(`new Promise(resolve => {
    if (document.readyState === 'complete') resolve();
    else addEventListener('load', resolve, {once:true});
  })`);
  const states = await evaluate(`Array.from(document.querySelectorAll('#demo-gallery option')).map(x => x.value).filter(Boolean)`);
  await evaluate('window.PimcampOnboardingPreview.demo.latency = 0');
  assert.ok(states.length >= 20, 'Expected the complete screen/state gallery');
  const results = [];
  const layoutFailures = [];
  for (const width of (process.env.PIMCAMP_REVIEW_LAYOUT === '0' ? [] : [1280, 375, 320])) {
    await call('Emulation.setDeviceMetricsOverride', { width, height: 960, deviceScaleFactor: 1, mobile: false });
    for (const theme of ['light', 'dark']) {
      console.log(`Reviewing ${width}px / ${theme}`);
      await evaluate(`document.querySelector('input[name="theme"][value="${theme}"]').click()`);
      for (const state of states) {
        await evaluate(`(() => { const select = document.querySelector('#demo-gallery'); select.value = ${JSON.stringify(state)}; select.dispatchEvent(new Event('change', {bubbles:true})); })()`);
        await evaluate(`new Promise(resolve => {
          const deadline = Date.now() + 3000;
          function check() {
            if (document.querySelector('main h1') || Date.now() > deadline) resolve();
            else setTimeout(check, 25);
          }
          setTimeout(check, 50);
        })`);
        const dimensions = await evaluate(`({width:innerWidth, scroll:document.documentElement.scrollWidth, title:document.querySelector('main h1')?.textContent})`);
        assert.ok(dimensions.title, `Missing heading: ${state}`);
        if (dimensions.scroll > dimensions.width) {
          const offenders = await evaluate(`Array.from(document.querySelectorAll('main *')).filter(n => n.getBoundingClientRect().right > innerWidth).map(n => ({tag:n.tagName, class:n.className, right:n.getBoundingClientRect().right})).slice(0,12)`);
          layoutFailures.push({width, theme, state, offenders});
        }
        if (process.env.PIMCAMP_REVIEW_SCREENSHOTS === '1' && width !== 320 && ['accounts-empty', 'imap', 'google-missing', 'connect-incoming-failed'].includes(state)) {
          const shot = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
          await writeFile(join(output, `${width}-${theme}-${state}.png`), Buffer.from(shot.data, 'base64'));
        }
        results.push({width, theme, state});
      }
    }
  }
  const journey = await evaluate(`(async () => {
    const { state, demo, service, gallery } = window.PimcampOnboardingPreview;
    const waitFor = async predicate => {
      const end = Date.now() + 5000;
      while (!predicate()) {
        if (Date.now() > end) throw Error('Interaction check timed out');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    const set = (id, value) => {
      const input = document.getElementById(id);
      input.value = value;
      input.dispatchEvent(new Event('input', {bubbles:true}));
      input.dispatchEvent(new Event('change', {bubbles:true}));
    };
    const submit = () => document.querySelector('main form').requestSubmit();
    demo.latency = 0;
    gallery('details');
    submit();
    if (!document.querySelector('#f-email[aria-invalid="true"]')) throw Error('Missing email validation');
    if (document.activeElement.id !== 'f-email') throw Error('Validation did not focus email');
    set('f-email', 'alex@custom.example');
    set('f-name', 'alex.work@custom.example');
    submit();
    if (state.screen !== 'imap') throw Error('Details did not continue');
    set('f-in-host', 'incoming.custom.example');
    set('f-out-host', 'outgoing.custom.example');
    set('f-in-pass', 'fixture-only-password');
    set('f-out-port', '2525');
    set('f-out-security', 'starttls');
    if (document.getElementById('f-out-port').value !== '2525') throw Error('Custom port overwritten');
    submit();
    if (state.screen !== 'review') throw Error('Settings did not reach review');
    if (document.querySelector('main').textContent.includes('fixture-only-password')) throw Error('Password in review');
    let commits = 0;
    const originalCommit = service.commitAccount;
    service.commitAccount = async (...args) => { commits++; return originalCommit(...args); };
    demo.outcomes.incoming = 'fail';
    document.querySelector('[data-action="connect"]').click();
    await waitFor(() => !state.connect.running);
    if (commits !== 0 || state.connect.saved) throw Error('Saved after rejected credentials');
    if (state.connect.rows.incoming.state !== 'failed') throw Error('Missing actionable failure');
    gallery('review-imap');
    demo.outcomes.incoming = 'pass';
    demo.outcomes.outgoing = 'pass';
    demo.outcomes.save = 'pass';
    demo.outcomes.observe = 'unavailable';
    const connect = document.querySelector('[data-action="connect"]');
    connect.click(); connect.click();
    await waitFor(() => !state.connect.running);
    if (commits !== 1 || !state.connect.saved) throw Error('Duplicate submission or missing save');
    if (state.connect.rows.observe.state !== 'unavailable') throw Error('Watcher unavailability hidden');
    if (state.draft.incoming.password || state.draft.outgoing.password) throw Error('Passwords retained after save');
    service.commitAccount = originalCommit;
    gallery('details');
    set('f-email', 'back-navigation@custom.example');
    set('f-name', 'Keep this name');
    document.querySelector('main [data-action="back"]').click();
    document.querySelector('[data-method="imap"]').click();
    if (document.getElementById('f-email').value !== 'back-navigation@custom.example' ||
        document.getElementById('f-name').value !== 'Keep this name') throw Error('Back discarded account details');
    gallery('accounts-list');
    await waitFor(() => document.querySelector('[data-action="check-account"]'));
    const originalCheck = service.checkAccount;
    service.checkAccount = async () => { throw Object.assign(new Error('fixture'), {publicMessage:'Fixture connection unavailable'}); };
    document.querySelector('[data-action="check-account"]').click();
    await waitFor(() => !Object.values(state.busy).some(Boolean));
    if (!document.querySelector('main').textContent.includes('Fixture connection unavailable')) throw Error('Check failure not shown');
    service.checkAccount = originalCheck;
    gallery('connect-save-failed');
    if (!document.querySelector('main').textContent.includes('Saving could not be confirmed')) throw Error('Save uncertainty hidden');
    gallery('done-partial');
    if (!document.querySelector('main').textContent.includes('New-mail notifications are not verified yet')) throw Error('Unverified watcher shown as missing');
    return { validationFocus:true, customAccount:true, customPortPreserved:true,
             reviewRedacted:true, rejectedCredentialsNotSaved:true,
             duplicateSubmitPrevented:true, partialStatusVisible:true, passwordsReleased:true,
             backPreservesDetails:true, checkFailureRecoverable:true, saveUncertaintyVisible:true,
             watcherStatusTruthful:true };
  })()`);
  assert.deepEqual(errors, [], 'Uncaught browser exceptions');
  // Exercise real browser keyboard events, not just JS click/requestSubmit.
  await evaluate(`window.PimcampOnboardingPreview.gallery('details')`);
  async function key(key, code, virtualKey) {
    const params = {key, code, windowsVirtualKeyCode:virtualKey};
    await call('Input.dispatchKeyEvent', {type:'keyDown', ...params, ...(key === 'Enter' ? {text:'\r'} : {})});
    await call('Input.dispatchKeyEvent', {type:'keyUp', ...params});
  }
  async function tabTo(id) {
    for (let count = 0; count < 80; count++) {
      if (await evaluate(`document.activeElement?.id === ${JSON.stringify(id)}`)) return;
      await key('Tab', 'Tab', 9);
    }
    throw Error(`Keyboard could not reach ${id}`);
  }
  for (const [id, text] of [['f-email', 'keyboard@custom.example'], ['f-name', 'Keyboard account']]) {
    await tabTo(id);
    await call('Input.insertText', {text});
  }
  await key('Enter', 'Enter', 13);
  assert.equal(await evaluate('window.PimcampOnboardingPreview.state.screen'), 'imap');
  for (const [id, text] of [['f-in-host', 'incoming.custom.example'],
                           ['f-out-host', 'outgoing.custom.example'], ['f-in-pass', 'fixture-keyboard-password']]) {
    await tabTo(id);
    await call('Input.insertText', {text});
  }
  await key('Enter', 'Enter', 13);
  assert.equal(await evaluate('window.PimcampOnboardingPreview.state.screen'), 'review');
  assert.equal(await evaluate(`document.querySelector('main').textContent.includes('fixture-keyboard-password')`), false);
  journey.keyboardDetailsAndServers = true;
  await writeFile(join(output, 'results.json'), JSON.stringify(results, null, 2));
  await writeFile(join(output, 'interaction-results.json'), JSON.stringify(journey, null, 2));
  await writeFile(join(output, 'layout-failures.json'), JSON.stringify(layoutFailures, null, 2));
  assert.deepEqual(layoutFailures, [], `Layout failures; screenshots and details: ${output}`);
  console.log(`Passed ${results.length} screen/layout checks and ${Object.keys(journey).length} interaction checks. Review artifacts: ${output}`);
  }
} finally {
  socket?.close();
  const exited = new Promise(resolveExit => browser.once('exit', resolveExit));
  if (browser.exitCode === null && browser.signalCode === null) { browser.kill('SIGTERM'); await exited; }
  await rm(profile, { recursive: true, force: true });
}
