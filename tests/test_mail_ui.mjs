import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import test from "node:test";

const require = createRequire(import.meta.url);
const mailText = require("../ui/mail/mail-text.js");
const mailClient = require("../ui/mail/mail-client.js");
const mailDrafts = require("../ui/mail/mail-drafts.js");
const mailState = mailDrafts;
const projectRoot = fileURLToPath(new URL("..", import.meta.url));

test("page references only locally provided same-origin assets", () => {
  const html = readFileSync(new URL("../ui/mail/index.html", import.meta.url), "utf8");
  const assets = [...html.matchAll(/(?:src|href)="\/mail\/assets\/([^\"]+)"/g)].map(match => match[1]);
  assert.deepEqual(assets.sort(), ["mail-app.js", "mail-client.js", "mail.css", "mail-drafts.js", "mail-text.js"].sort());
  for (const asset of assets) assert.equal(existsSync(join(projectRoot, "ui", "mail", asset)), true, asset);
  assert.match(html, /img-src 'none'/);
  assert.match(html, /id="source-reply-to-row"/);
  assert.match(html, /id="discard-draft"/);
  assert.match(html, /id="new-reply"/);
  assert.match(html, /id="logout-session"/);
  assert.match(html, /id="retry-open"/);
  const form = html.match(/<form\b[^>]*>([\s\S]*?)<\/form>/)?.[1] || "";
  assert.doesNotMatch(form, /\bname=/);
});

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function memoryIndexedDB() {
  const records = new Map();
  const stores = new Set();
  const database = {
    objectStoreNames: { contains: name => stores.has(name) },
    createObjectStore(name) {
      stores.add(name);
      return {};
    },
    transaction() {
      const transaction = {};
      const requestFor = (operation, value) => {
        const request = {};
        queueMicrotask(() => {
          request.result = operation(value);
          request.onsuccess?.();
          queueMicrotask(() => transaction.oncomplete?.());
        });
        return request;
      };
      transaction.objectStore = () => ({
        get: key => requestFor(() => records.get(key), key),
        put: record => requestFor(() => {
          records.set(record.key, record);
          return record.key;
        }, record),
        delete: key => requestFor(() => records.delete(key), key)
      });
      return transaction;
    }
  };
  return {
    open() {
      const request = {};
      queueMicrotask(() => {
        request.result = database;
        request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    }
  };
}

function mailAppFixture({
  storedDraft = null,
  statuses = [],
  prepares = [],
  submissions = [],
  activities = [],
  sessionResponses = [],
  logoutResponses = [],
  messageResponses = [],
  confirmResult = true,
  failPut = false,
  failDelete = false,
  navigationType = "navigate",
  initialVisibility = "visible",
  now: initialNow = Date.parse("2026-11-10T12:00:00Z"),
  sessionSnapshot = null
} = {}) {
  let currentTime = initialNow;
  const timers = new Map();
  let nextTimerId = 1;
  const setFixtureTimeout = (callback, delay) => {
    const id = nextTimerId++;
    timers.set(id, { callback, at: currentTime + Math.max(0, delay) });
    return id;
  };
  const clearFixtureTimeout = id => timers.delete(id);
  class FixtureDate extends Date { static now() { return currentTime; } }
  let savedDraft = storedDraft ? structuredClone(storedDraft) : undefined;
  const calls = { session: [], activity: [], logout: [], message: [], prepare: [], status: [], submit: [], puts: [], deletes: [] };
  const confirmPrompts = [];
  const activityQueue = [...activities];
  const sessionQueue = [...sessionResponses];
  const logoutQueue = [...logoutResponses];
  const messageQueue = [...messageResponses];
  const sessionStorageRecords = new Map();
  if (sessionSnapshot) sessionStorageRecords.set("pimcamp-mail-session-v2", JSON.stringify(sessionSnapshot));
  let activeAbsoluteDeadline = sessionSnapshot?.absolute_deadline || "";
  const store = {
    async get() { return savedDraft ? structuredClone(savedDraft) : undefined; },
    async put(value) {
      if (failPut) throw new Error("fixture storage write failed");
      savedDraft = mailDrafts.createDraftRecord(value);
      calls.puts.push(structuredClone(savedDraft));
      return structuredClone(savedDraft);
    },
    async delete(principalId, accountId, locator) {
      if (failDelete) throw new Error("fixture storage delete failed");
      calls.deletes.push([principalId, accountId, locator]);
      savedDraft = undefined;
    }
  };
  const client = {
    setCsrfToken() {},
    async openSession() {
      calls.session.push(true);
      const response = sessionQueue.shift();
      if (response instanceof Error) throw response;
      if (response) return structuredClone(response);
      activeAbsoluteDeadline = new Date(currentTime + 12 * 60 * 60 * 1000).toISOString();
      return {
        status: "authenticated",
        principal_id: "principal-1",
        csrf_token: "csrf-fixture",
        accounts: [{ account_id: "account-1", label: "Work" }],
        idle_deadline: new Date(currentTime + 30 * 60 * 1000).toISOString(),
        absolute_deadline: activeAbsoluteDeadline
      };
    },
    async activity() {
      calls.activity.push({ body: {} });
      const response = activityQueue.shift();
      if (response instanceof Error) throw response;
      if (response) return structuredClone(response);
      return {
        idle_deadline: new Date(Math.min(currentTime + 30 * 60 * 1000, Date.parse(activeAbsoluteDeadline))).toISOString(),
        absolute_deadline: activeAbsoluteDeadline
      };
    },
    async logout() {
      calls.logout.push(true);
      const response = logoutQueue.shift();
      if (response instanceof Error) throw response;
      return null;
    },
    async getMessage(locator) {
      calls.message.push(locator);
      const response = messageQueue.shift();
      if (response instanceof Error) throw response;
      return {
        account_id: "account-1",
        account_label: "Work",
        locator,
        source: {
          from: [{ address: "source@example.invalid" }],
          to: [{ address: "to@example.invalid" }],
          cc: [],
          reply_to: [{ address: "reply@example.invalid" }],
          subject: "Fixture",
          date: "2026-11-10T00:00:00Z"
        },
        body_parts: [{ media_type: "text/plain", content_utf8: "Fixture source" }]
      };
    },
    async prepareReply(locator, bodyText) {
      calls.prepare.push({ locator, bodyText });
      const response = prepares.shift();
      if (!response) throw new Error("no prepare fixture remains");
      return structuredClone(response);
    },
    async sendStatus(accountId, operationId) {
      calls.status.push({ accountId, operationId });
      const response = statuses.shift();
      if (response instanceof Error) throw response;
      return structuredClone(response || {
        account_id: accountId, operation_id: operationId, status: "not_submitted", retry_eligible: true
      });
    },
    async submitReply(operationId, payload) {
      calls.submit.push({ operationId, payload: structuredClone(payload) });
      return structuredClone(submissions.shift() || { status: "sent", operation_id: operationId });
    }
  };
  const elementIds = [
    "main", "gate", "gate-title", "gate-message", "gate-actions", "continue-session", "retry-open", "spinner", "logout-session",
    "mail-view", "draft-state", "notice", "source-heading", "account-badge", "source-from", "source-to",
    "source-cc-row", "source-cc", "source-reply-to-row", "source-reply-to", "source-date", "source-subject",
    "source-body", "composer-card", "composer-heading", "reply-form", "reply-from", "from-help", "reply-to",
    "reply-cc", "reply-bcc", "recipient-help", "reply-subject", "reply-body", "body-help", "review-heading",
    "review-from", "review-to", "review-cc", "review-bcc", "review-subject", "review-body", "composer-message",
    "send-reply", "discard-draft", "send-hint", "new-reply"
  ];
  const elements = new Map();
  class FakeElement {
    constructor(id = "") {
      this.id = id;
      this.hidden = id === "composer-card";
      this.disabled = false;
      this.value = "";
      this.textContent = "";
      this.dataset = {};
      this.children = [];
      this.listeners = new Map();
      this.elements = [];
    }
    addEventListener(type, listener) {
      const listeners = this.listeners.get(type) || [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    }
    async emit(type) {
      for (const listener of this.listeners.get(type) || []) {
        await listener({ preventDefault() {}, target: this });
      }
      if (this.form && ["input", "change"].includes(type)) {
        for (const listener of this.form.listeners.get(type) || []) {
          await listener({ preventDefault() {}, target: this });
        }
      }
    }
    replaceChildren(...children) {
      this.children = [...children];
      if (this.id === "reply-from") this.value = "";
    }
    append(child) { this.children.push(child); }
    add(child) { this.children.push(child); }
    querySelector() { return elements.get("spinner"); }
  }
  for (const id of elementIds) elements.set(id, new FakeElement(id));
  const form = elements.get("reply-form");
  form.elements = [
    "reply-from", "reply-to", "reply-cc", "reply-bcc", "reply-subject", "reply-body", "send-reply", "discard-draft"
  ].map(id => elements.get(id));
  for (const control of form.elements) control.form = form;
  class FakeOption extends FakeElement {
    constructor(text, value) {
      super();
      this.textContent = text;
      this.value = value;
      this.dataset = {};
    }
  }
  const window = {
    location: { pathname: "/mail/m/locator-1" },
    performance: { getEntriesByType: () => [{ type: navigationType }] },
    sessionStorage: {
      getItem(key) { return sessionStorageRecords.get(key) ?? null; },
      setItem(key, value) { sessionStorageRecords.set(key, value); },
      removeItem(key) { sessionStorageRecords.delete(key); }
    },
    listeners: new Map(),
    addEventListener(type, listener) {
      const listeners = this.listeners.get(type) || [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    },
    async emit(type, event = {}) {
      for (const listener of this.listeners.get(type) || []) await listener(event);
    },
    confirm(message) { confirmPrompts.push(message); return confirmResult; },
    PimcampMailClient: { createMailClient: () => client },
    PimcampMailText: mailText,
    PimcampMailDrafts: { ...mailDrafts, createDraftStore: () => store },
    PimcampMailState: mailState
  };
  const document = {
    visibilityState: initialVisibility,
    listeners: new Map(),
    addEventListener(type, listener) {
      const listeners = this.listeners.get(type) || [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    },
    async emit(type, event = {}) {
      for (const listener of this.listeners.get(type) || []) await listener(event);
    },
    getElementById(id) { return elements.get(id); },
    createElement() { return new FakeElement(); }
  };
  runInNewContext(readFileSync(new URL("../ui/mail/mail-app.js", import.meta.url), "utf8"), {
    window,
    document,
    Option: FakeOption,
    crypto: { randomUUID: () => "fresh-draft-id" },
    Date: FixtureDate,
    setTimeout: setFixtureTimeout,
    clearTimeout: clearFixtureTimeout
  });
  async function settle() {
    for (let i = 0; i < 12; i += 1) await new Promise(resolve => setImmediate(resolve));
  }
  async function advance(ms) {
    const target = currentTime + ms;
    while (true) {
      const due = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, timer] = due;
      timers.delete(id);
      currentTime = timer.at;
      timer.callback();
      await settle();
    }
    currentTime = target;
    await settle();
  }
  return {
    elements, calls, confirmPrompts, sessionStorageRecords,
    get savedDraft() { return savedDraft; }, settle, advance,
    setVisibility(value) { document.visibilityState = value; return document.emit("visibilitychange"); }
  };
}

function preparedMailReply(operationId, {
  fromChoices = [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }],
  selectedFrom = fromChoices.length === 1 ? fromChoices[0] : null,
  to = [{ name: null, address: "reply@example.invalid" }],
  bodyText = ""
} = {}) {
  return {
    operation_id: operationId,
    account_id: "account-1",
    account_label: "Work",
    locator: "locator-1",
    source: {
      from: [{ address: "source@example.invalid" }],
      to: [],
      cc: [],
      reply_to: [{ address: "reply@example.invalid" }],
      subject: "Fixture",
      date: "2026-11-10T00:00:00Z"
    },
    reply: {
      from_choices: fromChoices,
      selected_from: selectedFrom,
      to,
      cc: [],
      bcc: [],
      subject: "Re: Fixture",
      body_text: bodyText
    }
  };
}

test("HTML-only source becomes inert readable text without remote or executable markup", () => {
  const html = [
    "<!doctype html><h1>Project &amp; schedule</h1>",
    "<p>Meet at 10&nbsp;am.</p>",
    '<img src="https://remote.invalid/pixel.gif" alt="remote image">',
    '<script>window.location="https://remote.invalid/send"</script>',
    '<style>@import url("https://remote.invalid/theme.css")</style>',
    '<form action="https://remote.invalid/submit"><button>Send</button></form>',
    '<iframe src="https://remote.invalid/frame">frame text</iframe>',
    "<p>See you soon.</p>"
  ].join("");
  const rendered = mailText.htmlToText(html);

  assert.match(rendered, /Project & schedule/);
  assert.match(rendered, /Meet at 10\u00a0am\./);
  assert.match(rendered, /See you soon\./);
  assert.doesNotMatch(rendered, /<[^>]*>|remote\.invalid|window\.location|frame text|Send/);
});

test("source text prefers a plain part and falls back to HTML extraction", () => {
  assert.equal(mailText.sourceText({ body_parts: [
    { media_type: "text/html", content_utf8: "<p>HTML version</p>" },
    { media_type: "text/plain", content_utf8: "Plain version" }
  ] }), "Plain version");
  assert.equal(mailText.sourceText({ body_parts: [
    { media_type: "text/html", content_utf8: "<p>Readable &amp; safe</p>" }
  ] }), "Readable & safe");
});

test("mail client uses the agreed same-origin routes and read-only status lookup", async () => {
  const calls = [];
  const responses = [
    {
      status: "authenticated", principal_id: "principal-1", csrf_token: "csrf-fixture",
      accounts: [{ account_id: "account-1", label: "Work" }],
      idle_deadline: "2026-11-10T12:30:00Z", absolute_deadline: "2026-11-11T00:00:00Z"
    },
    {
      account_id: "account-1", account_label: "Work", locator: "opaque locator",
      source: { from: [], to: [], cc: [], reply_to: [], subject: "Fixture", date: "2026-11-10T00:00:00Z" },
      body_parts: [{ media_type: "text/plain", content_utf8: "Source body" }]
    },
    {
      operation_id: "33333333-3333-4333-8333-333333333333", account_id: "account-1", account_label: "Work", locator: "opaque locator",
      source: { from: [], to: [], cc: [], subject: "Fixture", date: "2026-11-10T00:00:00Z" },
      reply: { from_choices: [], selected_from: null, to: [], cc: [], bcc: [], subject: "Re: Fixture", body_text: "draft text" }
    },
    { account_id: "account-1", operation_id: "33333333-3333-4333-8333-333333333333", status: "unknown", retry_eligible: false }
  ];
  const client = mailClient.createMailClient({
    csrfToken: "csrf-fixture",
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return jsonResponse(responses.shift());
    }
  });

  const session = await client.openSession();
  const message = await client.getMessage("opaque locator");
  const prepared = await client.prepareReply("opaque locator", "draft text");
  const status = await client.sendStatus("account-1", "33333333-3333-4333-8333-333333333333");

  assert.equal(session.status, "authenticated");
  assert.equal(session.accounts[0].label, "Work");
  assert.deepEqual(message.source.reply_to, []);
  assert.equal(mailText.sourceText(message), "Source body");
  assert.equal(prepared.reply.selected_from, null);
  assert.equal(prepared.operation_id, "33333333-3333-4333-8333-333333333333");
  assert.equal(status.status, "unknown");
  assert.equal(status.retry_eligible, false);
  assert.deepEqual(calls.map(call => [call.options.method, call.path]), [
    ["POST", "/mail/api/session"],
    ["GET", "/mail/api/v2/messages/opaque%20locator"],
    ["POST", "/mail/api/v2/replies/prepare"],
    ["GET", "/mail/api/v2/accounts/account-1/send-status/33333333-3333-4333-8333-333333333333"]
  ]);
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[2].options.headers["X-Pimcamp-CSRF"], "csrf-fixture");
  assert.deepEqual(JSON.parse(calls[2].options.body), { locator: "opaque locator", body_text: "draft text" });
  assert.ok(calls.every(call => !call.path.endsWith("/submit")));
  await assert.rejects(client.getMessage("bad/locator"), { code: "invalid_request" });
});

test("mail session activity renews only idle deadline and logout requires a 204 response", async () => {
  const calls = [];
  const absoluteDeadline = "2026-11-11T00:00:00Z";
  const responses = [
    {
      status: "authenticated", principal_id: "principal-1", csrf_token: "csrf-fixture",
      accounts: [{ account_id: "account-1", label: "Work" }],
      idle_deadline: "2026-11-10T12:30:00Z", absolute_deadline: absoluteDeadline
    },
    { idle_deadline: "2026-11-10T12:45:00Z", absolute_deadline: absoluteDeadline }
  ];
  const client = mailClient.createMailClient({
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      if (path.endsWith("/logout")) return { ok: true, status: 204, json: async () => { throw new Error("no content"); } };
      return jsonResponse(responses.shift());
    }
  });

  await client.openSession();
  client.setCsrfToken("csrf-fixture");
  const activity = await client.activity();
  await client.logout();

  assert.equal(activity.idle_deadline, "2026-11-10T12:45:00Z");
  assert.equal(activity.absolute_deadline, absoluteDeadline);
  assert.deepEqual(calls.map(call => [call.options.method, call.path]), [
    ["POST", "/mail/api/session"],
    ["POST", "/mail/api/session/activity"],
    ["POST", "/mail/api/session/logout"]
  ]);
  for (const call of calls.slice(1)) {
    assert.equal(call.options.headers["X-Pimcamp-CSRF"], "csrf-fixture");
    assert.deepEqual(JSON.parse(call.options.body), {});
    assert.equal(call.options.credentials, "same-origin");
    assert.equal(call.options.cache, "no-store");
  }

  const invalid = mailClient.createMailClient({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({}) }) });
  invalid.setCsrfToken("csrf-fixture");
  await assert.rejects(invalid.logout(), { code: "invalid_response" });
});

