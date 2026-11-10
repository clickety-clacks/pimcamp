(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.PimcampMailDrafts = api;
    root.PimcampMailState = api.state;
  }
})(globalThis, function () {
  "use strict";

  const databaseName = "pimcamp-mail-drafts-v1";
  const storeName = "drafts";
  const statuses = new Set(["absent", "pending", "sent", "not_submitted", "unknown"]);

  function normalizeStatus(status) {
    return statuses.has(status) ? status : "unknown";
  }

  function canEditDraft({ status, operationId, retryEligible = false, discarded = false }) {
    if (discarded) return false;
    if (!operationId) return status == null || status === "";
    return status === "not_submitted" && retryEligible === true;
  }

  function canSubmit({ status, operationId, retryEligible = false, discarded = false, prepared, storageReady, busy }) {
    if (discarded || !prepared || !storageReady || busy || typeof operationId !== "string" || !operationId) return false;
    return status === "not_submitted" && retryEligible === true;
  }

  function operationIdForAttempt(preparedOperationId) {
    if (typeof preparedOperationId !== "string" || !preparedOperationId) {
      throw new TypeError("A prepared operation ID is required.");
    }
    return preparedOperationId;
  }

  function matchFrom(preferred, choices) {
    if (!preferred?.address) return null;
    return choices.find(choice => choice?.address && choice.address.toLowerCase() === preferred.address.toLowerCase()) || null;
  }

  function chooseFrom(preferred, choices) {
    return matchFrom(preferred, choices) || (!preferred?.address && choices.length === 1 ? choices[0] : null);
  }

  function choosePreparedFrom(savedChoice, serverChoice, choices) {
    const saved = matchFrom(savedChoice, choices);
    if (saved) return saved;
    if (choices.length !== 1) return null;
    return matchFrom(serverChoice, choices) || choices[0];
  }

  function copyReviewedPayload(value) {
    if (!value || typeof value !== "object") return null;
    return {
      account_id: typeof value.account_id === "string" ? value.account_id : "",
      locator: typeof value.locator === "string" ? value.locator : "",
      from: copyAddress(value.from),
      to: copyAddresses(value.to),
      cc: copyAddresses(value.cc),
      bcc: copyAddresses(value.bcc),
      subject: typeof value.subject === "string" ? value.subject : "",
      body_text: typeof value.body_text === "string" ? value.body_text : ""
    };
  }

  function reviewedPayloadSignature(value) {
    const payload = copyReviewedPayload(value);
    return payload ? JSON.stringify(payload) : null;
  }

  function sameReviewedPayload(left, right) {
    const leftSignature = reviewedPayloadSignature(left);
    return leftSignature !== null && leftSignature === reviewedPayloadSignature(right);
  }

  function needsNewOperation({ status, retryEligible = false, operationAttempted = false, operationPayload, reviewedPayload, discarded = false }) {
    return !discarded && operationAttempted === true && status === "not_submitted" && retryEligible === true &&
      !sameReviewedPayload(operationPayload, reviewedPayload);
  }

  const state = {
    canEditDraft,
    canSubmit,
    chooseFrom,
    choosePreparedFrom,
    matchFrom,
    needsNewOperation,
    normalizeStatus,
    operationIdForAttempt,
    sameReviewedPayload
  };

  class DraftStorageError extends Error {
    constructor(message = "Draft recovery is unavailable in this browser.") {
      super(message);
      this.name = "DraftStorageError";
      this.code = "draft_storage_unavailable";
    }
  }

  function keyFor(principalId, accountId, locator) {
    if (![principalId, accountId, locator].every(value => typeof value === "string" && value.length > 0)) {
      throw new DraftStorageError("The private draft identity is incomplete.");
    }
    return JSON.stringify([principalId, accountId, locator]);
  }

  function copyAddress(value) {
    if (typeof value === "string") return { name: null, address: value };
    if (!value || typeof value !== "object" || typeof value.address !== "string") return null;
    return { name: typeof value.name === "string" ? value.name : null, address: value.address };
  }

  function copyAddresses(values) {
    return Array.isArray(values) ? values.map(copyAddress).filter(Boolean) : [];
  }

  function copyFromChoice(value) {
    const address = copyAddress(value);
    if (!address) return null;
    return {
      ...address,
      provenance: typeof value.provenance === "string" ? value.provenance : "",
      validity: typeof value.validity === "string" ? value.validity : ""
    };
  }

  // Explicitly whitelist reviewed values and verified sender choices; source bodies and credentials stay out of storage.
  function createDraftRecord(draft, now = Date.now()) {
    const key = keyFor(draft.principal_id, draft.account_id, draft.locator);
    if (draft.discarded === true) {
      return {
        key,
        principal_id: draft.principal_id,
        account_id: draft.account_id,
        locator: draft.locator,
        draft_id: typeof draft.draft_id === "string" ? draft.draft_id : "",
        operation_id: typeof draft.operation_id === "string" ? draft.operation_id : null,
        operation_status: typeof draft.operation_status === "string" ? draft.operation_status : null,
        operation_attempted: true,
        discarded: true,
        updated_at: now
      };
    }
    return {
      key,
      principal_id: draft.principal_id,
      account_id: draft.account_id,
      locator: draft.locator,
      draft_id: typeof draft.draft_id === "string" ? draft.draft_id : "",
      from_choices: Array.isArray(draft.from_choices) ? draft.from_choices.map(copyFromChoice).filter(Boolean) : [],
      from: copyAddress(draft.from),
      to: copyAddresses(draft.to),
      cc: copyAddresses(draft.cc),
      bcc: copyAddresses(draft.bcc),
      subject: typeof draft.subject === "string" ? draft.subject : "",
      body_text: typeof draft.body_text === "string" ? draft.body_text : "",
      operation_id: typeof draft.operation_id === "string" ? draft.operation_id : null,
      operation_status: typeof draft.operation_status === "string" ? draft.operation_status : null,
      operation_payload: copyReviewedPayload(draft.operation_payload),
      operation_attempted: Boolean(draft.operation_attempted),
      require_from_choice: draft.require_from_choice === true,
      updated_at: now
    };
  }

  function createDraftStore({ indexedDBImpl = globalThis.indexedDB, now = Date.now } = {}) {
    let databasePromise;

    function open() {
      if (!indexedDBImpl) return Promise.reject(new DraftStorageError());
      if (databasePromise) return databasePromise;
      databasePromise = new Promise((resolve, reject) => {
        let request;
        try { request = indexedDBImpl.open(databaseName, 1); }
        catch (_) { reject(new DraftStorageError()); return; }
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(storeName)) {
            db.createObjectStore(storeName, { keyPath: "key" });
          }
        };
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => db.close();
          resolve(db);
        };
        request.onerror = () => reject(new DraftStorageError());
        request.onblocked = () => reject(new DraftStorageError());
      }).catch(error => {
        databasePromise = null;
        throw error instanceof DraftStorageError ? error : new DraftStorageError();
      });
      return databasePromise;
    }

    function transact(mode, action) {
      return open().then(db => new Promise((resolve, reject) => {
        let transaction;
        let result;
        try {
          transaction = db.transaction(storeName, mode);
          const request = action(transaction.objectStore(storeName));
          if (request) {
            request.onsuccess = () => { result = request.result; };
            request.onerror = () => reject(new DraftStorageError());
          }
          transaction.oncomplete = () => resolve(result);
          transaction.onerror = () => reject(new DraftStorageError());
          transaction.onabort = () => reject(new DraftStorageError());
        } catch (_) {
          reject(new DraftStorageError());
        }
      }));
    }

    return {
      get(principalId, accountId, locator) {
        const key = keyFor(principalId, accountId, locator);
        return transact("readonly", store => store.get(key));
      },
      put(draft) {
        const record = createDraftRecord(draft, now());
        return transact("readwrite", store => store.put(record)).then(() => record);
      },
      delete(principalId, accountId, locator) {
        const key = keyFor(principalId, accountId, locator);
        return transact("readwrite", store => store.delete(key));
      }
    };
  }

  return { DraftStorageError, createDraftRecord, createDraftStore, keyFor, ...state, state };
});
