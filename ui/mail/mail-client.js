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
      openSession() {
        return request("/mail/api/session", { method: "POST", body: {} });
      },
      getMessage(locator) {
        return request(`/mail/api/v2/messages/${segment(locator)}`);
      },
      prepareReply(locator, bodyText) {
        return request("/mail/api/v2/replies/prepare", {
          method: "POST", needsCsrf: true, body: { locator, body_text: String(bodyText ?? "") }
        });
      },
      submitReply(operationId, reviewedReply) {
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
        return request(`/mail/api/v2/replies/${id}/submit`, {
          method: "POST", needsCsrf: true, body: fields
        });
      },
      sendStatus(accountId, operationId) {
        return request(`/mail/api/v2/accounts/${segment(accountId)}/send-status/${segment(operationId)}`);
      }
    };
  }

  return { MailApiError, createMailClient };
});