test("mail client preserves flat v2 errors and distinguishes expired sessions from permission denial", async () => {
  const errors = [
    { contract_version: "pimcamp.v2", code: "session_expired", retryable: false, message: "Continue mail session." },
    { contract_version: "pimcamp.v2", code: "permission_denied", retryable: false, message: "This account is unavailable." }
  ];
  const statuses = [401, 403];
  const client = mailClient.createMailClient({
    fetchImpl: async () => jsonResponse(errors.shift(), statuses.shift())
  });

  const expired = await client.getMessage("loc-1").catch(error => error);
  const denied = await client.getMessage("loc-1").catch(error => error);
  assert.equal(expired.code, "session_expired");
  assert.equal(expired.status, 401);
  assert.equal(expired.contractVersion, "pimcamp.v2");
  assert.equal(expired.retryable, false);
  assert.equal(denied.code, "permission_denied");
  assert.equal(denied.status, 403);
  assert.equal(denied.contractVersion, "pimcamp.v2");
  assert.equal(denied.retryable, false);
});

test("mail client requires the authenticated v2 session response", async () => {
  const client = mailClient.createMailClient({ fetchImpl: async () => jsonResponse({
    status: "unauthenticated", principal_id: "principal-1", csrf_token: "csrf", accounts: []
  }) });
  const result = await client.openSession().catch(error => error);
  assert.equal(result.code, "invalid_response");
});

