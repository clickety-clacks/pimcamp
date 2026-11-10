(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PimcampMailState = api;
})(globalThis, function () {
  "use strict";

  const statuses = new Set(["absent", "pending", "sent", "not_submitted", "unknown"]);

  function normalizeStatus(status) {
    return statuses.has(status) ? status : "unknown";
  }

  function canEditDraft(status, operationId) {
    if (status === "not_submitted") return true;
    return !operationId && (status == null || status === "");
  }

  function canSubmit({ status, operationId, prepared, storageReady, busy }) {
    if (!prepared || !storageReady || busy) return false;
    if (operationId) return status === "not_submitted";
    return status == null || status === "";
  }

  function operationIdForAttempt(previousId, status, createId) {
    if (typeof createId !== "function") throw new TypeError("An operation ID factory is required.");
    return !previousId || status === "not_submitted" ? createId() : previousId;
  }

  function chooseFrom(preferred, choices) {
    if (preferred?.address) {
      return choices.find(choice => choice?.address && choice.address.toLowerCase() === preferred.address.toLowerCase()) || null;
    }
    return choices.length === 1 ? choices[0] : null;
  }

  return { canEditDraft, canSubmit, chooseFrom, normalizeStatus, operationIdForAttempt };
});
