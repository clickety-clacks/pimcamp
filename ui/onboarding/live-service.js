/* Real account setup transport. No fixture fallback and no persistent secrets. */
(() => {
  'use strict';
  let csrf;
  let setupId;
  let oauthPending = Promise.resolve();
  async function call(action, payload = {}, signal) {
    if (!csrf) {
      const response = await fetch('/api/session', {cache: 'no-store', credentials: 'same-origin', signal});
      if (!response.ok) throw publicError('Reopen account setup through the local Pimcamp launcher.');
      csrf = (await response.json()).csrf;
    }
    const response = await fetch('/api/action', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
      headers: {'Content-Type': 'application/json', 'X-Pimcamp-CSRF': csrf},
      body: JSON.stringify({action, ...payload}),
    });
    const result = await response.json();
    if (!response.ok) throw publicError(result.error?.message || 'Setup could not complete this step.');
    return result;
  }
  function publicError(message) {
    const error = new Error(message);
    error.publicMessage = message;
    return error;
  }
  window.PimcampSetupService = {
    isDemo: false,
    listAccounts: () => call('listAccounts'),
    async beginSetup(accountId) {
      if (setupId) await call('cancelSetup', {setupId});
      const setup = await call('beginSetup', accountId ? {accountId} : {});
      setupId = setup.setupId;
      return setup;
    },
    checkIncoming: (draft, signal) => call('checkIncoming', {setupId, draft}, signal),
    checkOutgoing: (draft, signal) => call('checkOutgoing', {setupId, draft}, signal),
    commitAccount: (draft, signal) => call('commitAccount', {setupId, draft}, signal),
    observationReadiness: (accountId, signal) => call('observationReadiness', {accountId}, signal),
    checkAccount: accountId => call('checkAccount', {accountId}),
    async beginOAuth(_draft, signal) {
      // Open synchronously while the click still carries browser user activation.
      const popup = window.open('about:blank', '_blank', 'popup,width=540,height=720');
      if (!popup) throw publicError('Allow the Google sign-in window in your browser, then try again.');
      popup.opener = null;
      const currentSetup = setupId;
      const previous = oauthPending;
      let release;
      oauthPending = new Promise(resolve => { release = resolve; });
      let started = false;
      let completed = false;
      try {
        // A retry must not overtake the cancelled attempt's credential cleanup.
        await previous;
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        // Do not abandon the start request: fetch cancellation cannot roll back
        // server-side credential creation. Wait for its receipt, then cancel.
        const result = await call('beginOAuth', {setupId: currentSetup});
        started = true;
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        const destination = new URL(result.authorizationUrl);
        if (destination.origin !== 'https://accounts.google.com') throw publicError('The Google sign-in address was not valid.');
        popup.location = destination.href;
        const deadline = Date.now() + 900000;
        while (Date.now() < deadline) {
          if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
          const status = await call('oauthStatus', {setupId: currentSetup}, signal);
          if (status.status === 'failed') throw publicError('Google authorization could not be verified. Try again or check the installation’s Google application settings.');
          if (status.status !== 'in-progress') { completed = true; return status; }
          // Provider COOP policies can detach the window proxy and make closed
          // appear true while consent is still open. Only backend state or the
          // explicit Cancel action ends this authorization attempt.
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        return {status: 'expired'};
      } finally {
        try {
          if (!popup.closed) popup.close();
          if (started && (!completed || signal?.aborted)) await call('cancelOAuth', {setupId: currentSetup});
        } finally { release(); }
      }
    },
  };
})();