test("explicit submit call posts the reviewed recipient and body snapshot", async () => {
  const calls = [];
  const client = mailClient.createMailClient({
    csrfToken: "csrf-fixture",
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return jsonResponse({ status: "sent", operation_id: "op-123" });
    }
  });
  const reviewedReply = {
    account_id: "account-2",
    locator: "locator-2",
    from: { name: "Work", address: "work@example.invalid" },
    to: [{ name: null, address: "reply@example.invalid" }],
    cc: [],
    bcc: [],
    subject: "Re: review",
    body_text: "Reviewed plain text"
  };

  const result = await client.submitReply("op-123", reviewedReply);
  assert.equal(result.status, "sent");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].path, "/mail/api/v2/replies/op-123/submit");
  assert.equal(calls[0].options.headers["X-Pimcamp-CSRF"], "csrf-fixture");
  assert.deepEqual(JSON.parse(calls[0].options.body), reviewedReply);
});

test("lost submit response is unknown and is never retried by the client", async () => {
  let attempts = 0;
  const client = mailClient.createMailClient({
    csrfToken: "csrf-fixture",
    fetchImpl: async () => {
      attempts += 1;
      throw new Error("fixture transport disconnected");
    }
  });
  const result = await client.submitReply("op-once", {
    account_id: "account-1", locator: "loc-1", from: {}, to: [], cc: [], bcc: [], subject: "", body_text: ""
  }).catch(error => error);

  assert.equal(result.code, "outcome_unknown");
  assert.equal(attempts, 1);
});

test("redirected API response requires mail-session renewal without exposing redirect content", async () => {
  const client = mailClient.createMailClient({
    fetchImpl: async () => ({ redirected: true, ok: true, status: 200, json: async () => ({ html: "login page" }) })
  });
  const result = await client.getMessage("loc-1").catch(error => error);
  assert.equal(result.code, "session_expired");
  assert.equal(result.status, 401);
  assert.equal(result.payload, null);
});

