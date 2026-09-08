"""Account-setup workflow. Mail protocol and credential work stay in lower tools."""

from dataclasses import dataclass, field
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
import uuid

from .onboarding import validate_imap_setup, ImapSetup, ServerSettings
from .onboarding_lower import (check_himalaya_account, render_carillon_account,
                               render_himalaya_account, render_pimcamp_profile,
                               render_google_accounts, google_account_identity)
from .onboarding_oauth import OrtieAuthorization, OAuthSetupError, render_ortie_account
from .onboarding_store import AccountStore


class SetupError(Exception):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


@dataclass
class Attempt:
    identifier: str
    revision: str
    created: float
    fingerprint: str = ""
    directory: Path | None = None
    settings: object = field(default=None, repr=False)
    references: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    saved: bool = False
    finalized: bool = False
    reconnect: bool = False
    previous_revision: str | None = None
    client_digest: str | None = None
    grant: object = field(default=None, repr=False)
    oauth_status: str = "idle"
    identity: str | None = None


class SetupService:
    def __init__(self, store: AccountStore, credentials, state_root: Path,
                 himalaya: str, carillon: str, *, checker=check_himalaya_account,
                 runtime_config: Path | None = None, google_application=None,
                 oauth_callback="http://127.0.0.1:33281/oauth/google/callback",
                 identity_reader=google_account_identity, remote_host_label: str | None = None,
                 google_application_store=None):
        self.store, self.credentials = store, credentials
        self.state_root = state_root.absolute()
        self.himalaya, self.carillon = himalaya, carillon
        self.checker = checker
        self.runtime_config = runtime_config
        self.google_application = google_application
        self.google_application_store = google_application_store
        self.oauth = OrtieAuthorization(google_application) if google_application else None
        self.oauth_callback = oauth_callback
        self.identity_reader = identity_reader
        self.remote_host_label = remote_host_label
        self.attempts: dict[str, Attempt] = {}
        self.expired_unsaved = deque(maxlen=128)
        self.lock = threading.RLock()

    def begin(self, reconnect_id: str | None = None) -> dict:
        with self.lock:
            self.expire()
            if len(self.attempts) >= 8:
                raise SetupError("Too many unfinished setups. Finish or cancel one first.")
            previous = self.store._revision(reconnect_id).name if reconnect_id else None
            attempt = Attempt(reconnect_id or str(uuid.uuid4()), str(uuid.uuid4()), time.monotonic(),
                              reconnect=bool(reconnect_id), previous_revision=previous)
            setup_id = str(uuid.uuid4())
            self.attempts[setup_id] = attempt
            return {"setupId": setup_id, "installation": {"host": self.remote_host_label,
                    "remote": bool(self.remote_host_label),
                    "oauthClientConfigured": self.oauth is not None,
                    "oauthCallback": self.oauth_callback,
                    "credentialStorage": getattr(self.credentials, "lifetime", "fixture")}}

    def _attempt(self, setup_id):
        self.expire()
        attempt = self.attempts.get(setup_id)
        if attempt is None:
            if setup_id in self.expired_unsaved:
                raise SetupError("This unfinished setup expired. Your account details can be reused; sign in again if Google authorization expired.", code="setup_expired")
            raise SetupError("This setup is no longer available. Check Accounts for a saved account before starting again; a previous save may have completed.", code="setup_unavailable")
        attempt.created = time.monotonic()  # Expiry is inactivity, not time spent actively setting up.
        return attempt

    def expire(self):
        for key, attempt in list(self.attempts.items()):
            # Keep completed receipts longer so a delayed retry can reconcile a
            # successful save instead of starting another account. No raw secrets
            # remain in completed attempts (_cleanup cleared them at commit).
            lifetime = 86400 if attempt.saved else 900
            if time.monotonic() - attempt.created > lifetime:
                self._cleanup(attempt)
                if not attempt.saved:
                    self.expired_unsaved.append(key)
                del self.attempts[key]

    def _cleanup(self, attempt):
        if not attempt.saved:
            failed = []
            for key, reference in list(attempt.references.items()):
                if not self.credentials.remove(reference):
                    failed.append(key)
                else:
                    del attempt.references[key]
            if failed:
                raise SetupError("Temporary credentials could not be cleared. Retry cancelling setup.")
        attempt.settings = None
        attempt.grant = None
        attempt.identity = None
        attempt.oauth_status = "idle"
        if attempt.directory is not None:
            shutil.rmtree(attempt.directory)
            attempt.directory = None

    def cancel(self, setup_id):
        with self.lock:
            attempt = self.attempts.get(setup_id)
            if attempt:
                self._cleanup(attempt)
                del self.attempts[setup_id]
            return {"ok": True}

    def list_accounts(self):
        return self.store.list_accounts()

    def save_status(self, setup_id, draft):
        """Read-only reconciliation of a lost save response; never publish or recheck mail."""
        with self.lock:
            attempt = self._attempt(setup_id)
            fingerprint = hashlib.sha256((attempt.revision + json.dumps(draft, sort_keys=True)).encode()).hexdigest()
            if not attempt.finalized or fingerprint != attempt.fingerprint:
                return {"ok": False}
            return {"ok": True, "accountId": attempt.identifier,
                    "message": "Account settings are saved. The connection lost the original confirmation."}

    def google_application_status(self):
        if self.google_application:
            return {"configured": True, "helperAvailable": True,
                    "clientId": self.google_application.client_id}
        return self.google_application_store.status() if self.google_application_store else {
            "configured": False, "helperAvailable": False}

    def configure_google_application(self, raw):
        with self.lock:
            if not self.google_application_store:
                raise SetupError("Google app setup is not enabled by this launcher. Reopen the installed Pimcamp setup UI.")
            if self.google_application and not self.google_application_store.path.exists():
                raise SetupError("Google sign-in is configured by the installation owner. It was not replaced.")
            if any(attempt.grant is not None for attempt in self.attempts.values()):
                raise SetupError("Finish or cancel Google sign-in before changing app settings.")
            application = self.google_application_store.configure(raw)
            self.google_application = application
            self.oauth = OrtieAuthorization(application)
            return {"configured": True, "clientId": application.client_id}

    @staticmethod
    def _write_private(path, raw):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)

    def _token_command(self, attempt, directory):
        return [self.google_application.ortie, "--log-level", "off", "--config",
                str(directory / "ortie.toml"), "--account", attempt.identifier, "token", "get"]

    def _ortie_config(self, attempt):
        reference = attempt.references["oauth"]
        return render_ortie_account(self.google_application, attempt.identifier, self.oauth_callback,
                                   self.credentials.command(reference), self.credentials.write_command(reference))

    def begin_oauth(self, setup_id):
        with self.lock:
            attempt = self._attempt(setup_id)
            if self.oauth is None:
                raise SetupError("Google authorization needs an OAuth application configured by the installation owner.")
            if attempt.saved:
                raise SetupError("Start a reconnect before changing a saved account.")
            if attempt.grant and attempt.oauth_status == "in-progress":
                return {"authorizationUrl": attempt.grant.authorization_url}
            self._cleanup(attempt)
            attempt.fingerprint = ""
            attempt.checks.clear()
            attempt.revision = str(uuid.uuid4())
            try:
                attempt.references["oauth"] = self.credentials.create(attempt.revision, "oauth", "{}")
                attempt.directory = Path(tempfile.mkdtemp(prefix=".check-", dir=self.store.root))
                self._write_private(attempt.directory / "ortie.toml", self._ortie_config(attempt))
                attempt.grant = self.oauth.begin(attempt.directory / "ortie.toml", attempt.identifier, self.oauth_callback)
                attempt.oauth_status = "in-progress"
                return {"authorizationUrl": attempt.grant.authorization_url}
            except Exception:
                self._cleanup(attempt)
                raise

    def oauth_result(self, setup_id):
        with self.lock:
            attempt = self._attempt(setup_id)
            return {"status": attempt.oauth_status, "identity": attempt.identity}

    def cancel_oauth(self, setup_id):
        with self.lock:
            attempt = self._attempt(setup_id)
            if not attempt.saved:
                self._cleanup(attempt)
                attempt.fingerprint = ""
                attempt.checks.clear()
                attempt.oauth_status = "cancelled"
            return {"ok": True}

    def complete_oauth(self, redirected_uri):
        # OAuth callbacks have provider state, not the browser's setup cookie.
        # Match and validate state before exchanging anything or modifying keys.
        from urllib.parse import urlsplit, parse_qs
        import hmac
        with self.lock:
            self.expire()
            states = parse_qs(urlsplit(redirected_uri).query).get("state", [])
            if len(states) != 1:
                raise OAuthSetupError("The Google callback does not belong to an active setup.")
            attempt = next((item for item in self.attempts.values() if item.grant and
                            hmac.compare_digest(item.grant.state, states[0]) and
                            item.oauth_status == "in-progress"), None)
            if attempt is None:
                raise OAuthSetupError("The Google callback does not belong to an active setup.")
            try:
                status = self.oauth.resume(attempt.directory / "ortie.toml", attempt.identifier,
                                           attempt.grant, redirected_uri)
                if status == "authorized":
                    # Profile requests use the authorized token, not this placeholder identity.
                    raw, _ = render_google_accounts("pending@example.invalid", attempt.identifier,
                                                    self._token_command(attempt, attempt.directory))
                    self._write_private(attempt.directory / "identity.toml", raw)
                    attempt.identity = self.identity_reader(self.himalaya, attempt.directory / "identity.toml",
                                                            attempt.identifier)
                    attempt.grant = None
                else:
                    self._cleanup(attempt)
                attempt.oauth_status = status
                return {"status": status}
            except Exception:
                self._cleanup(attempt)
                attempt.oauth_status = "failed"
                raise

    def _prepare(self, attempt, draft):
        if isinstance(draft, dict) and draft.get("method") == "google":
            if attempt.saved or attempt.oauth_status != "authorized" or not attempt.identity:
                raise SetupError("Authorize this Google account before checking its connections.")
            if str(draft.get("email", "")).strip().casefold() != attempt.identity.casefold():
                raise SetupError("Confirm the email address returned by Google before continuing.")
            # Reuse the same name/address validation without collecting a Google password.
            validated = validate_imap_setup({"email": attempt.identity, "account_name": draft.get("name"),
                "incoming": {"host": "imap.gmail.com", "password": "oauth-reference"},
                "outgoing": {"host": "smtp.gmail.com"}})
            attempt.settings = ImapSetup(validated.email, validated.account_name,
                ServerSettings("imap.gmail.com", 993, "tls", validated.email, ""),
                ServerSettings("smtp.gmail.com", 465, "tls", validated.email, ""))
            fingerprint = hashlib.sha256((attempt.revision + json.dumps(draft, sort_keys=True)).encode()).hexdigest()
            if fingerprint != attempt.fingerprint:
                attempt.checks.clear()
                raw, _ = render_google_accounts(attempt.identity, attempt.identifier,
                                                self._token_command(attempt, attempt.directory))
                path = attempt.directory / "himalaya.toml"
                if not path.exists():
                    self._write_private(path, raw)
                attempt.fingerprint = fingerprint
            return
        if not isinstance(draft, dict) or draft.get("method") != "imap":
            raise SetupError("Google authorization is not configured for this installation.")
        incoming, outgoing = draft.get("incoming", {}), draft.get("outgoing", {})
        if not isinstance(incoming, dict) or not isinstance(outgoing, dict):
            raise SetupError("Check the incoming and outgoing settings.")
        settings = validate_imap_setup({"email": draft.get("email"), "account_name": draft.get("name"),
                                        "incoming": incoming, "outgoing": outgoing,
                                        "same_login": outgoing.get("sameLogin", True)})
        fingerprint = hashlib.sha256((attempt.revision + json.dumps(draft, sort_keys=True)).encode()).hexdigest()
        if attempt.saved:
            raise SetupError("This account is already saved. Start a reconnect to change it.")
        if attempt.fingerprint == fingerprint:
            return
        self._cleanup(attempt)
        attempt.revision = str(uuid.uuid4())
        # Fingerprint includes the final revision so it remains stable on the next request.
        attempt.fingerprint = hashlib.sha256((attempt.revision + json.dumps(draft, sort_keys=True)).encode()).hexdigest()
        attempt.checks.clear()
        attempt.settings = settings
        try:
            attempt.references["imap"] = self.credentials.create(attempt.revision, "imap", settings.incoming.password)
            attempt.references["smtp"] = self.credentials.create(attempt.revision, "smtp", settings.outgoing.password)
            attempt.directory = Path(tempfile.mkdtemp(prefix=".check-", dir=self.store.root))
            raw = render_himalaya_account(settings, attempt.identifier,
                                          self.credentials.command(attempt.references["imap"]),
                                          self.credentials.command(attempt.references["smtp"]))
            descriptor = os.open(attempt.directory / "himalaya.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
        except Exception:
            attempt.fingerprint = ""
            self._cleanup(attempt)
            raise

    def check(self, setup_id, draft, backend):
        if backend not in ("imap", "smtp"):
            raise SetupError("Unknown connection check.")
        with self.lock:
            attempt = self._attempt(setup_id)
            self._prepare(attempt, draft)
            result = self.checker(self.himalaya, attempt.directory / "himalaya.toml", attempt.identifier, backend)
            attempt.checks[backend] = result.get("ok") is True
            return result

    def commit(self, setup_id, draft):
        with self.lock:
            attempt = self._attempt(setup_id)
            fingerprint = hashlib.sha256((attempt.revision + json.dumps(draft, sort_keys=True)).encode()).hexdigest()
            if fingerprint != attempt.fingerprint:
                raise SetupError("Settings changed. Check both connections again before saving.")
            if attempt.saved:
                if self.runtime_config is not None:
                    self.store.ensure_runtime_default(attempt.identifier, self.runtime_config)
                attempt.finalized = True
                return {"ok": True, "accountId": attempt.identifier, "message": "Account settings are already saved."}
            if attempt.checks != {"imap": True, "smtp": True}:
                raise SetupError("Both incoming and outgoing sign-in checks must pass before saving.")
            settings = attempt.settings
            revision = self.store.revisions / attempt.revision
            if draft.get("method") == "google":
                operations, observation = render_google_accounts(settings.email, attempt.identifier,
                                                                 self._token_command(attempt, revision))
                files = {"himalaya.toml": operations, "carillon.toml": observation,
                         "ortie.toml": self._ortie_config(attempt)}
            else:
                incoming = self.credentials.command(attempt.references["imap"])
                outgoing = self.credentials.command(attempt.references["smtp"])
                files = {
                    "himalaya.toml": render_himalaya_account(settings, attempt.identifier, incoming, outgoing),
                    "carillon.toml": render_carillon_account(settings, attempt.identifier, incoming),
                }
            if attempt.reconnect:
                previous = self.store._revision(attempt.identifier)
                if previous.name != attempt.previous_revision:
                    raise SetupError("This account changed during setup. Reload it before reconnecting.")
                old = json.loads((previous / "config.json").read_text())
                credentials = old["credentials"]
                old_references = json.loads((previous / "credential-refs.json").read_text())
            else:
                client = secrets.token_urlsafe(32)
                if "client" not in attempt.references:
                    attempt.references["client"] = self.credentials.create(attempt.revision, "client", client)
                    attempt.client_digest = hashlib.sha256(client.encode()).hexdigest()
                credentials = {attempt.client_digest: {"client_identity": "local-owner", "grants": []}}
            digest = next(iter(credentials))
            config = json.loads(render_pimcamp_profile(settings, attempt.identifier, revision,
                self.state_root / attempt.identifier / "state.sqlite3", self.himalaya, self.carillon, digest))
            if attempt.reconnect:
                config["credentials"] = credentials
            files["config.json"] = (json.dumps(config, indent=2) + "\n").encode()
            references = {key: {"entry": value.entry, "serial": value.serial, "backend": value.backend}
                          for key, value in attempt.references.items()}
            if attempt.reconnect:
                references["client"] = old_references["client"]
            files["credential-refs.json"] = (json.dumps(references) + "\n").encode()
            metadata = {"address": settings.email, "name": settings.account_name, "method": draft["method"],
                        "status": "connected", "note": "Incoming and outgoing sign-in checks passed.",
                        "incoming": settings.review()["incoming"], "outgoing": {
                            **settings.review()["outgoing"], "sameLogin": draft.get("outgoing", {}).get("sameLogin", True)}}
            self.store.publish(attempt.identifier, metadata, files, reconnect=attempt.reconnect,
                               expected_revision=attempt.previous_revision, revision_id=attempt.revision)
            attempt.saved = True
            self._cleanup(attempt)
            if self.runtime_config is not None:
                self.store.ensure_runtime_default(attempt.identifier, self.runtime_config)
            attempt.finalized = True
            return {"ok": True, "accountId": attempt.identifier, "message": f"Settings saved for {settings.email}."}

    def observation(self, identifier):
        self.store._revision(identifier)
        return {"state": "unavailable", "message": "Mail watching is configured but a new-mail event has not been verified. Reading and sending have separate connection checks."}

    def check_existing(self, identifier):
        with self.lock:
            revision = self.store._revision(identifier)
            results = [self.checker(self.himalaya, revision / "himalaya.toml", identifier, backend)
                       for backend in ("imap", "smtp")]
            record = next(row for row in self.store.list_accounts() if row["id"] == identifier)
            ok = all(result.get("ok") is True for result in results)
            return {**record, "status": "connected" if ok else "needs-reconnect",
                    "note": "Incoming and outgoing sign-in checks passed." if ok else "Could not connect or sign in. Check the account settings."}
