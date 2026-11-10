(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PimcampMailClient = api;
})(globalThis, function () {
  "use strict";

  class MailApiError extends Error {
    constructor(code, message, status = 0, payload = null) {
      super(message);
      this.name = "MailApiError";
      this.code = code;
      this.status = status;
      this.payload = payload;
    }
  }

  function segment(value) {
    if (typeof value !== "string" || !value || value.includes("/") || value.includes("\\")) {
      throw new MailApiError("invalid_request", "The requested mail reference is invalid.");
    }
    return encodeURIComponent(value);
  }

  function invalidResponse() {
    return new MailApiError("invalid_response", "Pimcamp returned an unreadable response.");
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function createMailClient({ fetchImpl = globalThis.fetch, csrfToken = "" } = {}) {
    if (typeof fetchImpl !== "function") throw new TypeError("A fetch implementation is required.");
    let csrf = typeof csrfToken === "string" ? csrfToken : "";

    async function request(path, { method = "GET", body, needsCsrf = false } = {}) {
      const headers = { Accept: "application/json" };
      const options = { method, credentials: "same-origin", cache: "no-store", headers };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      if (needsCsrf) {
        if (!csrf) throw new MailApiError("session_expired", "Continue your mail session to proceed.", 401);
        headers["X-Pimcamp-CSRF"] = csrf;
      }

      let response;
      try {
        response = await fetchImpl(path, options);
      } catch (_) {
        throw new MailApiError(method === "POST" && path.includes("/submit") ? "outcome_unknown" : "network_error",
          method === "POST" && path.includes("/submit")
            ? "The send response was lost. Check its status before trying again."
            : "Pimcamp could not be reached. Your local draft is preserved.");
      }

      if (response.redirected) {
        throw new MailApiError("session_expired", "Continue your mail session to proceed.", response.status);
      }

      let payload = null;
      try { payload = await response.json(); } catch (_) { /* handled as a structured API failure below */ }
      if (!response.ok) {
        const error = payload?.error || {};
        const code = typeof error.code === "string" ? error.code : (response.status === 401 ? "session_expired" : "request_failed");
        const message = typeof error.message === "string" ? error.message : "Pimcamp could not complete this request.";
        throw new MailApiError(code, message, response.status, payload);
      }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new MailApiError("invalid_response", "Pimcamp returned an unreadable response.", response.status);
      }
      return payload;
    }

    return {
      setCsrfToken(value) { csrf = typeof value === "string" ? value : ""; },
      async openSession() {
        const payload = await request("/mail/api/session", { method: "POST", body: {} });
        const accounts = payload.accounts;
        if (payload.status !== "authenticated" || typeof payload.principal_id !== "string" || !payload.principal_id ||
            typeof payload.csrf_token !== "string" || !payload.csrf_token || !Array.isArray(accounts) || !accounts.length ||
            accounts.some(account => !isObject(account) || typeof account.account_id !== "string" || !account.account_id ||
              typeof account.label !== "string" || !account.label)) {
          throw invalidResponse();
        }
        return payload;
      },
      async getMessage(locator) {
        const payload = await request(`/mail/api/v2/messages/${segment(locator)}`);
        if (payload.locator !== locator || typeof payload.account_id !== "string" || !payload.account_id ||
            typeof payload.account_label !== "string" || !payload.account_label || !isObject(payload.source) ||
            !Array.isArray(payload.body_parts)) {
          throw invalidResponse();
        }
        return payload;
      },
      async prepareReply(locator, bodyText) {
        const payload = await request("/mail/api/v2/replies/prepare", {
          method: "POST", needsCsrf: true, body: { locator, body_text: String(bodyText ?? "") }
        });
        const reply = payload.reply;
        if (typeof payload.operation_id !== "string" || !payload.operation_id ||
            typeof payload.account_id !== "string" || !payload.account_id || payload.locator !== locator ||
            typeof payload.account_label !== "string" || !payload.account_label || !isObject(payload.source) ||
            !isObject(reply) || !Array.isArray(reply.from_choices) || !Object.prototype.hasOwnProperty.call(reply, "selected_from") ||
            (reply.selected_from !== null && !isObject(reply.selected_from)) ||
            !Array.isArray(reply.to) || !Array.isArray(reply.cc) || !Array.isArray(reply.bcc) ||
            typeof reply.subject !== "string" || typeof reply.body_text !== "string") {
          throw invalidResponse();
        }
        return payload;
      },
      async submitReply(operationId, reviewedReply) {
        const id = segment(operationId);
        const fields = {
          account_id: reviewedReply.account_id,
          locator: reviewedReply.locator,
          from: reviewedReply.from,
          to: reviewedReply.to,
          cc: reviewedReply.cc,
          bcc: reviewedReply.bcc,
          subject: reviewedReply.subject,
          body_text: reviewedReply.body_text
        };
        const payload = await request(`/mail/api/v2/replies/${id}/submit`, {
          method: "POST", needsCsrf: true, body: fields
        });
        if (typeof payload.status !== "string" ||
            (payload.operation_id !== undefined && payload.operation_id !== operationId)) {
          throw invalidResponse();
        }
        return payload;
      },
      async sendStatus(accountId, operationId) {
        const payload = await request(`/mail/api/v2/accounts/${segment(accountId)}/send-status/${segment(operationId)}`);
        if (payload.account_id !== accountId || payload.operation_id !== operationId ||
            typeof payload.status !== "string" || typeof payload.retry_eligible !== "boolean") {
          throw invalidResponse();
        }
        return payload;
      }
    };
  }

  return { MailApiError, createMailClient };
});