test("draft persistence whitelists same-browser content and retains operation identity", () => {
  const operationPayload = {
    account_id: "account-1",
    locator: "locator-1",
    from: { name: "Work", address: "work@example.invalid" },
    to: [{ address: "reply@example.invalid" }],
    cc: [],
    bcc: [],
    subject: "Re: fixture",
    body_text: "Attempted reply"
  };
  const draft = mailDrafts.createDraftRecord({
    principal_id: "principal-1",
    account_id: "account-1",
    locator: "locator-1",
    draft_id: "draft-1",
    from_choices: [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }],
    from: { name: "Work", address: "work@example.invalid" },
    to: [{ address: "reply@example.invalid" }],
    cc: [],
    bcc: [],
    subject: "Re: fixture",
    body_text: "Keep this reply",
    operation_id: "op-1",
    operation_status: "unknown",
    operation_payload: operationPayload,
    source_body: "must not persist",
    csrf_token: "must not persist",
    session_cookie: "must not persist",
    password: "must not persist"
  }, 1234);

  assert.equal(draft.key, JSON.stringify(["principal-1", "account-1", "locator-1"]));
  assert.deepEqual(draft.from_choices, [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }]);
  assert.equal(draft.operation_id, "op-1");
  assert.equal(draft.operation_status, "unknown");
  assert.deepEqual(draft.operation_payload, {
    ...operationPayload,
    to: [{ name: null, address: "reply@example.invalid" }]
  });
  assert.equal(draft.body_text, "Keep this reply");
  assert.equal(draft.updated_at, 1234);
  assert.equal("source_body" in draft, false);
  assert.equal("csrf_token" in draft, false);
  assert.equal("session_cookie" in draft, false);
  assert.equal("password" in draft, false);
});

test("draft store restores only the matching principal, account, and locator", async () => {
  const indexedDBImpl = memoryIndexedDB();
  const firstPage = mailDrafts.createDraftStore({ indexedDBImpl, now: () => 42 });
  await firstPage.put({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1",
    from_choices: [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }],
    from: { address: "from@example.invalid" }, to: [{ address: "to@example.invalid" }],
    cc: [], bcc: [], subject: "Re: fixture", body_text: "Saved text",
    operation_id: "op-restore", operation_status: "unknown", operation_attempted: true,
    operation_payload: {
      account_id: "account-1", locator: "locator-1",
      from: { address: "from@example.invalid" }, to: [{ address: "to@example.invalid" }],
      cc: [], bcc: [], subject: "Re: fixture", body_text: "Saved text"
    }
  });

  const refreshedPage = mailDrafts.createDraftStore({ indexedDBImpl, now: () => 43 });
  const restored = await refreshedPage.get("principal-1", "account-1", "locator-1");
  const wrongAccount = await refreshedPage.get("principal-1", "account-2", "locator-1");
  const wrongPrincipal = await refreshedPage.get("principal-2", "account-1", "locator-1");

  assert.equal(restored.body_text, "Saved text");
  assert.deepEqual(restored.from_choices, [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }]);
  assert.equal(restored.operation_id, "op-restore");
  assert.equal(restored.operation_status, "unknown");
  assert.equal(restored.operation_payload.body_text, "Saved text");
  assert.equal(wrongAccount, undefined);
  assert.equal(wrongPrincipal, undefined);
  await refreshedPage.delete("principal-1", "account-1", "locator-1");
  assert.equal(await refreshedPage.get("principal-1", "account-1", "locator-1"), undefined);
});

test("discarded attempted drafts retain only the operation marker", () => {
  const marker = mailDrafts.createDraftRecord({
    principal_id: "principal-1",
    account_id: "account-1",
    locator: "locator-1",
    draft_id: "draft-1",
    operation_id: "op-uncertain",
    operation_status: "unknown",
    operation_attempted: true,
    discarded: true,
    from_choices: [{ address: "work@example.invalid" }],
    from: { address: "work@example.invalid" },
    to: [{ address: "reply@example.invalid" }],
    cc: [],
    bcc: [],
    subject: "Private subject",
    body_text: "Private text",
    operation_payload: { body_text: "Private attempted text" }
  }, 99);

  assert.equal(marker.discarded, true);
  assert.equal(marker.operation_attempted, true);
  assert.equal(marker.operation_id, "op-uncertain");
  assert.equal(marker.operation_status, "unknown");
  assert.equal(marker.updated_at, 99);
  for (const key of ["from_choices", "from", "to", "cc", "bcc", "subject", "body_text", "operation_payload"]) {
    assert.equal(key in marker, false, key);
  }
});

test("mail app restores verified From choices and operation without preparing or sending again", async () => {
  const fromChoices = [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }];
  const payload = {
    account_id: "account-1",
    locator: "locator-1",
    from: { name: "Work", address: "work@example.invalid" },
    to: [{ name: null, address: "reply@example.invalid" }],
    cc: [],
    bcc: [],
    subject: "Re: Fixture",
    body_text: "Saved reply"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-restore",
    from_choices: fromChoices, ...payload, operation_id: "op-restore", operation_status: "unknown",
    operation_attempted: true, operation_payload: payload
  });
  const app = mailAppFixture({
    storedDraft,
    statuses: [{ status: "not_submitted", retry_eligible: true }]
  });

  await app.settle();

  const from = app.elements.get("reply-from");
  assert.equal(app.elements.get("gate").hidden, true);
  assert.equal(app.elements.get("mail-view").hidden, false);
  assert.equal(app.elements.get("account-badge").textContent, "Account · Work");
  assert.equal(app.elements.get("source-subject").textContent, "Fixture");
  assert.ok(from.children.some(option => option.value === "work@example.invalid"));
  assert.equal(from.value, "work@example.invalid");
  assert.equal(app.elements.get("reply-body").value, "Saved reply");
  assert.equal(app.elements.get("send-reply").textContent, "Retry send");
  assert.equal(app.elements.get("send-reply").disabled, false);
  assert.equal(app.calls.prepare.length, 0);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.calls.status[0].operationId, "op-restore");
});

test("reauthentication restores the same draft and operation without preparing or sending", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const payload = {
    account_id: "account-1", locator: "locator-1",
    from: { name: choice.name, address: choice.address },
    to: [{ name: null, address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Keep this during reauthentication"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-reauth",
    from_choices: [choice], ...payload, operation_id: "op-reauth", operation_status: "unknown",
    operation_attempted: true, operation_payload: payload
  });
  const expired = Object.assign(new Error("expired fixture session"), { status: 401, code: "session_expired" });
  const app = mailAppFixture({
    storedDraft,
    statuses: [expired, { status: "not_submitted", retry_eligible: true }]
  });
  await app.settle();

  assert.equal(app.elements.get("gate-title").textContent, "Mail session expired");
  assert.equal(app.elements.get("mail-view").hidden, true);
  assert.equal(app.elements.get("reply-body").value, "");
  assert.equal(app.calls.submit.length, 0);

  await app.elements.get("continue-session").emit("click");
  await app.settle();

  assert.equal(app.calls.session.length, 2);
  assert.deepEqual(app.calls.message, ["locator-1", "locator-1"]);
  assert.equal(app.elements.get("mail-view").hidden, false);
  assert.equal(app.elements.get("reply-body").value, payload.body_text);
  assert.equal(app.elements.get("reply-from").value, choice.address);
  assert.equal(app.elements.get("send-reply").textContent, "Retry send");
  assert.equal(app.calls.prepare.length, 0);
  assert.deepEqual(app.calls.status.map(call => call.operationId), ["op-reauth", "op-reauth"]);
  assert.equal(app.calls.submit.length, 0);
});

