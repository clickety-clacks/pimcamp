(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PimcampMailDrafts = api;
})(globalThis, function () {
  "use strict";

  const databaseName = "pimcamp-mail-drafts-v1";
  const storeName = "drafts";

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

  // Explicitly whitelist draft fields; source bodies, credentials, cookies and CSRF never enter storage.
  function createDraftRecord(draft, now = Date.now()) {
    const key = keyFor(draft.principal_id, draft.account_id, draft.locator);
    return {
      key,
      principal_id: draft.principal_id,
      account_id: draft.account_id,
      locator: draft.locator,
      draft_id: typeof draft.draft_id === "string" ? draft.draft_id : "",
      from: copyAddress(draft.from),
      to: copyAddresses(draft.to),
      cc: copyAddresses(draft.cc),
      bcc: copyAddresses(draft.bcc),
      subject: typeof draft.subject === "string" ? draft.subject : "",
      body_text: typeof draft.body_text === "string" ? draft.body_text : "",
      operation_id: typeof draft.operation_id === "string" ? draft.operation_id : null,
      operation_status: typeof draft.operation_status === "string" ? draft.operation_status : null,
      operation_attempted: Boolean(draft.operation_attempted),
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
      }
    };
  }

  return { DraftStorageError, createDraftRecord, createDraftStore, keyFor };
});
