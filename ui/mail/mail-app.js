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
  const sessionStorageKey = "pimcamp-mail-session-v2";
  const activityDebounceMs = 500;
  const state = {
    locator: currentLocator(),
    principalId: "",
    sourceSubject: "",
    accountId: "",
    authorizedAccounts: new Map(),
    draft: null,
    formBound: false,
    fromChoices: [],
    prepared: false,
    storageReady: false,
    saving: Promise.resolve(),
    saveVersion: 0,
    busy: false,
    submitting: false,
    reconciling: false,
    discarding: false,
    idleDeadline: "",
    absoluteDeadline: "",
    deadlineTimer: null,
    activityTimer: null,
    activityPromise: null,
    activityError: null,
    renewalPromise: null,
    resumeCheckPending: false,
    bootPending: false,
    logoutPending: false
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
    return error?.status === 401 && error?.code === "session_expired";
  }

  function validDeadline(value) {
    const match = typeof value === "string" &&
      /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|\+00:00)$/.exec(value);
    if (!match) return false;
    const [, yearText, monthText, dayText, hourText, minuteText, secondText] = match;
    const year = Number(yearText);
    const month = Number(monthText);
    const day = Number(dayText);
    const hour = Number(hourText);
    const minute = Number(minuteText);
    const second = Number(secondText);
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) return false;
    const calendar = new Date(0);
    calendar.setUTCFullYear(year, month - 1, day);
    calendar.setUTCHours(hour, minute, second, 0);
    return calendar.getUTCFullYear() === year && calendar.getUTCMonth() === month - 1 &&
      calendar.getUTCDate() === day && Number.isFinite(Date.parse(value));
  }

  function validSessionShape(payload) {
    return payload && typeof payload === "object" &&
      validDeadline(payload.idle_deadline) && validDeadline(payload.absolute_deadline) &&
      Date.parse(payload.idle_deadline) <= Date.parse(payload.absolute_deadline);
  }

  function sessionExpired(now = Date.now()) {
    return !validDeadline(state.idleDeadline) || !validDeadline(state.absoluteDeadline) ||
      now >= Date.parse(state.idleDeadline) || now >= Date.parse(state.absoluteDeadline);
  }

  function foregroundVisible() {
    return document.visibilityState !== "hidden";
  }

  function clearTimer(name) {
    if (state[name] !== null) clearTimeout(state[name]);
    state[name] = null;
  }

  function saveSessionSnapshot() {
    if (!state.principalId || !state.csrfToken || !validDeadline(state.idleDeadline) || !validDeadline(state.absoluteDeadline)) return;
    const snapshot = {
      status: "authenticated",
      principal_id: state.principalId,
      csrf_token: state.csrfToken,
      accounts: [...state.authorizedAccounts.values()],
      idle_deadline: state.idleDeadline,
      absolute_deadline: state.absoluteDeadline,
      logout_pending: state.logoutPending
    };
    try { window.sessionStorage?.setItem(sessionStorageKey, JSON.stringify(snapshot)); } catch (_) { /* this tab can continue without refresh recovery */ }
  }

  function readSessionSnapshot() {
    try {
      const raw = window.sessionStorage?.getItem(sessionStorageKey);
      if (!raw) return null;
      const snapshot = JSON.parse(raw);
      if (snapshot?.status !== "authenticated" || typeof snapshot.principal_id !== "string" || !snapshot.principal_id ||
          typeof snapshot.csrf_token !== "string" || !snapshot.csrf_token || !Array.isArray(snapshot.accounts) ||
          !snapshot.accounts.length || !validSessionShape(snapshot)) return null;
      return snapshot;
    } catch (_) { return null; }
  }

  function clearSessionSnapshot() {
    clearTimer("deadlineTimer");
    clearTimer("activityTimer");
    state.idleDeadline = "";
    state.absoluteDeadline = "";
    state.csrfToken = "";
    try { window.sessionStorage?.removeItem(sessionStorageKey); } catch (_) { /* local display remains gated */ }
  }

  function scheduleDeadline() {
    clearTimer("deadlineTimer");
    if (!validDeadline(state.idleDeadline) || !validDeadline(state.absoluteDeadline)) return;
    const deadline = Math.min(Date.parse(state.idleDeadline), Date.parse(state.absoluteDeadline));
    const delay = Math.max(0, deadline - Date.now());
    state.deadlineTimer = setTimeout(() => {
      state.deadlineTimer = null;
      if (state.activityPromise) {
        void state.activityPromise.then(() => {
          if (sessionExpired()) void requireRenewal();
          else if (state.deadlineTimer === null) scheduleDeadline();
        });
      } else if (sessionExpired()) void requireRenewal();
      else scheduleDeadline();
    }, delay);
  }

  function scheduleEditingActivity() {
    clearTimer("activityTimer");
    if (!state.principalId || !state.idleDeadline || !foregroundVisible()) return;
    state.activityTimer = setTimeout(() => {
      state.activityTimer = null;
      void sendActivity().catch(() => undefined);
    }, activityDebounceMs);
  }

  async function sendActivity() {
    if (!foregroundVisible()) return false;
    if (state.activityPromise) return state.activityPromise;
    if (sessionExpired()) {
      await requireRenewal();
      return false;
    }
    const originalAbsoluteDeadline = state.absoluteDeadline;
    state.activityPromise = (async () => {
      try {
        const result = await client.activity();
        if (state.absoluteDeadline !== originalAbsoluteDeadline || !state.csrfToken) return false;
        if (!validSessionShape(result) || Date.parse(result.absolute_deadline) > Date.parse(originalAbsoluteDeadline)) {
          state.activityError = new Error("The mail session deadline changed unexpectedly.");
          await requireRenewal();
          return false;
        }
        const effectiveAbsoluteDeadline = Date.parse(result.absolute_deadline) < Date.parse(originalAbsoluteDeadline)
          ? result.absolute_deadline
          : originalAbsoluteDeadline;
        if (Date.now() >= Date.parse(result.idle_deadline) || Date.now() >= Date.parse(effectiveAbsoluteDeadline)) {
          state.activityError = null;
          await requireRenewal();
          return false;
        }
        state.activityError = null;
        state.idleDeadline = result.idle_deadline;
        state.absoluteDeadline = effectiveAbsoluteDeadline;
        saveSessionSnapshot();
        scheduleDeadline();
        return true;
      } catch (error) {
        state.activityError = error;
        if (isSessionError(error)) await requireRenewal();
        else if (error?.status === 403 && error?.code === "permission_denied") {
          await requireRenewal({
            title: "Mail session needs attention",
            message: "Pimcamp could not use this tab's mail session. Continue to recheck session and account access before reopening this message. Your saved reply and send operation remain in this browser."
          });
        }
        else showComposerMessage(publicError(error), "error");
        return false;
      } finally {
        state.activityPromise = null;
      }
    })();
    return state.activityPromise;
  }

  async function ensureSessionActivity() {
    clearTimer("activityTimer");
    return sendActivity();
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
    byId("gate-actions").hidden = mode !== "reauth" && mode !== "retry" && mode !== "logout-retry";
    byId("continue-session").hidden = mode !== "reauth";
    byId("retry-open").hidden = mode !== "retry" && mode !== "logout-retry";
    byId("retry-open").textContent = mode === "logout-retry" ? "Try logout again" : "Try again";
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
    const source = payload?.source;
    const accountId = payload?.account_id;
    const account = state.authorizedAccounts.get(accountId);
    if (typeof accountId !== "string" || !accountId || payload?.locator !== state.locator || !account ||
        typeof payload.account_label !== "string" || payload.account_label !== account.label || !source || typeof source !== "object") {
      throw new Error("The message response is incomplete.");
    }
    state.sourceSubject = typeof source.subject === "string" ? source.subject : "";
    state.accountId = accountId;
    byId("account-badge").textContent = `Account · ${account.label}`;
    appendTextLines(byId("source-from"), source.from);
    appendTextLines(byId("source-to"), source.to);
    const cc = Array.isArray(source.cc) ? source.cc : source.cc ? [source.cc] : [];
    byId("source-cc-row").hidden = cc.length === 0;
    appendTextLines(byId("source-cc"), cc);
    const replyTo = Array.isArray(source.reply_to) ? source.reply_to : source.reply_to ? [source.reply_to] : [];
    byId("source-reply-to-row").hidden = replyTo.length === 0;
    appendTextLines(byId("source-reply-to"), replyTo);
    byId("source-date").textContent = source.sent_at || source.received_at || source.date || "Date unavailable";
    byId("source-subject").textContent = typeof source.subject === "string" ? source.subject : "(No subject)";
    const body = textTools.sourceText(payload);
    byId("source-body").textContent = body || "No readable message text is available.";
  }

  function choicesFrom(values) {
    // The API contract returns only currently authorized From choices; discard explicit invalidations.
    const choices = Array.isArray(values) ? values : [];
    return choices.filter(choice => choice && typeof choice.address === "string" && choice.address.length > 0 && !/[\r\n\t]/.test(choice.address) &&
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
    const selected = draft.require_from_choice
      ? mailState.matchFrom(draft.from, state.fromChoices)
      : selectFrom(draft.from, state.fromChoices);
    if (draft.from && !selected && typeof draft.from.address === "string") {
      const option = new Option(`Unavailable: ${formatAddress(draft.from)}`, draft.from.address);
      option.disabled = true;
      select.add(option);
    }
    select.value = selected?.address || (draft.from?.address || "");
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

  function clearReplyFields() {
    byId("reply-from").replaceChildren(new Option("Choose a verified From address", ""));
    for (const id of ["reply-to", "reply-cc", "reply-bcc", "reply-subject", "reply-body"]) byId(id).value = "";
    state.formBound = false;
    updateReview();
  }

  function syncDraftFromForm() {
    if (!state.draft) return;
    const selectedFrom = byId("reply-from").value;
    const from = state.fromChoices.find(choice => choice.address === selectedFrom) || null;
    if (from) {
      state.draft.from = { name: singleLine(from.name) || null, address: from.address };
      state.draft.require_from_choice = false;
    } else if (state.draft.from?.address !== selectedFrom) state.draft.from = null;
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
    const savedFrom = state.draft?.from?.address === byId("reply-from").value ? state.draft.from : null;
    byId("review-from").textContent = formatAddress(from) || (savedFrom ? `${formatAddress(savedFrom)} (unavailable)` : "Choose a verified From address");
    byId("review-to").textContent = byId("reply-to").value.trim() || "No recipient";
    byId("review-cc").textContent = byId("reply-cc").value.trim() || "None";
    byId("review-bcc").textContent = byId("reply-bcc").value.trim() || "None";
    byId("review-subject").textContent = byId("reply-subject").value || "(No subject)";
    byId("review-body").textContent = byId("reply-body").value;
  }

  function currentReviewedReply() {
    const selectedFrom = state.fromChoices.find(choice => choice.address === byId("reply-from").value);
    return {
      account_id: state.accountId,
      locator: state.locator,
      from: selectedFrom ? { name: singleLine(selectedFrom.name) || null, address: selectedFrom.address } : null,
      to: (state.draft?.to || []).map(item => ({ ...item })),
      cc: (state.draft?.cc || []).map(item => ({ ...item })),
      bcc: (state.draft?.bcc || []).map(item => ({ ...item })),
      subject: state.draft?.subject || "",
      body_text: state.draft?.body_text || ""
    };
  }

  function statusBlocksSubmit() {
    return sessionExpired() || state.busy || !mailState.canSubmit({
      status: state.draft?.operation_status,
      operationId: state.draft?.operation_id,
      retryEligible: state.draft?.retry_eligible,
      discarded: state.draft?.discarded,
      operationAttempted: state.draft?.operation_attempted,
      prepared: state.prepared,
      storageReady: state.storageReady,
      busy: state.submitting || state.reconciling
    });
  }

  function updateFormAvailability() {
    const editable = !sessionExpired() && !state.busy && !state.submitting && !state.reconciling && !state.discarding && mailState.canEditDraft({
      status: state.draft?.operation_status,
      operationId: state.draft?.operation_id,
      retryEligible: state.draft?.retry_eligible,
      discarded: state.draft?.discarded
    });
    for (const control of form.elements) {
      if (control.id !== "send-reply" && control.id !== "discard-draft") control.disabled = !editable;
    }
    const discard = byId("discard-draft");
    discard.hidden = !state.draft || state.draft.discarded === true || state.draft.operation_status === "sent";
    discard.disabled = !state.draft || sessionExpired() || state.busy || state.submitting || state.reconciling || state.discarding;
    const newReply = byId("new-reply");
    if (newReply) newReply.disabled = sessionExpired() || state.submitting || state.busy;
    const logout = byId("logout-session");
    if (logout) logout.disabled = sessionExpired() || state.busy || state.submitting || state.reconciling;
  }

  function updateSendButton() {
    const button = byId("send-reply");
    if (!button) return;
    if (!state.draft) {
      button.disabled = true;
      updateFormAvailability();
      return;
    }
    if (state.draft.discarded === true) {
      button.textContent = "Reply text discarded";
      button.disabled = true;
      updateFormAvailability();
      return;
    }
    const reviewedReply = currentReviewedReply();
    const prepareCorrection = mailState.needsNewOperation({
      status: state.draft.operation_status,
      retryEligible: state.draft.retry_eligible,
      operationAttempted: state.draft.operation_attempted,
      operationPayload: state.draft.operation_payload,
      reviewedPayload: reviewedReply
    });
    const retry = state.draft.operation_attempted === true && state.draft.operation_status === "not_submitted" &&
      state.draft.retry_eligible === true && !prepareCorrection;
    button.textContent = prepareCorrection ? "Prepare corrected reply" : retry ? "Retry send" : "Send reply";
    const recipientCount = reviewedReply.to.length + reviewedReply.cc.length + reviewedReply.bcc.length;
    const valid = prepareCorrection
      ? Boolean(recipientCount && reviewedReply.subject.trim())
      : Boolean(reviewedReply.from && recipientCount && reviewedReply.subject.trim());
    button.disabled = statusBlocksSubmit() || !valid;
    byId("send-hint").textContent = prepareCorrection
      ? "Preparing a corrected reply will not send it. Review the new operation before activating Send reply."
      : "Mail is sent only when you activate Send reply.";
    updateFormAvailability();
  }

  function snapshotDraft() {
    if (!state.draft) return null;
    const payload = state.draft.operation_payload;
    return {
      ...state.draft,
      to: (state.draft.to || []).map(item => ({ ...item })),
      cc: (state.draft.cc || []).map(item => ({ ...item })),
      bcc: (state.draft.bcc || []).map(item => ({ ...item })),
      operation_payload: payload ? {
        ...payload,
        from: payload.from ? { ...payload.from } : null,
        to: (payload.to || []).map(item => ({ ...item })),
        cc: (payload.cc || []).map(item => ({ ...item })),
        bcc: (payload.bcc || []).map(item => ({ ...item }))
      } : null
    };
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

  function handleOperationStatus(status, retryEligible = false) {
    const normalized = mailState.normalizeStatus(status);
    state.draft.operation_status = normalized;
    state.draft.retry_eligible = normalized === "not_submitted" && retryEligible === true;
    if (["sent", "pending", "unknown"].includes(normalized)) state.draft.operation_attempted = true;
    if (normalized === "sent") {
      state.draft.from_choices = [];
      state.fromChoices = [];
      state.draft.from = null;
      state.draft.to = [];
      state.draft.cc = [];
      state.draft.bcc = [];
      state.draft.subject = "";
      state.draft.body_text = "";
      state.draft.operation_payload = null;
      state.draft.require_from_choice = false;
      composerCard.dataset.state = "sent";
      if (state.draft.discarded === true) {
        showComposerMessage("Reply text was discarded. Pimcamp confirmed submission; delivery is not confirmed.", "success");
        showDraftState("Reply text discarded; submission confirmed", "saved");
      } else {
        showComposerMessage("Pimcamp confirmed submission. Delivery is not confirmed.", "success");
        showDraftState("Submitted", "saved");
      }
    } else if (state.draft.discarded === true) {
      if (normalized === "not_submitted" && state.draft.retry_eligible) {
        showComposerMessage("Reply text was discarded. This operation did not submit; no reply text remains to retry.");
        showDraftState("Reply text discarded; not submitted", "saved");
      } else if (normalized === "not_submitted") {
        showComposerMessage("Reply text was discarded. Pimcamp did not confirm retry eligibility; keep the operation and do not retry.", "error");
        showDraftState("Reply text discarded; retry unavailable", "error");
      } else if (normalized === "pending" || normalized === "absent") {
        showComposerMessage("Reply text was discarded. The operation is unresolved; discarding cannot cancel a send. Keep checking status and do not retry.");
        showDraftState("Reply text discarded; status unresolved");
      } else {
        showComposerMessage("Reply text was discarded. Pimcamp cannot confirm whether the reply was submitted; discarding cannot cancel a send.", "error");
        showDraftState("Reply text discarded; outcome unknown", "error");
      }
    } else if (normalized === "not_submitted" && state.draft.retry_eligible && state.draft.operation_attempted) {
      composerCard.dataset.state = "ready";
      if (mailState.needsNewOperation({
        status: normalized,
        retryEligible: state.draft.retry_eligible,
        operationAttempted: state.draft.operation_attempted,
        operationPayload: state.draft.operation_payload,
        reviewedPayload: currentReviewedReply()
      })) {
        showComposerMessage("Pimcamp confirmed this operation did not submit. Correct the reply, prepare a new operation, then review and explicitly send it.");
        showDraftState("Draft saved; corrected reply needs a new operation", "saved");
      } else {
        showComposerMessage("Pimcamp confirmed this operation did not submit. Review the reply and explicitly retry if you still want to send it.");
        showDraftState("Draft saved; safe retry is available", "saved");
      }
    } else if (normalized === "not_submitted" && state.draft.retry_eligible) {
      composerCard.dataset.state = "ready";
      showComposerMessage("The reply is prepared but has not been sent. Review it, then choose Send reply if you want to submit it.");
      showDraftState("Draft saved; ready for your review", "saved");
    } else if (normalized === "not_submitted") {
      showComposerMessage("Pimcamp has not confirmed that this operation is eligible for retry. Keep this operation; do not retry.", "error");
      showDraftState("Retry eligibility not confirmed", "error");
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
      await handleOperationStatus(result.status, result.retry_eligible);
    } catch (error) {
      if (isSessionError(error)) {
        await requireRenewal();
      } else {
        state.draft.operation_status = "unknown";
        state.draft.retry_eligible = false;
        showDraftState("Send outcome unknown", "error");
        if (error?.status === 403 && error?.code === "permission_denied") {
          hidePrivateContent();
          setGate("Mail account unavailable", publicError(error), "retry");
        } else {
          showComposerMessage(publicError({ code: "status_unavailable" }), "error");
        }
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

    if (stored?.discarded === true) {
      state.prepared = false;
      state.fromChoices = [];
      state.draft = {
        ...stored,
        principal_id: state.principalId,
        account_id: state.accountId,
        locator: state.locator,
        from_choices: [],
        from: null,
        to: [],
        cc: [],
        bcc: [],
        subject: "",
        body_text: "",
        operation_payload: null,
        operation_attempted: true,
        retry_eligible: false,
        discarded: true
      };
      byId("composer-card").hidden = false;
      byId("reply-form").hidden = false;
      byId("new-reply").hidden = true;
      clearReplyFields();
      showComposerMessage("Reply text was discarded. The send-operation status is retained.");
      showDraftState("Reply text discarded; checking send status");
      updateSendButton();
      if (state.draft.operation_id) await querySendStatus();
      return;
    }

    let prepared = null;
    if (stored?.operation_id) {
      state.prepared = true;
      state.fromChoices = choicesFrom(stored.from_choices);
    } else {
      try {
        prepared = await client.prepareReply(state.locator, stored?.body_text || "");
        if (prepared.account_id !== state.accountId || prepared.locator !== state.locator) {
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
    }

    const reply = prepared?.reply && typeof prepared.reply === "object" ? prepared.reply : {};
    if (!stored?.operation_id) state.fromChoices = choicesFrom(reply.from_choices);
    const savedFrom = stored?.from || null;
    const chosenFrom = stored?.require_from_choice === true
      ? mailState.matchFrom(savedFrom, state.fromChoices)
      : mailState.choosePreparedFrom(savedFrom, reply.selected_from, state.fromChoices);
    const draft = {
      principal_id: state.principalId,
      account_id: state.accountId,
      locator: state.locator,
      draft_id: stored?.draft_id || uuid(),
      from_choices: state.fromChoices,
      from: chosenFrom || (stored?.require_from_choice === true ? null : stored?.from || null),
      to: Array.isArray(stored?.to) ? stored.to : (reply.to || []),
      cc: Array.isArray(stored?.cc) ? stored.cc : (reply.cc || []),
      bcc: Array.isArray(stored?.bcc) ? stored.bcc : (reply.bcc || []),
      subject: typeof stored?.subject === "string" ? stored.subject : (reply.subject || ""),
      body_text: typeof stored?.body_text === "string" ? stored.body_text : (reply.body_text || ""),
      operation_id: stored?.operation_id || prepared?.operation_id || null,
      operation_payload: stored?.operation_payload || null,
      require_from_choice: stored?.require_from_choice === true,
      operation_status: stored?.operation_id
        ? mailState.normalizeStatus(stored.operation_status || "unknown")
        : null,
      retry_eligible: false,
      operation_attempted: Boolean(stored?.operation_attempted),
      discarded: false
    };
    if (!draft.subject && state.sourceSubject) {
      draft.subject = /^re:/i.test(state.sourceSubject) ? state.sourceSubject : `Re: ${state.sourceSubject}`;
    }
    state.draft = draft;
    applyDraftToForm(draft);
    byId("composer-card").hidden = false;

    if (state.prepared && state.storageReady) await queueSave();
    else if (!state.storageReady) showDraftState("Draft recovery unavailable; keep this page open", "error");
    if (state.draft.operation_id) await querySendStatus();
  }

  async function loadMessage({ recordForegroundActivity = false } = {}) {
    if (!state.locator) {
      setGate("Message link unavailable", "This link does not contain a valid message reference.", "invalid");
      return;
    }
    if (!foregroundVisible()) {
      state.resumeCheckPending = true;
      return;
    }
    if (recordForegroundActivity && !(await ensureSessionActivity())) {
      if (!sessionExpired()) {
        const detail = foregroundVisible()
          ? publicError(state.activityError)
          : "Bring this tab to the foreground to check the mail session.";
        setGate("Message could not be opened", detail, "retry");
      }
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
      setGate("Message could not be opened", publicError(error), "retry");
    }
  }

  async function acceptSession(payload) {
    if (payload.status !== "authenticated" || typeof payload.principal_id !== "string" || !payload.principal_id ||
        typeof payload.csrf_token !== "string" || !payload.csrf_token || !Array.isArray(payload.accounts) ||
        !payload.accounts.length || !validSessionShape(payload)) {
      throw new Error("The mail session response is incomplete.");
    }
    const accounts = new Map();
    for (const account of payload.accounts) {
      if (!account || typeof account.account_id !== "string" || !account.account_id ||
          typeof account.label !== "string" || !account.label || accounts.has(account.account_id)) {
        throw new Error("The mail session response is incomplete.");
      }
      accounts.set(account.account_id, account);
    }
    state.principalId = payload.principal_id;
    state.authorizedAccounts = accounts;
    state.csrfToken = payload.csrf_token;
    state.idleDeadline = payload.idle_deadline;
    state.absoluteDeadline = payload.absolute_deadline;
    client.setCsrfToken(payload.csrf_token);
    saveSessionSnapshot();
    scheduleDeadline();
  }

  async function requireRenewal({
    title = "Mail session expired",
    message = "Your reply and send-operation identity are preserved in this browser. Continue your mail session to reopen the message and check any send operation."
  } = {}) {
    if (state.renewalPromise) return state.renewalPromise;
    state.renewalPromise = (async () => {
      const save = state.draft ? queueSave() : Promise.resolve(true);
      clearSessionSnapshot();
      state.authorizedAccounts = new Map();
      client.setCsrfToken("");
      hidePrivateContent({ clearAuthorization: true });
      setGate(title, message, "reauth");
      const saved = await save;
      if (!saved && state.draft) {
        setGate(title, "Browser storage could not confirm recovery. The reply remains only in this tab's memory; keep this tab open and continue your mail session to reopen it.", "reauth");
      }
    })();
    try { await state.renewalPromise; }
    finally { state.renewalPromise = null; }
  }

  function hidePrivateContent({ clearAuthorization = false } = {}) {
    view.hidden = true;
    byId("source-from").replaceChildren();
    byId("source-to").replaceChildren();
    byId("source-cc").replaceChildren();
    byId("source-cc-row").hidden = true;
    byId("source-reply-to").replaceChildren();
    byId("source-reply-to-row").hidden = true;
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
    if (clearAuthorization) state.authorizedAccounts = new Map();
    state.prepared = false;
    state.storageReady = false;
    state.fromChoices = [];
    updateFormAvailability();
  }

  async function resumeSession() {
    if (state.busy) return;
    if (!foregroundVisible()) return;
    state.busy = true;
    setGate("Opening your message", "Checking your mail session…", "loading");
    try {
      const session = await client.openSession();
      await acceptSession(session);
      state.logoutPending = false;
      saveSessionSnapshot();
      if (sessionExpired()) {
        await requireRenewal();
        return;
      }
      await loadMessage();
    } catch (error) {
      const auth = isSessionError(error);
      setGate(auth ? "Mail session needed" : "Mail could not be reached", auth
        ? "Continue through the approved private sign-in to view this message."
        : publicError(error), auth ? "reauth" : "retry");
    } finally {
      state.busy = false;
      updateSendButton();
      if (state.resumeCheckPending && foregroundVisible()) {
        state.resumeCheckPending = false;
        void validateSessionOnResume();
      }
    }
  }

  async function validateSessionOnResume({ recordForegroundActivity = false } = {}) {
    if (state.logoutPending) return;
    if (!foregroundVisible()) {
      state.resumeCheckPending = true;
      return;
    }
    if (state.busy || state.submitting || state.reconciling) {
      state.resumeCheckPending = true;
      return;
    }
    state.resumeCheckPending = false;
    if (!state.principalId || sessionExpired()) {
      await requireRenewal();
      return;
    }
    state.busy = true;
    hidePrivateContent();
    setGate("Checking your mail session", "Checking authorization before showing this message. This check does not extend the session.", "loading");
    try {
      await loadMessage({ recordForegroundActivity });
    } finally {
      state.busy = false;
      updateSendButton();
      if (state.resumeCheckPending && foregroundVisible()) {
        state.resumeCheckPending = false;
        void validateSessionOnResume();
      }
    }
  }

  async function logoutSession(event) {
    event?.preventDefault();
    if (state.busy || state.submitting || state.reconciling || sessionExpired()) {
      if (sessionExpired()) await requireRenewal();
      return;
    }
    state.busy = true;
    updateSendButton();
    try {
      if (state.draft && state.formBound && !(await queueSave())) {
        showComposerMessage("Save the reply in this browser before ending the mail session.", "error");
        return;
      }
      if (!(await ensureSessionActivity())) {
        if (!sessionExpired()) {
          state.logoutPending = true;
          saveSessionSnapshot();
          hidePrivateContent();
          const detail = state.activityError?.code === "permission_denied"
            ? publicError(state.activityError)
            : "The private session could not be confirmed before logout. Private content is hidden; retry when the connection is available.";
          setGate("Logout not confirmed", detail, "logout-retry");
        }
        return;
      }
      await client.logout();
      clearSessionSnapshot();
      client.setCsrfToken("");
      state.authorizedAccounts = new Map();
      state.principalId = "";
      state.logoutPending = false;
      hidePrivateContent({ clearAuthorization: true });
      setGate("Mail session ended", "Your local reply and send-operation records are preserved in this browser. Continue mail session to reopen the message.", "reauth");
    } catch (error) {
      if (isSessionError(error)) {
        await requireRenewal();
      } else {
        state.logoutPending = true;
        saveSessionSnapshot();
        hidePrivateContent();
        setGate("Logout not confirmed", "Private content is hidden, but Pimcamp could not confirm that the mail session ended. Retry logout when the connection is available.", "logout-retry");
      }
    } finally {
      state.busy = false;
      updateSendButton();
    }
  }

  async function retryGateAction() {
    if (state.logoutPending && !sessionExpired()) return logoutSession();
    if (state.principalId && !sessionExpired()) return validateSessionOnResume({ recordForegroundActivity: true });
    return resumeSession();
  }

  async function boot() {
    if (!state.locator) {
      setGate("Message link unavailable", "This link does not contain a valid message reference.", "invalid");
      return;
    }
    if (!foregroundVisible()) {
      state.bootPending = true;
      setGate("Mail tab is paused", "Bring this tab to the foreground before checking the private message.", "loading");
      return;
    }
    state.bootPending = false;
    const cached = readSessionSnapshot();
    const navigationType = window.performance?.getEntriesByType?.("navigation")?.[0]?.type || "navigate";
    if (!cached) {
      if (navigationType === "reload" || navigationType === "back_forward") {
        setGate("Mail session needed", "Continue your mail session to check access before reopening this message.", "reauth");
      } else {
        await resumeSession();
      }
      return;
    }
    state.busy = true;
    try {
      state.logoutPending = cached.logout_pending === true;
      await acceptSession(cached);
      if (state.logoutPending) {
        if (sessionExpired()) {
          await requireRenewal();
        } else {
          setGate("Logout not confirmed", "Private content stays hidden until you retry ending the mail session or continue after the session expires.", "logout-retry");
        }
        return;
      }
      if (sessionExpired()) {
        await requireRenewal();
        return;
      }
      await loadMessage({ recordForegroundActivity: true });
    } finally {
      state.busy = false;
      updateSendButton();
      if (state.resumeCheckPending && foregroundVisible()) {
        state.resumeCheckPending = false;
        void validateSessionOnResume();
      }
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState === "hidden") {
      if (state.draft && state.formBound) void queueSave();
      return;
    }
    if (state.bootPending) {
      void boot();
      return;
    }
    if (state.logoutPending) {
      if (sessionExpired()) void requireRenewal();
      return;
    }
    if (state.principalId) {
      void validateSessionOnResume();
    }
  }

  async function discardDraft(event) {
    event.preventDefault();
    if (!state.draft || state.draft.discarded === true || state.busy || state.submitting || state.reconciling || state.discarding) return;
    const mustRetainOperation = state.draft.operation_attempted === true ||
      ["pending", "unknown"].includes(state.draft.operation_status);
    const prompt = mustRetainOperation
      ? "Discard the reply text? This keeps the send-operation record. It cannot cancel a message that may already have been submitted."
      : "Discard this unsent reply from this browser? No email will be sent.";
    if (!window.confirm(prompt)) return;

    state.discarding = true;
    updateSendButton();
    try {
      const activityAccepted = await ensureSessionActivity();
      if (!activityAccepted && sessionExpired()) return;
      await state.saving.catch(() => undefined);
      if (mustRetainOperation) {
        const marker = {
          principal_id: state.draft.principal_id,
          account_id: state.draft.account_id,
          locator: state.draft.locator,
          draft_id: state.draft.draft_id,
          operation_id: state.draft.operation_id,
          operation_status: state.draft.operation_status,
          operation_attempted: true,
          discarded: true
        };
        await draftStore.put(marker);
        state.draft = {
          ...marker,
          from_choices: [],
          from: null,
          to: [],
          cc: [],
          bcc: [],
          subject: "",
          body_text: "",
          operation_payload: null,
          retry_eligible: false
        };
        state.fromChoices = [];
        state.prepared = false;
        state.storageReady = true;
        byId("reply-form").hidden = false;
        byId("new-reply").hidden = true;
        clearReplyFields();
        composerCard.hidden = false;
        composerCard.dataset.state = "discarded";
        showComposerMessage("Reply text was discarded. This does not cancel a message that may have been submitted; the operation status is retained.");
        showDraftState("Reply text discarded; operation retained", "saved");
        if (state.draft.operation_id) await querySendStatus();
      } else {
        await draftStore.delete(state.draft.principal_id, state.draft.account_id, state.draft.locator);
        state.draft = null;
        state.fromChoices = [];
        state.prepared = false;
        state.storageReady = true;
        state.formBound = false;
        clearReplyFields();
        form.hidden = true;
        byId("new-reply").hidden = false;
        composerCard.hidden = false;
        composerCard.dataset.state = "discarded";
        showComposerMessage("Reply draft discarded. No email was sent.");
        showDraftState("No draft saved", "saved");
        showNotice("", "");
      }
    } catch (_) {
      state.storageReady = false;
      showComposerMessage("Pimcamp could not remove this reply from browser storage. The text is still here and sending is disabled.", "error");
      showDraftState("Draft was not removed", "error");
    } finally {
      state.discarding = false;
      updateSendButton();
    }
  }

  async function startNewReply(event) {
    event.preventDefault();
    if (state.draft || state.submitting || state.busy || !state.storageReady) return;
    state.submitting = true;
    byId("new-reply").disabled = true;
    showComposerMessage("Preparing a new reply. No email will be sent.");
    try {
      if (!(await ensureSessionActivity())) return;
      const prepared = await client.prepareReply(state.locator, "");
      if (prepared.account_id !== state.accountId || prepared.locator !== state.locator) {
        throw new Error("The reply account did not match the source account.");
      }
      const reply = prepared.reply;
      state.fromChoices = choicesFrom(reply.from_choices);
      state.prepared = true;
      state.draft = {
        principal_id: state.principalId,
        account_id: state.accountId,
        locator: state.locator,
        draft_id: uuid(),
        from_choices: state.fromChoices,
        from: mailState.choosePreparedFrom(null, reply.selected_from, state.fromChoices),
        to: Array.isArray(reply.to) ? reply.to : [],
        cc: Array.isArray(reply.cc) ? reply.cc : [],
        bcc: Array.isArray(reply.bcc) ? reply.bcc : [],
        subject: typeof reply.subject === "string" ? reply.subject : "",
        body_text: typeof reply.body_text === "string" ? reply.body_text : "",
        operation_id: prepared.operation_id,
        operation_payload: null,
        require_from_choice: false,
        operation_status: null,
        retry_eligible: false,
        operation_attempted: false,
        discarded: false
      };
      if (!state.draft.subject && state.sourceSubject) {
        state.draft.subject = /^re:/i.test(state.sourceSubject) ? state.sourceSubject : "Re: " + state.sourceSubject;
      }
      byId("new-reply").hidden = true;
      byId("new-reply").disabled = false;
      form.hidden = false;
      composerCard.hidden = false;
      applyDraftToForm(state.draft);
      if (await queueSave()) await querySendStatus();
    } catch (error) {
      if (isSessionError(error)) await requireRenewal();
      else showComposerMessage(publicError(error), "error");
    } finally {
      state.submitting = false;
      byId("new-reply").disabled = false;
      updateSendButton();
    }
  }

  async function prepareCorrectedReply(reviewedReply) {
    const previousDraftFrom = state.draft?.from || null;
    const previousFromWasUnverified = Boolean(previousDraftFrom) && !reviewedReply.from;
    state.submitting = true;
    updateSendButton();
    showComposerMessage("Preparing a corrected reply. No email will be sent.");
    try {
      if (!(await queueSave())) return;
      if (!(await ensureSessionActivity())) return;
      const prepared = await client.prepareReply(state.locator, reviewedReply.body_text);
      if (prepared.account_id !== state.accountId || prepared.locator !== state.locator) {
        throw new Error("The reply account did not match the source account.");
      }
      const reply = prepared.reply;
      state.fromChoices = choicesFrom(reply.from_choices);
      const refreshedFrom = previousDraftFrom
        ? mailState.chooseFrom(previousDraftFrom, state.fromChoices)
        : mailState.choosePreparedFrom(null, reply.selected_from, state.fromChoices);
      const mustChooseFromAgain = previousFromWasUnverified || Boolean(previousDraftFrom && !refreshedFrom);
      state.draft = {
        ...state.draft,
        from_choices: state.fromChoices,
        from: mustChooseFromAgain ? null : refreshedFrom || previousDraftFrom || null,
        to: reviewedReply.to.map(item => ({ ...item })),
        cc: reviewedReply.cc.map(item => ({ ...item })),
        bcc: reviewedReply.bcc.map(item => ({ ...item })),
        subject: reviewedReply.subject,
        body_text: reviewedReply.body_text,
        operation_id: prepared.operation_id,
        operation_payload: null,
        require_from_choice: mustChooseFromAgain,
        operation_status: null,
        retry_eligible: false,
        operation_attempted: false,
        discarded: false
      };
      state.prepared = true;
      applyDraftToForm(state.draft);
      showComposerMessage("A new operation is prepared. Nothing was sent. Review the From and all reply fields before Send.");
      if (await queueSave()) await querySendStatus();
    } catch (error) {
      if (isSessionError(error)) await requireRenewal();
      else {
        showComposerMessage(publicError(error), "error");
        updateSendButton();
      }
    } finally {
      state.submitting = false;
      updateSendButton();
    }
  }

  async function submitReply(event) {
    event.preventDefault();
    if (!state.draft || statusBlocksSubmit()) return;
    syncDraftFromForm();
    const reviewedReply = currentReviewedReply();
    const recipients = [...reviewedReply.to, ...reviewedReply.cc, ...reviewedReply.bcc];
    if (mailState.needsNewOperation({
      status: state.draft.operation_status,
      retryEligible: state.draft.retry_eligible,
      operationAttempted: state.draft.operation_attempted,
      operationPayload: state.draft.operation_payload,
      reviewedPayload: reviewedReply,
      discarded: state.draft.discarded
    })) {
      await prepareCorrectedReply(reviewedReply);
      return;
    }
    if (!reviewedReply.from || !recipients.length || !reviewedReply.subject.trim()) {
      showComposerMessage("Choose a verified From identity and at least one recipient, then review the reply.", "error");
      updateSendButton();
      return;
    }
    const previousOperationId = state.draft.operation_id;
    const previousOperationStatus = state.draft.operation_status;
    const previousRetryEligible = state.draft.retry_eligible;
    const previousAttempted = state.draft.operation_attempted;
    const previousOperationPayload = state.draft.operation_payload;
    state.submitting = true;
    updateSendButton();
    showComposerMessage("Submitting the reviewed reply…");
    try {
      if (!(await queueSave())) return;
      if (!(await ensureSessionActivity())) return;
      const operationId = mailState.operationIdForAttempt(previousOperationId);
      state.draft.operation_id = operationId;
      state.draft.operation_status = "sending";
      state.draft.retry_eligible = false;
      state.draft.operation_attempted = true;
      state.draft.operation_payload = reviewedReply;
      if (!(await queueSave())) {
        state.draft.operation_id = previousOperationId;
        state.draft.operation_status = previousOperationStatus;
        state.draft.retry_eligible = previousRetryEligible;
        state.draft.operation_attempted = previousAttempted;
        state.draft.operation_payload = previousOperationPayload;
        updateSendButton();
        return;
      }

      try {
        const result = await client.submitReply(operationId, reviewedReply);
        const status = mailState.normalizeStatus(result.status);
        await handleOperationStatus(status, false);
        if (status !== "sent") await querySendStatus();
      } catch (error) {
        if (isSessionError(error)) {
          state.draft.operation_status = "unknown";
          state.draft.retry_eligible = false;
          await queueSave();
          await requireRenewal();
        } else {
          state.draft.operation_status = "unknown";
          state.draft.retry_eligible = false;
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

  form.addEventListener("input", () => { queueSave(); scheduleEditingActivity(); });
  form.addEventListener("change", () => { queueSave(); scheduleEditingActivity(); });
  form.addEventListener("submit", submitReply);
  byId("discard-draft").addEventListener("click", discardDraft);
  byId("new-reply").addEventListener("click", startNewReply);
  byId("logout-session").addEventListener("click", logoutSession);
  byId("continue-session").addEventListener("click", resumeSession);
  byId("retry-open").addEventListener("click", retryGateAction);
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("pageshow", event => { if (event.persisted) onVisibilityChange(); });
  void boot();
})();