test("refresh requires a deliberate session action when the tab has no cached session", async () => {
  const app = mailAppFixture({
    navigationType: "reload",
    prepares: [preparedMailReply("op-after-refresh")]
  });
  await app.settle();

  assert.equal(app.calls.session.length, 0);
  assert.equal(app.calls.message.length, 0);
  assert.equal(app.elements.get("gate-title").textContent, "Mail session needed");
  assert.equal(app.elements.get("continue-session").hidden, false);

  await app.elements.get("continue-session").emit("click");
  await app.settle();
  assert.equal(app.calls.session.length, 1);
  assert.deepEqual(app.calls.message, ["locator-1"]);
  assert.equal(app.calls.submit.length, 0);
});

test("a background-opened tab waits for foreground before creating a session or reading mail", async () => {
  const app = mailAppFixture({
    initialVisibility: "hidden",
    prepares: [preparedMailReply("op-background-open")]
  });
  await app.settle();
  assert.equal(app.elements.get("gate-title").textContent, "Mail tab is paused");
  assert.equal(app.calls.session.length, 0);
  assert.equal(app.calls.message.length, 0);

  await app.setVisibility("visible");
  await app.settle();
  assert.equal(app.calls.session.length, 1);
  assert.deepEqual(app.calls.message, ["locator-1"]);
  assert.equal(app.calls.activity.length, 0);
  assert.equal(app.calls.submit.length, 0);
});

test("restored tab keeps its absolute deadline and foreground navigation does not create a session", async () => {
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-refresh",
    from_choices: [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }],
    from: { name: "Work", address: "work@example.invalid" }, to: [{ address: "reply@example.invalid" }],
    cc: [], bcc: [], subject: "Re: Fixture", body_text: "Saved through refresh",
    operation_id: "op-refresh", operation_status: "unknown", operation_attempted: true
  });
  const snapshot = {
    status: "authenticated", principal_id: "principal-1", csrf_token: "csrf-cached",
    accounts: [{ account_id: "account-1", label: "Work" }],
    idle_deadline: "2026-11-10T12:30:00Z", absolute_deadline: "2026-11-11T00:00:00Z"
  };
  const app = mailAppFixture({
    navigationType: "reload", sessionSnapshot: snapshot, storedDraft,
    statuses: [{ status: "not_submitted", retry_eligible: true }]
  });
  await app.settle();

  assert.equal(app.calls.session.length, 0);
  assert.equal(app.calls.activity.length, 1);
  assert.equal(app.calls.activity[0].body && Object.keys(app.calls.activity[0].body).length, 0);
  assert.equal(app.calls.message.length, 1);
  assert.equal(app.elements.get("reply-body").value, "Saved through refresh");
  const savedSession = JSON.parse(app.sessionStorageRecords.get("pimcamp-mail-session-v2"));
  assert.equal(savedSession.absolute_deadline, snapshot.absolute_deadline);
  assert.equal(savedSession.idle_deadline, "2026-11-10T12:30:00.000Z");
  assert.equal(app.calls.submit.length, 0);
});

test("only foreground edits renew idle; status and visibility checks do not", async () => {
  const app = mailAppFixture({
    prepares: [preparedMailReply("op-activity", { bodyText: "Local body" })],
    statuses: [
      { status: "not_submitted", retry_eligible: true },
      { status: "not_submitted", retry_eligible: true }
    ]
  });
  await app.settle();
  assert.equal(app.calls.activity.length, 0);

  app.elements.get("reply-body").value = "Edited locally";
  await app.elements.get("reply-body").emit("input");
  await app.advance(500);
  assert.equal(app.calls.activity.length, 1);
  assert.deepEqual(app.calls.activity[0].body, {});
  assert.equal(app.calls.submit.length, 0);

  await app.setVisibility("hidden");
  await app.setVisibility("visible");
  await app.settle();
  assert.equal(app.calls.activity.length, 1);
  assert.equal(app.calls.message.length, 2);
  assert.equal(app.calls.submit.length, 0);
});

test("session expires at deadline equality, hiding content while retaining draft and operation", async () => {
  const app = mailAppFixture({
    prepares: [preparedMailReply("op-expiry", { bodyText: "Keep across expiry" })]
  });
  await app.settle();
  const operationId = app.savedDraft.operation_id;
  await app.advance(29 * 60 * 1000);
  assert.equal(app.elements.get("mail-view").hidden, false);
  assert.equal(app.calls.session.length, 1);

  await app.advance(60 * 1000);
  assert.equal(app.elements.get("mail-view").hidden, true);
  assert.equal(app.elements.get("gate-title").textContent, "Mail session expired");
  assert.equal(app.savedDraft.operation_id, operationId);
  assert.equal(app.savedDraft.body_text, "Keep across expiry");
  assert.equal(app.sessionStorageRecords.has("pimcamp-mail-session-v2"), false);
  assert.equal(app.calls.activity.length, 0);
  assert.equal(app.calls.submit.length, 0);
});

test("an activity response cannot extend the absolute session deadline or authorize Send", async () => {
  const app = mailAppFixture({
    prepares: [preparedMailReply("op-absolute-cap")],
    activities: [{ idle_deadline: "2026-11-10T12:45:00Z", absolute_deadline: "2026-11-11T00:01:00Z" }]
  });
  await app.settle();
  const operationId = app.savedDraft.operation_id;

  await app.elements.get("reply-form").emit("submit");
  await app.settle();

  assert.equal(app.calls.activity.length, 1);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.savedDraft.operation_id, operationId);
  assert.equal(app.elements.get("gate-title").textContent, "Mail session expired");
  assert.equal(app.sessionStorageRecords.has("pimcamp-mail-session-v2"), false);
});

test("explicit logout invalidates the tab session while retaining local draft and operation", async () => {
  const app = mailAppFixture({
    prepares: [preparedMailReply("op-logout", { bodyText: "Preserve on logout" })]
  });
  await app.settle();
  const operationId = app.savedDraft.operation_id;

  await app.elements.get("logout-session").emit("click");
  await app.settle();

  assert.equal(app.calls.activity.length, 1);
  assert.equal(app.calls.logout.length, 1);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.elements.get("mail-view").hidden, true);
  assert.equal(app.elements.get("gate-title").textContent, "Mail session ended");
  assert.equal(app.savedDraft.operation_id, operationId);
  assert.equal(app.savedDraft.body_text, "Preserve on logout");
  assert.equal(app.sessionStorageRecords.has("pimcamp-mail-session-v2"), false);
});

