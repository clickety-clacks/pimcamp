(function () {
  "use strict";

  const api = window.PimcampMailClient;
  const textTools = window.PimcampMailText;
  const draftTools = window.PimcampMailDrafts;
  const mailState = window.PimcampMailState;
  const client = api.createMailClient();
  const draftStore = draftTools.createDraftStore();
  const byId = id => document.getElementById(id);
  const view = byId("mail-view");
  const gate = byId("gate");
  const form = byId("reply-form");
  const composerCard = byId("composer-card");
  const state = {
    locator: currentLocator(),
    principalId: "",
    sourceSubject: "",
    accountId: "",
    draft: null,
    formBound: false,
    fromChoices: [],
    prepared: false,
    storageReady: false,
    saving: Promise.resolve(),
    saveVersion: 0,
    busy: false,
    submitting: false,
    reconciling: false
  };

  function currentLocator() {
    const parts = window.location.pathname.split("/").filter(Boolean);
    if (parts.length !== 3 || parts[0] !== "mail" || parts[1] !== "m") return "";
    try {
      const value = decodeURIComponent(parts[2]);
      return value && !value.includes("/") && !value.includes("\\") ? value : "";
    } catch (_) { return ""; }
  }

  function uuid() {
    if (typeof crypto?.randomUUID === "function") return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    return [...bytes].map((value, index) => `${[4, 6, 8, 10].includes(index) ? "-" : ""}${value.toString(16).padStart(2, "0")}`).join("");
  }

  function isSessionError(error) {
    return error?.status === 401 || ["session_expired", "authentication_required", "unauthenticated"].includes(error?.code);
  }

  function publicError(error) {
    if (error?.code === "not_found" || error?.code === "message_missing") return "This message is no longer available. Your saved reply remains in this browser.";
    if (error?.code === "ambiguous_identity" || error?.code === "ambiguous") return "Pimcamp could not confirm which message this link refers to. No reply is available.";
    if (error?.code === "permission_denied" || error?.code === "account_unavailable") return "This mail account is not available to the current session.";
    if (error?.code === "draft_storage_unavailable") return "Draft recovery is unavailable in this browser. Keep this page open; sending stays disabled until the reviewed draft is saved.";
    if (error?.code === "outcome_unknown") return "The send response was lost. Pimcamp will check the saved operation; do not send again while its outcome is unknown.";
    if (error?.code === "status_unavailable") return "Pimcamp could not check the send status. The outcome is unknown, so do not retry.";
    return "Pimcamp could not complete this step. Your reply is preserved when browser storage is available.";
  }

  function setGate(title, message, mode = "loading") {
    view.hidden = true;
    gate.hidden = false;
    byId("gate-title").textContent = title;
    byId("gate-message").textContent = message;
    byId("gate-actions").hidden = mode !== "reauth" && mode !== "retry";
    byId("continue-session").hidden = mode !== "reauth";
    byId("retry-open").hidden = mode !== "retry";
    gate.querySelector(".spinner").hidden = mode !== "loading";
  }

  function showNotice(message, tone = "") {
    const notice = byId("notice");
    notice.textContent = message;
    notice.dataset.tone = tone;
    notice.hidden = !message;
  }

  function showComposerMessage(message, tone = "") {
    const output = byId("composer-message");
    output.textContent = message;
    output.dataset.tone = tone;
  }

  function showDraftState(message, status = "") {
    const output = byId("draft-state");
    output.textContent = message;
    output.dataset.state = status;
  }

  function appendTextLines(target, values, emptyText = "Not provided") {
    target.replaceChildren();
    const entries = Array.isArray(values) ? values : values && typeof values === "object" ? [values] : [];
    if (!entries.length) {
      target.textContent = emptyText;
      return;
    }
    for (const value of entries) {
      const line = document.createElement("div");
      line.className = "address-line";
      line.textContent = formatAddress(value);
      target.append(line);
    }
  }

  function formatAddress(value) {
    if (typeof value === "string") return singleLine(value);
    if (!value || typeof value !== "object") return "";
    const address = singleLine(value.address);
    const name = singleLine(value.name);
    return name && address ? `${name} <${address}>` : address || name;
  }

  function singleLine(value) {
    return typeof value === "string" ? value.replace(/[\r\n\t]+/g, " ").trim() : "";
  }

  function renderMessage(payload) {
    const account = payload?.account && typeof payload.account === "object" ? payload.account : {};
    const source = payload?.source && typeof payload.source === "object" ? payload.source : payload;
    const accountId = payload?.account_id || account.account_id;
    if (typeof accountId !== "string" || !accountId || !source || typeof source !== "object") {
      throw new Error("The message response is incomplete.");
    }
    state.sourceSubject = typeof source.subject === "string" ? source.subject : "";
    state.accountId = accountId;
    byId("account-badge").textContent = `Account · ${payload.account_label || account.label || accountId}`;
    appendTextLines(byId("source-from"), source.from);
    appendTextLines(byId("source-to"), source.to);
    const cc = Array.isArray(source.cc) ? source.cc : source.cc ? [source.cc] : [];
    byId("source-cc-row").hidden = cc.length === 0;
    appendTextLines(byId("source-cc"), cc);
    byId("source-date").textContent = source.sent_at || source.received_at || source.date || "Date unavailable";
    byId("source-subject").textContent = typeof source.subject === "string" ? source.subject : "(No subject)";
    const body = textTools.sourceText(source);
    byId("source-body").textContent = body || "No readable message text is available.";
  }

  function choicesFrom(reply) {
    // The API contract returns only currently authorized From choices; discard explicit invalidations.
    const values = Array.isArray(reply?.from_choices) ? reply.from_choices : [];
    return values.filter(choice => choice && typeof choice.address === "string" && choice.address.length > 0 && !/[\r\n\t]/.test(choice.address) &&
      choice.verified !== false && choice.valid !== false && choice.validity !== "invalid" && choice.validity !== "withdrawn");
  }

  function selectFrom(preferred, choices) {
    return mailState.chooseFrom(preferred, choices);
  }

  function setAddressTextarea(element, values) {
    element.value = (Array.isArray(values) ? values : []).map(formatAddress).filter(Boolean).join("\n");
  }

  function parseAddressLines(value) {
    return String(value ?? "").split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => {
      const match = /^(.*?)\s*<([^<>]+)>$/.exec(line);
      if (!match) return { name: null, address: line };
      const name = match[1].trim().replace(/^"|"$/g, "");
      return { name: name || null, address: match[2].trim() };
    });
  }

  function applyDraftToForm(draft) {
    const select = byId("reply-from");
    select.replaceChildren(new Option("Choose a verified From address", ""));
    for (const choice of state.fromChoices) {
      const option = new Option(formatAddress(choice), choice.address);
      option.dataset.provenance = typeof choice.provenance === "string" ? choice.provenance : "";
      select.add(option);
    }
    const selected = selectFrom(draft.from, state.fromChoices);
    select.value = selected?.address || "";
    if (draft.from && !selected) {
      byId("from-help").textContent = "The saved From identity is no longer available. Choose a current verified identity; your text is preserved.";
    } else if (state.fromChoices.length > 1 && !selected) {
      byId("from-help").textContent = "Choose which verified identity should send this reply.";
    } else if (!state.fromChoices.length) {
      byId("from-help").textContent = "No verified From identity is available. The reply is saved, but cannot be sent.";
    } else {
      byId("from-help").textContent = "Choose the identity that will send this reply.";
    }
    setAddressTextarea(byId("reply-to"), draft.to);
    setAddressTextarea(byId("reply-cc"), draft.cc);
    setAddressTextarea(byId("reply-bcc"), draft.bcc);
    byId("reply-subject").value = draft.subject || "";
    byId("reply-body").value = draft.body_text || "";
    state.formBound = true;
    updateReview();
    updateSendButton();
  }

  function syncDraftFromForm() {
    if (!state.draft) return;
    const from = state.fromChoices.find(choice => choice.address === byId("reply-from").value) || null;
    state.draft.from = from ? { name: singleLine(from.name) || null, address: from.address } : null;
    state.draft.to = parseAddressLines(byId("reply-to").value);
    state.draft.cc = parseAddressLines(byId("reply-cc").value);
    state.draft.bcc = parseAddressLines(byId("reply-bcc").value);
    state.draft.subject = byId("reply-subject").value;
    state.draft.body_text = byId("reply-body").value;
    updateReview();
    updateSendButton();
  }

  function updateReview() {
    const from = state.fromChoices.find(choice => choice.address === byId("reply-from").value);
    byId("review-from").textContent = formatAddress(from) || "Choose a verified From address";
    byId("review-to").textContent = byId("reply-to").value.trim() || "No recipient";
    byId("review-cc").textContent = byId("reply-cc").value.trim() || "None";
    byId("review-bcc").textContent = byId("reply-bcc").value.trim() || "None";
    byId("review-subject").textContent = byId("reply-subject").value || "(No subject)";
    byId("review-body").textContent = byId("reply-body").value;
  }

  function statusBlocksSubmit() {
    return !mailState.canSubmit({
      status: state.draft?.operation_status,
      operationId: state.draft?.operation_id,
      prepared: state.prepared,
      storageReady: state.storageReady,
      busy: state.submitting || state.reconciling
    });
  }

  function updateFormAvailability() {
    const editable = !state.submitting && !state.reconciling && mailState.canEditDraft(
      state.draft?.operation_status, state.draft?.operation_id
    );
    for (const control of form.elements) {
      if (control.id !== "send-reply") control.disabled = !editable;
    }
  }

  function updateSendButton() {
    const button = byId("send-reply");
    if (!button || !state.draft) return;
    const retry = state.draft.operation_status === "not_submitted";
    button.textContent = retry ? "Retry send" : "Send reply";
    const valid = Boolean(byId("reply-from").value && byId("reply-to").value.trim() && byId("reply-subject").value.trim());
    button.disabled = statusBlocksSubmit() || !valid;
    updateFormAvailability();
  }

  function snapshotDraft() {
    return state.draft ? { ...state.draft, to: state.draft.to.map(item => ({ ...item })), cc: state.draft.cc.map(item => ({ ...item })), bcc: state.draft.bcc.map(item => ({ ...item })) } : null;
  }

  function queueSave() {
    if (state.formBound) syncDraftFromForm();
    if (!state.draft) return Promise.resolve(false);
    state.storageReady = false;
    showDraftState("Saving draft in this browser…");
    updateSendButton();
    const record = snapshotDraft();
    const saveVersion = ++state.saveVersion;
    state.saving = state.saving.catch(() => undefined).then(() => draftStore.put(record));
    return state.saving.then(() => {
      if (saveVersion !== state.saveVersion) return false;
      state.storageReady = true;
      showDraftState("Draft saved in this browser", "saved");
      updateSendButton();
      return true;
    }).catch(() => {
      if (saveVersion !== state.saveVersion) return false;
      state.storageReady = false;
      showDraftState("Draft recovery unavailable; keep this page open", "error");
      showComposerMessage(publicError({ code: "draft_storage_unavailable" }), "error");
      updateSendButton();
      return false;
    });
  }

  function handleOperationStatus(status) {
    const normalized = mailState.normalizeStatus(status);
    state.draft.operation_status = normalized;
    state.draft.operation_attempted = true;
    if (normalized === "sent") {
      state.draft.from = null;
      state.draft.to = [];
      state.draft.cc = [];
      state.draft.bcc = [];
      state.draft.subject = "";
      state.draft.body_text = "";
      composerCard.dataset.state = "sent";
      showComposerMessage("Pimcamp confirmed submission. Delivery is not confirmed.", "success");
      showDraftState("Submitted", "saved");
    } else if (normalized === "not_submitted") {
      composerCard.dataset.state = "ready";
      showComposerMessage("Pimcamp confirmed this operation did not submit. Review the reply and explicitly retry if you still want to send it.");
      showDraftState("Draft saved; safe retry is available", "saved");
    } else if (normalized === "pending") {
      showComposerMessage("Pimcamp has not confirmed completion yet. Keep this draft; do not retry.");
      showDraftState("Send status pending");
    } else if (normalized === "absent") {
      showComposerMessage("No receipt is visible yet. The request may still arrive; do not retry.");
      showDraftState("Send outcome not established");
    } else if (normalized === "unknown") {
      showComposerMessage("The reply may have been submitted, but Pimcamp cannot confirm it. Keep this operation and do not retry.", "error");
      showDraftState("Send outcome unknown", "error");
    }
    if (normalized === "sent") {
      byId("reply-from").value = "";
      setAddressTextarea(byId("reply-to"), []);
      setAddressTextarea(byId("reply-cc"), []);
      setAddressTextarea(byId("reply-bcc"), []);
      byId("reply-subject").value = "";
      byId("reply-body").value = "";
      updateReview();
    }
    updateSendButton();
    return queueSave();
  }

  async function querySendStatus() {
    if (!state.draft?.operation_id || !state.accountId) return;
    state.reconciling = true;
    state.draft.operation_status = state.draft.operation_status || "pending";
    showComposerMessage("Checking the saved send operation…");
    updateSendButton();
    try {
      const result = await client.sendStatus(state.accountId, state.draft.operation_id);
      await handleOperationStatus(result.status);
    } catch (error) {
      if (isSessionError(error)) {
        await requireRenewal();
      } else {
        state.draft.operation_status = "unknown";
        showComposerMessage(publicError({ code: "status_unavailable" }), "error");
        showDraftState("Send outcome unknown", "error");
        await queueSave();
      }
    } finally {
      state.reconciling = false;
      updateSendButton();
    }
  }

  async function prepareAndRestore() {
    let stored = null;
    const memoryDraft = state.draft && state.draft.principal_id === state.principalId &&
      state.draft.account_id === state.accountId && state.draft.locator === state.locator
      ? snapshotDraft()
      : null;
    try {
      stored = await draftStore.get(state.principalId, state.accountId, state.locator);
      state.storageReady = true;
    } catch (_) {
      state.storageReady = false;
    }
    if (memoryDraft) stored = memoryDraft;

    let prepared = null;
    try {
      prepared = await client.prepareReply(state.locator, stored?.body_text || "");
      const preparedAccount = prepared.account_id || prepared.account?.account_id;
      const preparedSource = prepared.source || prepared.message;
      const preparedLocator = prepared.locator || preparedSource?.locator;
      if (preparedAccount !== state.accountId || (preparedLocator && preparedLocator !== state.locator)) {
        prepared = null;
        throw new Error("The reply account did not match the source account.");
      }
      state.prepared = true;
    } catch (error) {
      if (isSessionError(error)) {
        await requireRenewal();
        return;
      }
      state.prepared = false;
      showNotice(publicError(error), "error");
    }

    const reply = prepared?.reply && typeof prepared.reply === "object" ? prepared.reply : prepared || {};
    state.fromChoices = choicesFrom(reply);
    const savedFrom = stored?.from || null;
    const selectedFrom = selectFrom(savedFrom, state.fromChoices);
    const draft = {
      principal_id: state.principalId,
      account_id: state.accountId,
      locator: state.locator,
      draft_id: stored?.draft_id || uuid(),
      from: selectedFrom || (stored?.from && !state.prepared ? stored.from : null),
      to: Array.isArray(stored?.to) ? stored.to : (reply.to || []),
      cc: Array.isArray(stored?.cc) ? stored.cc : (reply.cc || []),
      bcc: Array.isArray(stored?.bcc) ? stored.bcc : (reply.bcc || []),
      subject: typeof stored?.subject === "string" ? stored.subject : (reply.subject || ""),
      body_text: typeof stored?.body_text === "string" ? stored.body_text : (reply.body_text || ""),
      operation_id: stored?.operation_id || null,
      operation_status: stored?.operation_id
        ? mailState.normalizeStatus(stored.operation_status || "unknown")
        : null,
      operation_attempted: Boolean(stored?.operation_attempted)
    };
    if (!draft.subject && state.sourceSubject) {
      draft.subject = /^re:/i.test(state.sourceSubject) ? state.sourceSubject : `Re: ${state.sourceSubject}`;
    }
    state.draft = draft;
    applyDraftToForm(draft);
    byId("composer-card").hidden = false;

    if (state.prepared && state.storageReady) await queueSave();
    else if (!state.storageReady) showDraftState("Draft recovery unavailable; keep this page open", "error");
    if (stored?.operation_id) await querySendStatus();
  }

  async function loadMessage() {
    if (!state.locator) {
      setGate("Message link unavailable", "This link does not contain a valid message reference.", "invalid");
      return;
    }
    if (state.draft && state.formBound) await queueSave();
    hidePrivateContent();
    showNotice("", "");
    setGate("Opening your message", "Loading the authorized source message…", "loading");
    try {
      const message = await client.getMessage(state.locator);
      renderMessage(message);
      gate.hidden = true;
      view.hidden = false;
      await prepareAndRestore();
    } catch (error) {
      if (isSessionError(error)) {
        await requireRenewal();
        return;
      }
      const message = publicError(error);
      setGate("Message could not be opened", message, "retry");
    }
  }

  async function acceptSession(payload) {
    const principal = payload.principal_id || payload.principal_namespace;
    const csrf = payload.csrf_token || payload.csrf;
    if (typeof principal !== "string" || !principal || typeof csrf !== "string" || !csrf) {
      throw new Error("The mail session response is incomplete.");
    }
    state.principalId = principal;
    client.setCsrfToken(csrf);
  }

  async function requireRenewal() {
    if (state.draft) await queueSave();
    hidePrivateContent();
    client.setCsrfToken("");
    setGate("Mail session expired", "Your reply is preserved in this browser. Continue your mail session to reopen the message and check any send operation.", "reauth");
  }

  function hidePrivateContent() {
    view.hidden = true;
    byId("source-from").replaceChildren();
    byId("source-to").replaceChildren();
    byId("source-cc").replaceChildren();
    byId("source-cc-row").hidden = true;
    byId("source-date").textContent = "";
    byId("source-subject").textContent = "";
    byId("source-body").textContent = "";
    byId("account-badge").textContent = "";
    for (const id of ["reply-to", "reply-cc", "reply-bcc", "reply-subject", "reply-body"]) byId(id).value = "";
    byId("reply-from").replaceChildren();
    for (const id of ["review-from", "review-to", "review-cc", "review-bcc", "review-subject", "review-body"]) {
      byId(id).textContent = "";
    }
    byId("composer-message").textContent = "";
    byId("draft-state").textContent = "";
    state.formBound = false;
    state.sourceSubject = "";
    state.accountId = "";
    state.prepared = false;
    state.storageReady = false;
    state.fromChoices = [];
  }

  async function resumeSession() {
    if (state.busy) return;
    state.busy = true;
    setGate("Opening your message", "Checking your mail session…", "loading");
    try {
      const session = await client.openSession();
      await acceptSession(session);
      await loadMessage();
    } catch (error) {
      const auth = isSessionError(error);
      setGate(auth ? "Mail session needed" : "Mail could not be reached", auth
        ? "Continue through the approved private sign-in to view this message."
        : publicError(error), auth ? "reauth" : "retry");
    } finally {
      state.busy = false;
    }
  }

  async function submitReply(event) {
    event.preventDefault();
    if (!state.draft || statusBlocksSubmit()) return;
    syncDraftFromForm();
    const selectedFrom = state.fromChoices.find(choice => choice.address === byId("reply-from").value);
    const recipients = [...state.draft.to, ...state.draft.cc, ...state.draft.bcc];
    if (!selectedFrom || !recipients.length || !state.draft.subject.trim()) {
      showComposerMessage("Choose a verified From identity and at least one recipient, then review the reply.", "error");
      updateSendButton();
      return;
    }
    const reviewedReply = {
      account_id: state.accountId,
      locator: state.locator,
      from: { name: singleLine(selectedFrom.name) || null, address: selectedFrom.address },
      to: state.draft.to.map(item => ({ ...item })),
      cc: state.draft.cc.map(item => ({ ...item })),
      bcc: state.draft.bcc.map(item => ({ ...item })),
      subject: state.draft.subject,
      body_text: state.draft.body_text
    };
    const previousOperationId = state.draft.operation_id;
    const previousOperationStatus = state.draft.operation_status;
    const previousAttempted = state.draft.operation_attempted;
    state.submitting = true;
    updateSendButton();
    showComposerMessage("Submitting the reviewed reply…");
    try {
      if (!(await queueSave())) return;
      const operationId = mailState.operationIdForAttempt(previousOperationId, previousOperationStatus, uuid);
      state.draft.operation_id = operationId;
      state.draft.operation_status = "sending";
      state.draft.operation_attempted = true;
      if (!(await queueSave())) {
        state.draft.operation_id = previousOperationId;
        state.draft.operation_status = previousOperationStatus;
        state.draft.operation_attempted = previousAttempted;
        updateSendButton();
        return;
      }

      try {
        const result = await client.submitReply(operationId, reviewedReply);
        const status = mailState.normalizeStatus(result.status);
        await handleOperationStatus(status);
        if (["pending", "unknown", "absent"].includes(status)) await querySendStatus();
      } catch (error) {
        if (isSessionError(error)) {
          state.draft.operation_status = "unknown";
          await queueSave();
          await requireRenewal();
        } else {
          state.draft.operation_status = "unknown";
          state.draft.operation_attempted = true;
          showComposerMessage(publicError(error), "error");
          await queueSave();
          await querySendStatus();
        }
      }
    } finally {
      state.submitting = false;
      updateSendButton();
    }
  }

  form.addEventListener("input", () => { queueSave(); });
  form.addEventListener("change", () => { queueSave(); });
  form.addEventListener("submit", submitReply);
  byId("continue-session").addEventListener("click", resumeSession);
  byId("retry-open").addEventListener("click", resumeSession);
  if (!state.locator) setGate("Message link unavailable", "This link does not contain a valid message reference.", "invalid");
  else resumeSession();
})();
