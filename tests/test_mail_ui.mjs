import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(import.meta.url);
const mailText = require("../ui/mail/mail-text.js");
const mailClient = require("../ui/mail/mail-client.js");
const mailDrafts = require("../ui/mail/mail-drafts.js");
const mailState = require("../ui/mail/mail-state.js");
const projectRoot = fileURLToPath(new URL("..", import.meta.url));

test("page references only locally provided same-origin assets", () => {
  const html = readFileSync(new URL("../ui/mail/index.html", import.meta.url), "utf8");
  const assets = [...html.matchAll(/(?:src|href)="\/mail\/assets\/([^\"]+)"/g)].map(match => match[1]);
  assert.ok(assets.length > 0);
  for (const asset of assets) assert.equal(existsSync(join(projectRoot, "ui", "mail", asset)), true, asset);
  assert.match(html, /img-src 'none'/);
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
        }, record)
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
    { principal_id: "principal-1", csrf_token: "csrf-fixture" },
    { account_id: "account-1", source: { subject: "Fixture" } },
    { account_id: "account-1", reply: { from_choices: [] } },
    { status: "unknown" }
  ];
  const client = mailClient.createMailClient({
    csrfToken: "csrf-fixture",
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return jsonResponse(responses.shift());
    }
  });

  await client.openSession();
  await client.getMessage("opaque locator");
  await client.prepareReply("opaque locator", "draft text");
  const status = await client.sendStatus("account one", "operation one");

  assert.equal(status.status, "unknown");
  assert.deepEqual(calls.map(call => [call.options.method, call.path]), [
    ["POST", "/mail/api/session"],
    ["GET", "/mail/api/v2/messages/opaque%20locator"],
    ["POST", "/mail/api/v2/replies/prepare"],
    ["GET", "/mail/api/v2/accounts/account%20one/send-status/operation%20one"]
  ]);
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[2].options.headers["X-Pimcamp-CSRF"], "csrf-fixture");
  assert.deepEqual(JSON.parse(calls[2].options.body), { locator: "opaque locator", body_text: "draft text" });
  assert.ok(calls.every(call => !call.path.endsWith("/submit")));
  assert.throws(() => client.getMessage("bad/locator"), { code: "invalid_request" });
});

test("explicit submit call posts the reviewed recipient and body snapshot", async () => {
  const calls = [];
  const client = mailClient.createMailClient({
    csrfToken: "csrf-fixture",
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return jsonResponse({ status: "pending" });
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
  assert.equal(result.status, "pending");
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
  assert.equal(result.payload, null);
});

test("draft persistence whitelists same-browser content and retains operation identity", () => {
  const draft = mailDrafts.createDraftRecord({
    principal_id: "principal-1",
    account_id: "account-1",
    locator: "locator-1",
    draft_id: "draft-1",
    from: { name: "Work", address: "work@example.invalid" },
    to: [{ address: "reply@example.invalid" }],
    cc: [],
    bcc: [],
    subject: "Re: fixture",
    body_text: "Keep this reply",
    operation_id: "op-1",
    operation_status: "unknown",
    source_body: "must not persist",
    csrf_token: "must not persist",
    session_cookie: "must not persist",
    password: "must not persist"
  }, 1234);

  assert.equal(draft.key, JSON.stringify(["principal-1", "account-1", "locator-1"]));
  assert.equal(draft.operation_id, "op-1");
  assert.equal(draft.operation_status, "unknown");
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
    from: { address: "from@example.invalid" }, to: [{ address: "to@example.invalid" }],
    cc: [], bcc: [], subject: "Re: fixture", body_text: "Saved text",
    operation_id: "op-restore", operation_status: "unknown"
  });

  const refreshedPage = mailDrafts.createDraftStore({ indexedDBImpl, now: () => 43 });
  const restored = await refreshedPage.get("principal-1", "account-1", "locator-1");
  const wrongAccount = await refreshedPage.get("principal-1", "account-2", "locator-1");
  const wrongPrincipal = await refreshedPage.get("principal-2", "account-1", "locator-1");

  assert.equal(restored.body_text, "Saved text");
  assert.equal(restored.operation_id, "op-restore");
  assert.equal(restored.operation_status, "unknown");
  assert.equal(wrongAccount, undefined);
  assert.equal(wrongPrincipal, undefined);
});

test("unknown or pending operation cannot send again; only proven pre-submit failure permits a new operation", () => {
  const ready = { prepared: true, storageReady: true, busy: false };
  assert.equal(mailState.canSubmit({ ...ready, status: null, operationId: null }), true);
  assert.equal(mailState.canSubmit({ ...ready, status: "unknown", operationId: "op-old" }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "pending", operationId: "op-old" }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "absent", operationId: "op-old" }), false);
  assert.equal(mailState.canSubmit({ ...ready, status: "not_submitted", operationId: "op-old" }), true);
  assert.equal(mailState.operationIdForAttempt("op-old", "not_submitted", () => "op-new"), "op-new");
  assert.equal(mailState.operationIdForAttempt("op-old", "unknown", () => "op-new"), "op-old");
  assert.equal(mailState.normalizeStatus("unrecognized"), "unknown");
  assert.equal(mailState.canEditDraft("unknown", "op-old"), false);
});

test("multiple verified From choices require a saved human choice", () => {
  const choices = [
    { name: "Primary", address: "primary@example.invalid" },
    { name: "Work", address: "work@example.invalid" }
  ];
  assert.equal(mailState.chooseFrom(null, choices), null);
  assert.equal(mailState.chooseFrom({ address: "WORK@example.invalid" }, choices), choices[1]);
  assert.equal(mailState.chooseFrom(null, [choices[0]]), choices[0]);
});