test("uncertain logout stays gated across reload and preserves the operation for explicit retry", async () => {
  const networkError = new Error("logout response unavailable");
  const first = mailAppFixture({
    prepares: [preparedMailReply("op-logout-unknown", { bodyText: "Keep across uncertain logout" })],
    logoutResponses: [networkError]
  });
  await first.settle();
  const operationId = first.savedDraft.operation_id;

  await first.elements.get("logout-session").emit("click");
  await first.settle();

  assert.equal(first.elements.get("mail-view").hidden, true);
  assert.equal(first.elements.get("gate-title").textContent, "Logout not confirmed");
  assert.equal(first.savedDraft.operation_id, operationId);
  assert.equal(first.sessionStorageRecords.has("pimcamp-mail-session-v2"), true);
  const pendingSnapshot = JSON.parse(first.sessionStorageRecords.get("pimcamp-mail-session-v2"));
  assert.equal(pendingSnapshot.logout_pending, true);
  assert.equal(pendingSnapshot.accounts.length, 1);

  const reload = mailAppFixture({
    navigationType: "reload",
    sessionSnapshot: pendingSnapshot,
    storedDraft: first.savedDraft
  });
  await reload.settle();

  assert.equal(reload.calls.session.length, 0);
  assert.equal(reload.calls.activity.length, 0);
  assert.equal(reload.calls.message.length, 0);
  assert.equal(reload.elements.get("mail-view").hidden, true);
  assert.equal(reload.elements.get("gate-title").textContent, "Logout not confirmed");
  assert.equal(reload.elements.get("retry-open").textContent, "Try logout again");

  await reload.elements.get("retry-open").emit("click");
  await reload.settle();

  assert.equal(reload.calls.activity.length, 1);
  assert.equal(reload.calls.logout.length, 1);
  assert.equal(reload.calls.session.length, 0);
  assert.equal(reload.calls.message.length, 0);
  assert.equal(reload.savedDraft.operation_id, operationId);
  assert.equal(reload.savedDraft.body_text, "Keep across uncertain logout");
  assert.equal(reload.sessionStorageRecords.has("pimcamp-mail-session-v2"), false);
  assert.equal(reload.elements.get("gate-title").textContent, "Mail session ended");
});

test("expired uncertain logout renewal retries session opening instead of repeating logout", async () => {
  const first = mailAppFixture({
    prepares: [preparedMailReply("op-logout-expiry", { bodyText: "Keep through expiry" })],
    logoutResponses: [new Error("logout response unavailable")]
  });
  await first.settle();
  await first.elements.get("logout-session").emit("click");
  await first.settle();
  const pendingSnapshot = JSON.parse(first.sessionStorageRecords.get("pimcamp-mail-session-v2"));

  const reload = mailAppFixture({
    navigationType: "reload",
    sessionSnapshot: pendingSnapshot,
    storedDraft: first.savedDraft,
    sessionResponses: [new Error("temporary session service outage")]
  });
  await reload.settle();
  await reload.advance(30 * 60 * 1000);
  assert.equal(reload.elements.get("gate-title").textContent, "Mail session expired");

  await reload.elements.get("continue-session").emit("click");
  await reload.settle();
  assert.equal(reload.calls.session.length, 1);
  assert.equal(reload.calls.logout.length, 0);
  assert.equal(reload.elements.get("gate-title").textContent, "Mail could not be reached");

  await reload.elements.get("retry-open").emit("click");
  await reload.settle();
  assert.equal(reload.calls.session.length, 2);
  assert.equal(reload.calls.logout.length, 0);
  assert.deepEqual(reload.calls.message, ["locator-1"]);
  assert.equal(reload.savedDraft.operation_id, "op-logout-expiry");
  assert.equal(reload.calls.submit.length, 0);
});

test("403 permission denial stays an authorization error and does not offer session renewal", async () => {
  const denied = Object.assign(new Error("fixture authorization denial"), { status: 403, code: "permission_denied" });
  const app = mailAppFixture({ messageResponses: [denied] });
  await app.settle();

  assert.equal(app.elements.get("gate-title").textContent, "Message could not be opened");
  assert.equal(app.elements.get("continue-session").hidden, true);
  assert.equal(app.elements.get("retry-open").hidden, false);
  assert.equal(app.elements.get("mail-view").hidden, true);
  assert.equal(app.calls.session.length, 1);
});

test("403 permission denial during status lookup hides private content and is not mislabeled as unavailable status", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-status-denied",
    from_choices: [choice], from: choice, to: [{ address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Preserve this reply", operation_id: "op-status-denied",
    operation_status: "pending", operation_attempted: true
  });
  const denied = Object.assign(new Error("fixture authorization denial"), { status: 403, code: "permission_denied" });
  const app = mailAppFixture({ storedDraft, statuses: [denied] });
  await app.settle();

  assert.equal(app.elements.get("gate-title").textContent, "Mail account unavailable");
  assert.match(app.elements.get("gate-message").textContent, /not available to the current session/);
  assert.equal(app.elements.get("mail-view").hidden, true);
  assert.equal(app.elements.get("source-body").textContent, "");
  assert.equal(app.savedDraft.operation_id, "op-status-denied");
  assert.equal(app.savedDraft.body_text, "Preserve this reply");
  assert.equal(app.savedDraft.operation_status, "unknown");
  assert.equal(app.elements.get("send-reply").disabled, true);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.elements.get("continue-session").hidden, true);
});

test("a rejected tab session snapshot clears on 403 activity and continues only after human action", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const operationPayload = {
    account_id: "account-1", locator: "locator-1", from: choice,
    to: [{ address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Preserve across tabs"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-stale-tab",
    from_choices: [choice], ...operationPayload, operation_id: "op-stale-tab", operation_status: "unknown",
    operation_attempted: true, operation_payload: operationPayload
  });
  const snapshot = {
    status: "authenticated", principal_id: "principal-1", csrf_token: "csrf-old-tab",
    accounts: [{ account_id: "account-1", label: "Work" }],
    idle_deadline: "2026-11-10T12:30:00Z", absolute_deadline: "2026-11-11T00:00:00Z"
  };
  const staleSession = Object.assign(new Error("the browser cookie now names another tab's session"), {
    status: 403, code: "permission_denied", retryable: false
  });
  const app = mailAppFixture({
    navigationType: "reload", sessionSnapshot: snapshot, storedDraft,
    activities: [staleSession],
    statuses: [{ status: "not_submitted", retry_eligible: true }]
  });
  await app.settle();

  assert.equal(app.calls.session.length, 0);
  assert.equal(app.calls.activity.length, 1);
  assert.equal(app.calls.message.length, 0);
  assert.equal(app.calls.status.length, 0);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.sessionStorageRecords.has("pimcamp-mail-session-v2"), false);
  assert.equal(app.elements.get("mail-view").hidden, true);
  assert.equal(app.elements.get("source-body").textContent, "");
  assert.equal(app.elements.get("gate-title").textContent, "Mail session needs attention");
  assert.equal(app.elements.get("continue-session").hidden, false);
  assert.equal(app.savedDraft.operation_id, "op-stale-tab");
  assert.equal(app.savedDraft.body_text, "Preserve across tabs");

  await app.elements.get("continue-session").emit("click");
  await app.settle();

  assert.equal(app.calls.session.length, 1);
  assert.equal(app.calls.message.length, 1);
  assert.deepEqual(app.calls.status.map(call => call.operationId), ["op-stale-tab"]);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.savedDraft.operation_id, "op-stale-tab");
  assert.equal(app.savedDraft.body_text, "Preserve across tabs");
  assert.equal(app.elements.get("send-reply").textContent, "Retry send");
});

