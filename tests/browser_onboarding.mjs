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
  let page;
  const pageDeadline = Date.now() + 10000;
  while (!page && Date.now() < pageDeadline) {
    const pages = await (await fetch(`http://${base.host}/json/list`)).json();
    page = pages.find(candidate => candidate.type === 'page');
    if (!page) await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.ok(page, 'Chromium did not create its initial page');
  socket = new WebSocket(page.webSocketDebuggerUrl);
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
      await waitFor(() => document.querySelector('[data-screen="done"]'));
      if (document.querySelector('main').textContent.includes('Not tested') || document.querySelector('main .notice--warning')) throw Error('Successful setup still presents untested notifications as a problem');
      const cookieVisible = document.cookie.includes('pimcamp_setup');
      if (cookieVisible) throw Error('Session cookie is readable by JavaScript');
      const accounts = await window.PimcampSetupService.listAccounts();
      if (accounts.length !== 1 || accounts[0].address !== 'browser-test@custom.example') throw Error('Account did not persist through real service');
      if (JSON.stringify(accounts).includes('fixture-browser-password')) throw Error('Account listing exposed password');
      click('[data-action="add-account"]');
      await waitFor(() => document.querySelector('[data-method="google"]'));
      click('[data-method="google"]');
      set('f-email', 'google-browser-test@example.com');
      set('f-name', 'Preserve my Google draft');
      document.querySelector('main form').requestSubmit();
      const byText = text => Array.from(document.querySelectorAll('main button')).find(button => button.textContent.trim() === text);
      await waitFor(() => byText('Set up Google sign-in'));
      byText('Set up Google sign-in').click();
      await waitFor(() => document.querySelector('[data-action="setup-next"]'));
      for (let part = 0; part < 3; part++) click('[data-action="setup-next"]');
      const input = document.querySelector('#f-client-file');
      const transfer = new DataTransfer();
      transfer.items.add(new File([JSON.stringify({installed:{client_id:'123-live-browser.apps.googleusercontent.com',client_secret:'fixture-import-secret'}})],
                                  'client_secret_fixture.json', {type:'application/json'}));
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {bubbles:true}));
      document.querySelector('#gsetup-import-form').requestSubmit();
      await waitFor(() => document.querySelector('[data-action="setup-continue-google"]'));
      if (document.querySelector('main').textContent.includes('fixture-import-secret')) throw Error('Imported secret appeared in live summary');
      const app = await window.PimcampSetupService.googleApplicationStatus();
      if (!app.configured || app.clientId !== '123-live-browser.apps.googleusercontent.com') throw Error('Registration did not persist through HTTP');
      if ((await window.PimcampSetupService.listAccounts()).length !== 1) throw Error('Registration changed existing accounts');
      click('[data-action="setup-continue-google"]');
      await waitFor(() => byText('Continue with Google'));
      if (!document.querySelector('main').textContent.includes('google-browser-test@example.com')) throw Error('Live registration lost the email draft');
      return {ownerHandoff:true, realTransport:true, customAccountSaved:true, reviewRedacted:true,
              cookieHttpOnly:true, completionWithoutNotificationWarning:true, accountListingRedacted:true,
              googleRegistrationImport:true, googleRegistrationPreservesAccount:true, googleRegistrationReturnsToConsent:true};
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
        if (process.env.PIMCAMP_REVIEW_SCREENSHOTS === '1' && width !== 320 && ['accounts-empty', 'accounts-details', 'imap', 'google-missing', 'connect-incoming-failed', 'connect-storage-failed', 'done-remote', 'google-setup-intro', 'google-setup-platform', 'google-setup-import', 'google-setup-saved'].includes(state)) {
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
    const originalObservation = service.observationReadiness;
    service.observationReadiness = () => { throw Error('Onboarding must not wait for notification verification'); };
    const connect = document.querySelector('[data-action="connect"]');
    connect.click(); connect.click();
    await waitFor(() => !state.connect.running);
    if (commits !== 1 || !state.connect.saved) throw Error('Duplicate submission or missing save');
    if (state.screen !== 'done' || document.querySelector('h1').textContent !== 'Account connected') throw Error('Saved account did not finish clearly');
    if (document.querySelector('main .notice--warning')) throw Error('Untested notifications look like failed setup');
    if (state.draft.incoming.password || state.draft.outgoing.password) throw Error('Passwords retained after save');
    service.commitAccount = originalCommit;
    service.observationReadiness = originalObservation;
    gallery('details');
    set('f-email', 'back-navigation@custom.example');
    set('f-name', 'Keep this name');
    document.querySelector('main [data-action="back"]').click();
    document.querySelector('[data-method="imap"]').click();
    if (document.getElementById('f-email').value !== 'back-navigation@custom.example' ||
        document.getElementById('f-name').value !== 'Keep this name') throw Error('Back discarded account details');
    gallery('accounts-list');
    await waitFor(() => document.querySelector('[data-action="check-account"]'));
    const details = document.querySelector('.account__details');
    if (!details || !details.textContent.toLowerCase().includes('notification')) throw Error('Notification information missing from account details');
    const originalCheck = service.checkAccount;
    service.checkAccount = async () => { throw Object.assign(new Error('fixture'), {publicMessage:'Fixture connection unavailable'}); };
    document.querySelector('[data-action="check-account"]').click();
    await waitFor(() => !Object.values(state.busy).some(Boolean));
    if (!document.querySelector('main').textContent.includes('Fixture connection unavailable')) throw Error('Check failure not shown');
    service.checkAccount = originalCheck;
    gallery('connect-save-failed');
    if (!document.querySelector('main').textContent.includes('Saving could not be confirmed')) throw Error('Save uncertainty hidden');
    gallery('done-google');
    if (document.querySelector('main .notice--warning') || document.querySelector('main').textContent.includes('Not tested')) throw Error('Google completion looks unfinished');
    gallery('connect-saved');
    for (const spinner of document.querySelectorAll('main .spinner')) {
      if (getComputedStyle(spinner).display !== 'none' || getComputedStyle(spinner).animationName !== 'none') throw Error('Completed check still spins');
    }
    return { validationFocus:true, customAccount:true, customPortPreserved:true,
             reviewRedacted:true, rejectedCredentialsNotSaved:true,
             duplicateSubmitPrevented:true, notificationDetailsAvailable:true, passwordsReleased:true,
             backPreservesDetails:true, checkFailureRecoverable:true, saveUncertaintyVisible:true,
             completionWithoutNotificationWarning:true, completedChecksStopSpinning:true, savedAccountFinishesAutomatically:true };
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
  const registration = await evaluate(`(async () => {
    const {state, demo, service, gallery} = window.PimcampOnboardingPreview;
    const waitFor = async predicate => {
      const deadline = Date.now() + 5000;
      while (!predicate()) {
        if (Date.now() > deadline) throw Error('Registration interaction timed out');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    const choose = text => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([text], 'client_secret_fixture.json', {type:'application/json'}));
      const input = document.querySelector('#f-client-file');
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {bubbles:true}));
    };
    gallery('google-setup-import');
    const email = state.draft.email;
    const raw = JSON.stringify({installed:{client_id:'123-browser.apps.googleusercontent.com',client_secret:'fixture-import-secret'}});
    let submitted = 0;
    const original = service.configureGoogleApplication;
    service.configureGoogleApplication = async payload => {
      submitted++;
      if (payload.credentialsJson !== raw) throw Error('Imported file payload was lost or changed');
      return original(payload);
    };
    choose('{"web":{}}');
    document.querySelector('#gsetup-import-form').requestSubmit();
    await new Promise(resolve => setTimeout(resolve, 50));
    if (submitted || !document.querySelector('#f-client-file[aria-invalid="true"]')) throw Error('Wrong client file not rejected locally');
    choose(raw);
    const form = document.querySelector('#gsetup-import-form');
    form.requestSubmit(); form.requestSubmit();
    await waitFor(() => state.googleSetup.step === 'saved' || state.googleSetup.error);
    if (state.googleSetup.error) throw Error('Valid registration failed: ' + state.googleSetup.error.message);
    await waitFor(() => document.querySelector('[data-action="setup-continue-google"]'));
    if (submitted !== 1) throw Error('Duplicate registration submission');
    if (document.querySelector('#f-client-file') || JSON.stringify(state).includes('fixture-import-secret') || document.querySelector('main').textContent.includes('fixture-import-secret')) throw Error('Imported secret retained or displayed');
    if (state.draft.email !== email || state.connect.saved) throw Error('Registration changed the mailbox or falsely connected it');
    document.querySelector('[data-action="setup-continue-google"]').click();
    if (state.screen !== 'google' || !state.installation.oauthClientConfigured) throw Error('Did not return to usable Google sign-in');
    service.configureGoogleApplication = original;
    gallery('google-setup-import');
    choose(raw);
    document.querySelector('[data-action="setup-cancel"]').click();
    if (document.querySelector('#f-client-file') || state.screen !== 'google') throw Error('Cancellation retained the file input');
    const originalTimeout = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) => originalTimeout(callback, delay === 45000 ? 20 : delay, ...args);
    try {
      gallery('google-setup-import');
      let release;
      service.configureGoogleApplication = () => new Promise(resolve => { release = resolve; });
      choose(raw);
      document.querySelector('#gsetup-import-form').requestSubmit();
      await waitFor(() => state.googleSetup.error);
      if (state.googleSetup.submitting || state.googleSetup.error.kind !== 'unconfirmed') throw Error('Timeout left saving indefinitely or claimed no write');
      release(await original({credentialsJson:raw}));
      await waitFor(() => document.querySelector('[data-action="setup-continue-google"]'));
      gallery('google-setup-import');
      choose(raw);
      document.querySelector('#gsetup-import-form').requestSubmit();
      await waitFor(() => state.googleSetup.error);
      const staleRelease = release;
      document.querySelector('[data-action="setup-cancel"]').click();
      gallery('google-setup-import');
      staleRelease({configured:true, clientId:'stale.apps.googleusercontent.com'});
      await new Promise(resolve => originalTimeout(resolve, 50));
      if (state.googleSetup.step !== 'import' || state.googleSetup.app.clientId === 'stale.apps.googleusercontent.com') throw Error('Late save overwrote a newer screen');
    } finally {
      window.setTimeout = originalTimeout;
      service.configureGoogleApplication = original;
    }
    return {registrationImport:true, registrationDuplicatePrevented:true, registrationSecretReleased:true,
            registrationDraftPreserved:true, registrationCancellation:true, registrationTimeoutRecovers:true,
            registrationLateSaveHandled:true, registrationStaleSaveIgnored:true};
  })()`);
  Object.assign(journey, registration);
  const recovery = await evaluate(`(async () => {
    const {state, gallery, service} = window.PimcampOnboardingPreview;
    const idle = async () => {
      const deadline = Date.now() + 5000;
      while (state.connect.running) {
        if (Date.now() > deadline) throw Error('Recovery test timed out');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    gallery('google-setup-platform');
    const checkpoint = document.querySelector('[data-slot="test-user-email"]');
    if (!checkpoint || checkpoint.textContent !== state.draft.email) throw Error('Test-user checkpoint does not identify the entered email');
    const guide = document.querySelector('main').textContent;
    if (!guide.includes('Save') || !guide.includes('list')) throw Error('Google saved-list checkpoint missing');
    gallery('google-progress');
    const help = document.querySelector('[data-slot="help"]');
    if (!help || !help.textContent.includes('verification process')) throw Error('No recovery help while waiting for callback');
    help.querySelector('summary').click();
    if (!help.open) throw Error('Google help does not expand');
    gallery('connect-storage-failed');
    if (Array.from(document.querySelectorAll('main button')).some(button => /Edit .*settings/.test(button.textContent))) throw Error('Vault error asks user to repair email settings');
    if (!document.querySelector('main').textContent.includes('existing')) throw Error('Vault repair does not distinguish an existing vault');
    gallery('done-remote');
    const done = document.querySelector('main').textContent;
    if (!done.includes(state.draft.email) || !done.includes(state.installation.host)) throw Error('Completion lost address or host');
    const incoming = service.checkIncoming, commit = service.commitAccount;
    try {
      gallery('review-imap');
      service.checkIncoming = async () => { throw Object.assign(new Error('fixture'), {code:'credential_storage', publicMessage:'Storage unavailable'}); };
      document.querySelector('[data-action="connect"]').click(); await idle();
      if (state.connect.problem?.kind !== 'storage') throw Error('Real storage code not recognized');
      service.checkIncoming = incoming;
      gallery('review-google-remote');
      service.commitAccount = async () => { throw Object.assign(new Error('fixture'), {code:'google_signin_required', publicMessage:'Sign in again'}); };
      document.querySelector('[data-action="connect"]').click(); await idle();
      const retry = Array.from(document.querySelectorAll('main button')).find(button => button.textContent === 'Sign in with Google again');
      if (!retry) throw Error('Expired Google save has no sign-in recovery');
      const email = state.draft.email; retry.click();
      if (state.screen !== 'google' || state.draft.email !== email) throw Error('Google expiry recovery lost draft');
      gallery('review-imap');
      service.commitAccount = async () => { throw Object.assign(new Error('fixture'), {code:'setup_unavailable', publicMessage:'Check saved accounts'}); };
      document.querySelector('[data-action="connect"]').click(); await idle();
      if (!Array.from(document.querySelectorAll('main button')).some(button => button.textContent === 'Check Accounts')) throw Error('Unknown save offers no reconciliation path');
    } finally { service.checkIncoming = incoming; service.commitAccount = commit; }
    return {googleTestUserCheckpoint:true, googleHelpDuringWait:true, vaultRepairNotPasswordBlame:true, completionIdentityAndHost:true,
            structuredVaultError:true, expiredGoogleRecovery:true, unknownSaveRecovery:true};
  })()`);
  Object.assign(journey, recovery);
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
