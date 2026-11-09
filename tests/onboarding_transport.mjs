// Deterministic tests of the real transport; no browser, provider, or credentials.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../ui/onboarding/live-service.js', import.meta.url), 'utf8');
function fixture({detached = false, start, expireAction, failAction, saved = false} = {}) {
  const actions = [];
  let statuses = ['authorized'];
  const context = {
    URL, DOMException, setTimeout: callback => setTimeout(callback, 0),
    window: {open: () => ({closed: detached, close() {this.closed = true;}})},
    fetch: async (path, options) => {
      if (path === '/api/session') return {ok:true, json:async () => ({csrf:'fixture'})};
      const request = JSON.parse(options.body);
      actions.push(request.action);
      if (request.action === expireAction) {
        expireAction = null;
        return {ok:false, json:async () => ({error:{code:'setup_expired', message:'Expired'}})};
      }
      if (request.action === failAction) throw Error('Connection lost; outcome unknown');
      let result;
      switch (request.action) {
        case 'beginSetup': result = {setupId:'fixture-setup'}; break;
        case 'cancelSetup': result = {ok:true}; break;
        case 'saveStatus': result = {ok:saved, accountId:saved ? 'saved-account' : undefined}; break;
        case 'checkIncoming': case 'checkOutgoing': case 'commitAccount': result = {ok:true}; break;
        case 'googleApplicationStatus': result = {configured:false, helperAvailable:true}; break;
        case 'configureGoogleApplication':
          assert.equal(request.credentialsJson, 'fixture-downloaded-json');
          assert.equal(options.headers['X-Pimcamp-CSRF'], 'fixture');
          result = {configured:true, clientId:'fixture.apps.googleusercontent.com'}; break;
        case 'beginOAuth':
          if (start) await start();
          result = {authorizationUrl:'https://accounts.google.com/fixture'}; break;
        case 'oauthStatus': result = {status:statuses.shift() || 'authorized', identity:'fixture@example.com'}; break;
        case 'cancelOAuth': result = {ok:true}; break;
        default: throw Error('Unexpected fixture action');
      }
      return {ok:true, json:async () => result};
    },
  };
  vm.runInNewContext(source, context);
  return {service:context.window.PimcampSetupService, actions, statuses:value => {statuses = value;}};
}

{
  const test = fixture({detached:true});
  await test.service.beginSetup();
  test.statuses(['in-progress', 'authorized']);
  const result = await test.service.beginOAuth({}, new AbortController().signal);
  assert.equal(result.status, 'authorized', 'Detached provider popup must not imply cancellation');
  assert.equal(test.actions.filter(action => action === 'oauthStatus').length, 2);
  assert.ok(!test.actions.includes('cancelOAuth'));
}

{
  let releaseStart;
  const started = new Promise(resolve => {releaseStart = resolve;});
  const test = fixture({start:() => started});
  await test.service.beginSetup();
  const controller = new AbortController();
  const first = test.service.beginOAuth({}, controller.signal);
  // Attach rejection handling before triggering cancellation.
  const rejected = assert.rejects(first, error => error.name === 'AbortError');
  await new Promise(resolve => setTimeout(resolve, 0));
  controller.abort();
  const second = test.service.beginOAuth({}, new AbortController().signal);
  releaseStart();
  await rejected;
  assert.equal((await second).status, 'authorized');
  assert.deepEqual(test.actions.slice(1), ['beginOAuth', 'cancelOAuth', 'beginOAuth', 'oauthStatus'],
                   'Retry must wait for pending start and explicit cleanup');
}

{
  const test = fixture();
  await test.service.beginSetup();
  test.statuses(['failed']);
  await assert.rejects(test.service.beginOAuth({}, new AbortController().signal), /could not be verified/);
  assert.equal(test.actions.at(-1), 'cancelOAuth');
}
{
  const test = fixture();
  await test.service.beginSetup();
  assert.equal((await test.service.googleApplicationStatus()).configured, false);
  assert.equal((await test.service.configureGoogleApplication({credentialsJson:'fixture-downloaded-json'})).configured, true);
  assert.ok(!test.actions.includes('beginOAuth'), 'Import must not silently start account consent');
  await test.service.beginSetup();
  assert.deepEqual(test.actions.slice(-2), ['cancelSetup', 'beginSetup']);
}
{
  const test = fixture({expireAction:'commitAccount'});
  await test.service.beginSetup('existing-account');
  assert.equal((await test.service.commitAccount({method:'imap', email:'fixture@example.com'})).ok, true);
  assert.deepEqual(test.actions.slice(1), ['commitAccount','cancelSetup','beginSetup','checkIncoming','checkOutgoing','commitAccount']);
}
{
  const test = fixture({failAction:'commitAccount'});
  await test.service.beginSetup();
  await assert.rejects(test.service.commitAccount({method:'imap'}), /outcome unknown/);
  assert.deepEqual(test.actions, ['beginSetup','commitAccount','saveStatus'], 'Unknown writes must not be replayed');
}
{
  const test = fixture({expireAction:'checkIncoming'});
  await test.service.beginSetup();
  await assert.rejects(test.service.checkIncoming({method:'google'}), error => error.code === 'google_signin_required');
  assert.ok(!test.actions.includes('beginOAuth'), 'Expired consent must not be silently restarted');
}
{
  const test = fixture({expireAction:'beginOAuth'});
  await test.service.beginSetup();
  assert.equal((await test.service.beginOAuth({}, new AbortController().signal)).status, 'authorized');
  assert.deepEqual(test.actions.slice(1), ['beginOAuth','cancelSetup','beginSetup','beginOAuth','oauthStatus']);
}
{
  const test = fixture({failAction:'commitAccount', saved:true});
  await test.service.beginSetup();
  assert.equal((await test.service.commitAccount({method:'imap'})).accountId, 'saved-account');
  assert.deepEqual(test.actions, ['beginSetup','commitAccount','saveStatus']);
}
console.log('Passed 9 onboarding transport lifecycle checks; no network or credentials used.');