test("expired activity never submits and renewal checks the same operation before a later explicit send", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const payload = {
    account_id: "account-1", locator: "locator-1",
    from: { name: choice.name, address: choice.address },
    to: [{ name: null, address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Reviewed reply"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-expired-send",
    from_choices: [choice], ...payload, operation_id: "op-expired-send", operation_status: "unknown",
    operation_attempted: true, operation_payload: payload
  });
  const expired = Object.assign(new Error("expired fixture session"), { status: 401, code: "session_expired" });
  const app = mailAppFixture({
    storedDraft,
    activities: [expired],
    statuses: [
      { status: "not_submitted", retry_eligible: true },
      { status: "not_submitted", retry_eligible: true }
    ],
    submissions: [{ status: "sent", operation_id: "op-expired-send" }]
  });
  await app.settle();
  assert.equal(app.elements.get("send-reply").textContent, "Retry send");

  await app.elements.get("reply-form").emit("submit");
  await app.settle();
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.savedDraft.operation_id, "op-expired-send");
  assert.equal(app.savedDraft.body_text, "Reviewed reply");
  assert.equal(app.elements.get("gate-title").textContent, "Mail session expired");

  await app.elements.get("continue-session").emit("click");
  await app.settle();
  assert.deepEqual(app.calls.status.map(call => call.operationId), ["op-expired-send", "op-expired-send"]);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.elements.get("send-reply").textContent, "Retry send");

  await app.elements.get("reply-form").emit("submit");
  await app.settle();
  assert.equal(app.calls.submit.length, 1);
  assert.equal(app.calls.submit[0].operationId, "op-expired-send");
  assert.equal(app.calls.activity.length, 2);
});

test("discard before an attempt deletes the draft and sends no mail", async () => {
  const app = mailAppFixture({
    prepares: [preparedMailReply("op-unsent", { bodyText: "" })],
    statuses: [{ status: "not_submitted", retry_eligible: true }]
  });
  await app.settle();
  app.elements.get("reply-body").value = "Discard me";
  await app.elements.get("reply-body").emit("input");
  await app.settle();

  await app.elements.get("discard-draft").emit("click");
  await app.settle();

  assert.equal(app.confirmPrompts.length, 1);
  assert.match(app.confirmPrompts[0], /No email will be sent/);
  assert.deepEqual(app.calls.deletes, [["principal-1", "account-1", "locator-1"]]);
  assert.equal(app.savedDraft, undefined);
  assert.equal(app.elements.get("reply-body").value, "");
  assert.equal(app.elements.get("reply-form").hidden, true);
  assert.equal(app.elements.get("new-reply").hidden, false);
  assert.equal(app.calls.submit.length, 0);
});

test("discard removal failure preserves reply text and disables sending", async () => {
  const app = mailAppFixture({
    prepares: [preparedMailReply("op-delete-failure", { bodyText: "" })],
    statuses: [{ status: "not_submitted", retry_eligible: true }],
    failDelete: true
  });
  await app.settle();
  const body = app.elements.get("reply-body");
  body.value = "Keep this if browser storage cannot remove it";
  await body.emit("input");
  await app.settle();

  await app.elements.get("discard-draft").emit("click");
  await app.settle();

  assert.equal(body.value, "Keep this if browser storage cannot remove it");
  assert.equal(app.savedDraft.body_text, body.value);
  assert.equal(app.elements.get("send-reply").disabled, true);
  assert.match(app.elements.get("composer-message").textContent, /could not remove/);
  assert.equal(app.calls.submit.length, 0);
});

test("discard after an unknown status keeps the operation marker even without a local submit attempt", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-status-error",
    from_choices: [choice], from: choice,
    to: [{ address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Status must be checked before removing the record",
    operation_id: "op-status-error", operation_status: "not_submitted", operation_attempted: false
  });
  const app = mailAppFixture({
    storedDraft,
    statuses: [new Error("fixture status route unavailable"), { status: "not_submitted", retry_eligible: true }]
  });
  await app.settle();
  assert.equal(app.savedDraft.operation_status, "unknown");

  await app.elements.get("discard-draft").emit("click");
  await app.settle();

  assert.match(app.confirmPrompts[0], /cannot cancel/);
  assert.equal(app.calls.deletes.length, 0);
  assert.equal(app.savedDraft.discarded, true);
  assert.equal(app.savedDraft.operation_id, "op-status-error");
  assert.equal(app.savedDraft.operation_attempted, true);
  for (const key of ["from_choices", "from", "to", "cc", "bcc", "subject", "body_text", "operation_payload"]) {
    assert.equal(key in app.savedDraft, false, key);
  }
});

test("failed discarded-marker write keeps the reply visible and disables sending", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const payload = {
    account_id: "account-1", locator: "locator-1",
    from: { name: choice.name, address: choice.address },
    to: [{ name: null, address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Keep this visible if marker storage fails"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-marker-error",
    from_choices: [choice], ...payload, operation_id: "op-marker-error", operation_status: "unknown",
    operation_attempted: true, operation_payload: payload
  });
  const app = mailAppFixture({
    storedDraft,
    statuses: [{ status: "unknown", retry_eligible: false }],
    failPut: true
  });
  await app.settle();
  const body = app.elements.get("reply-body");
  assert.equal(body.value, payload.body_text);

  await app.elements.get("discard-draft").emit("click");
  await app.settle();

  assert.match(app.confirmPrompts[0], /cannot cancel/);
  assert.equal(body.value, payload.body_text);
  assert.equal(app.savedDraft.body_text, payload.body_text);
  assert.equal(app.elements.get("send-reply").disabled, true);
  assert.match(app.elements.get("composer-message").textContent, /could not remove/);
  assert.equal(app.calls.submit.length, 0);
});

test("discard after an attempt removes reply text but retains a minimal operation marker", async () => {
  const payload = {
    account_id: "account-1", locator: "locator-1",
    from: { name: "Work", address: "work@example.invalid" },
    to: [{ name: null, address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Keep this private until discarded"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-uncertain",
    from_choices: [{ name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" }],
    ...payload, operation_id: "op-uncertain", operation_status: "unknown",
    operation_attempted: true, operation_payload: payload
  });
  const app = mailAppFixture({
    storedDraft,
    statuses: [
      { status: "unknown", retry_eligible: false },
      { status: "unknown", retry_eligible: false }
    ]
  });
  await app.settle();
  assert.equal(app.elements.get("reply-body").value, payload.body_text);

  await app.elements.get("discard-draft").emit("click");
  await app.settle();

  assert.match(app.confirmPrompts[0], /cannot cancel/);
  assert.equal(app.calls.deletes.length, 0);
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.savedDraft.discarded, true);
  assert.equal(app.savedDraft.operation_id, "op-uncertain");
  assert.equal(app.savedDraft.operation_status, "unknown");
  assert.equal(app.savedDraft.operation_attempted, true);
  for (const key of ["from_choices", "from", "to", "cc", "bcc", "subject", "body_text", "operation_payload"]) {
    assert.equal(key in app.savedDraft, false, key);
  }
  assert.equal(app.elements.get("reply-body").value, "");
  assert.equal(app.elements.get("discard-draft").hidden, true);
  assert.equal(app.elements.get("send-reply").disabled, true);
  assert.match(app.elements.get("composer-message").textContent, /cannot cancel/);
});

test("corrected payload after proven non-submission prepares a new operation and requires renewed From choice", async () => {
  const oldChoice = { name: "Old Work", address: "old@example.invalid", provenance: "config", validity: "valid" };
  const oldPayload = {
    account_id: "account-1", locator: "locator-1",
    from: { name: oldChoice.name, address: oldChoice.address },
    to: [{ name: null, address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "Original reply"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-correct",
    from_choices: [oldChoice], ...oldPayload, operation_id: "op-old", operation_status: "not_submitted",
    operation_attempted: true, operation_payload: oldPayload
  });
  const freshChoice = { name: "Current Work", address: "current@example.invalid", provenance: "config", validity: "valid" };
  const app = mailAppFixture({
    storedDraft,
    statuses: [
      { status: "not_submitted", retry_eligible: true },
      { status: "not_submitted", retry_eligible: true }
    ],
    prepares: [preparedMailReply("op-new", { fromChoices: [freshChoice], selectedFrom: freshChoice, bodyText: "Corrected reply" })],
    submissions: [{ status: "sent", operation_id: "op-new" }]
  });
  await app.settle();

  const body = app.elements.get("reply-body");
  body.value = "Corrected reply";
  await body.emit("input");
  await app.settle();
  assert.equal(app.elements.get("send-reply").textContent, "Prepare corrected reply");
  assert.equal(app.elements.get("send-reply").disabled, false);

  await app.elements.get("reply-form").emit("submit");
  await app.settle();

  assert.equal(app.calls.prepare.length, 1);
  assert.deepEqual(app.calls.prepare[0], { locator: "locator-1", bodyText: "Corrected reply" });
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.calls.status[1].operationId, "op-new");
  assert.equal(app.savedDraft.require_from_choice, true);

  const restored = mailAppFixture({
    storedDraft: app.savedDraft,
    statuses: [{ status: "not_submitted", retry_eligible: true }],
    submissions: [{ status: "sent", operation_id: "op-new" }]
  });
  await restored.settle();
  assert.equal(restored.elements.get("reply-body").value, "Corrected reply");
  assert.equal(restored.elements.get("reply-from").value, "");
  assert.ok(restored.elements.get("reply-from").children.some(option => option.value === "current@example.invalid"));
  assert.equal(restored.elements.get("send-reply").disabled, true);

  restored.elements.get("reply-from").value = "current@example.invalid";
  await restored.elements.get("reply-from").emit("change");
  await restored.settle();
  assert.equal(restored.elements.get("send-reply").textContent, "Send reply");
  assert.equal(restored.elements.get("send-reply").disabled, false);

  await restored.elements.get("reply-form").emit("submit");
  await restored.settle();

  assert.equal(restored.calls.prepare.length, 0);
  assert.equal(restored.calls.submit.length, 1);
  assert.equal(restored.calls.submit[0].operationId, "op-new");
  assert.equal(restored.calls.submit[0].payload.from.address, "current@example.invalid");
  assert.equal(restored.calls.submit[0].payload.body_text, "Corrected reply");
});

test("an unchanged proven not-submitted reply retries only after explicit activation with its original operation", async () => {
  const choice = { name: "Work", address: "work@example.invalid", provenance: "config", validity: "valid" };
  const payload = {
    account_id: "account-1", locator: "locator-1",
    from: { name: choice.name, address: choice.address },
    to: [{ name: null, address: "reply@example.invalid" }], cc: [], bcc: [],
    subject: "Re: Fixture", body_text: "The reviewed reply"
  };
  const storedDraft = mailDrafts.createDraftRecord({
    principal_id: "principal-1", account_id: "account-1", locator: "locator-1", draft_id: "draft-retry",
    from_choices: [choice], ...payload, operation_id: "op-retry", operation_status: "not_submitted",
    operation_attempted: true, operation_payload: payload
  });
  const app = mailAppFixture({
    storedDraft,
    statuses: [{ status: "not_submitted", retry_eligible: true }],
    submissions: [{ status: "sent", operation_id: "op-retry" }]
  });
  await app.settle();
  assert.equal(app.calls.submit.length, 0);
  assert.equal(app.elements.get("send-reply").textContent, "Retry send");

  await app.elements.get("reply-form").emit("submit");
  await app.settle();

  assert.equal(app.calls.prepare.length, 0);
  assert.equal(app.calls.submit.length, 1);
  assert.equal(app.calls.submit[0].operationId, "op-retry");
  assert.deepEqual(app.calls.submit[0].payload, payload);
});

test("only a prepared operation with proven retry eligibility can submit, and corrected attempts need a new operation", () => {
  const ready = { prepared: true, storageReady: true, busy: false };
  assert.equal(mailState.canSubmit({ ...ready, status: null, operationId: null }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "not_submitted", operationId: "op-prepared", retryEligible: true }), true);
  assert.equal(mailState.canSubmit({ ...ready, status: "unknown", operationId: "op-old" }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "pending", operationId: "op-old" }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "absent", operationId: "op-old" }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "not_submitted", operationId: "op-old", retryEligible: false }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "not_submitted", operationId: "op-old", retryEligible: true }), true);
  assert.equal(mailState.operationIdForAttempt("op-prepared"), "op-prepared");
  assert.throws(() => mailState.operationIdForAttempt(null), /prepared operation ID/);
  assert.equal(mailState.normalizeStatus("unrecognized"), "unknown");
  assert.equal(mailState.canEditDraft({ status: null, operationId: null }), true);
  assert.equal(mailState.canEditDraft({ status: "not_submitted", operationId: "op-prepared", retryEligible: true }), true);
  assert.equal(mailState.canEditDraft({ status: "unknown", operationId: "op-old", operationAttempted: true }), false);
  assert.equal(mailState.canEditDraft({ status: "not_submitted", operationId: "op-old", retryEligible: true }), true);
  assert.equal(mailState.canEditDraft({ status: "not_submitted", operationId: "op-old", retryEligible: true, discarded: true }), false);
  const attempted = {
    account_id: "account-1", locator: "locator-1", from: { address: "work@example.invalid" },
    to: [{ address: "reply@example.invalid" }], cc: [], bcc: [], subject: "Re: fixture", body_text: "Original"
  };
  assert.equal(mailState.needsNewOperation({
    status: "not_submitted", retryEligible: true, operationAttempted: true,
    operationPayload: attempted, reviewedPayload: { ...attempted }
  }), false);
  assert.equal(mailState.needsNewOperation({
    status: "not_submitted", retryEligible: true, operationAttempted: true,
    operationPayload: attempted, reviewedPayload: { ...attempted, body_text: "Corrected" }
  }), true);
  assert.equal(mailState.needsNewOperation({
    status: "unknown", retryEligible: false, operationAttempted: true,
    operationPayload: attempted, reviewedPayload: { ...attempted, body_text: "Corrected" }
  }), false);
  assert.equal(mailState.sameReviewedPayload(attempted, { ...attempted }), true);
});

test("multiple verified From choices require a saved human choice", () => {
  const choices = [
    { name: "Primary", address: "primary@example.invalid" },
    { name: "Work", address: "work@example.invalid" }
  ];
  assert.equal(mailState.chooseFrom(null, choices), null);
  assert.equal(mailState.chooseFrom({ address: "WORK@example.invalid" }, choices), choices[1]);
  assert.equal(mailState.chooseFrom(null, [choices[0]]), choices[0]);
  assert.equal(mailState.choosePreparedFrom(null, choices[1], choices), null);
  assert.equal(mailState.choosePreparedFrom({ address: "WORK@example.invalid" }, choices[0], choices), choices[1]);
  assert.equal(mailState.choosePreparedFrom(null, choices[0], [choices[0]]), choices[0]);
});
