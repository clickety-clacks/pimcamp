"""Local account-setup HTTP boundary with a one-time, owner-file launch handoff."""

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs

from .onboarding import SetupValidationError
from .onboarding_credentials import CredentialError
from .onboarding_service import SetupError
from .onboarding_oauth import OAuthSetupError
from .onboarding_store import AccountStoreError


class SetupHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service, assets: Path, launch_directory: Path, port=33281):
        self.service = service
        self.assets = assets
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.bootstrap = secrets.token_urlsafe(32)
        self.bootstrap_deadline = time.monotonic() + 300
        self.bootstrap_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), SetupHandler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"
        launch_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = launch_directory.lstat()
        if not launch_directory.is_dir() or launch_directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            self.server_close()
            raise SetupError("The local setup launch directory must be private.")
        self.launch_file = launch_directory / f"open-setup-{os.getpid()}-{secrets.token_hex(8)}.html"
        document = (
            '<!doctype html><meta charset="utf-8"><title>Opening Pimcamp setup</title>'
            '<meta name="referrer" content="no-referrer">'
            f'<form method="post" action="{self.origin}/bootstrap">'
            f'<input type="hidden" name="token" value="{self.bootstrap}">'
            '<button>Open Pimcamp account setup</button></form>'
            '<script>document.forms[0].submit()</script>'
        )
        descriptor = os.open(self.launch_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(document)

    def server_close(self):
        super().server_close()
        launch_file = getattr(self, "launch_file", None)
        if launch_file is not None:
            launch_file.unlink(missing_ok=True)

    def service_actions(self):
        with self.service.lock:
            try:
                self.service.expire()
            except SetupError:
                # Keep failed cleanup visible: the next setup request retries
                # expiration and receives its actionable error, not a success.
                pass


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "PimcampSetup"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass  # Never log URLs, request bodies, cookies or raw provider errors.

    def reply(self, status, payload, content_type="application/json", extra=()):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def valid_host(self):
        return self.headers.get_all("Host") == [self.server.authority]

    def authenticated(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookies.get("pimcamp_setup")
            return value is not None and hmac.compare_digest(value.value, self.server.session)
        except Exception:
            return False

    def read_body(self):
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise SetupError("Invalid request body length.")
        if len(lengths[0]) > 6 or not 0 < int(lengths[0]) <= 65536:
            raise SetupError("Account settings exceed the request size limit.")
        data = self.rfile.read(int(lengths[0]))
        if len(data) != int(lengths[0]):
            raise SetupError("The account settings request was incomplete.")
        return data

    def do_GET(self):
        if self.valid_host() and self.path.startswith("/oauth/google/callback?"):
            try:
                self.server.service.complete_oauth(self.server.origin + self.path)
                self.reply(303, b"", extra=(("Location", "/oauth/complete"),))
            except Exception:
                # Never reflect authorization codes or provider diagnostics.
                self.reply(400, b"Google authorization could not complete. Return to account setup and retry.",
                           "text/plain; charset=utf-8")
            return
        if self.valid_host() and self.path == "/oauth/complete":
            self.reply(200, b"Return to the Pimcamp setup window to continue. You can close this Google window.",
                       "text/plain; charset=utf-8")
            return
        if not self.valid_host() or not self.authenticated():
            self.reply(403, {"error": {"message": "Open account setup through the local Pimcamp launcher."}})
            return
        if self.path == "/api/session":
            self.reply(200, {"csrf": self.server.csrf})
            return
        names = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/live-service.js": ("live-service.js", "text/javascript; charset=utf-8")}
        if self.path not in names:
            self.reply(404, {"error": {"message": "Page not found."}})
            return
        filename, mime = names[self.path]
        content = (self.server.assets / filename).read_bytes()
        if filename == "index.html":
            content = content.replace(b'<html lang="en"', b'<html data-mode="live" lang="en"')
            content = content.replace(b'<script src="app.js" defer></script>',
                                      b'<script src="live-service.js" defer></script><script src="app.js" defer></script>')
        self.reply(200, content, mime)

    def do_POST(self):
        if not self.valid_host():
            self.reply(403, {"error": {"message": "Invalid setup origin."}})
            return
        try:
            if self.path == "/bootstrap":
                if self.headers.get("Origin") not in (None, "null", self.server.origin):
                    raise SetupError("Open setup from its local launch file.")
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise SetupError("Invalid setup handoff.")
                values = parse_qs(self.read_body().decode(), strict_parsing=True)
                token = values.get("token", [""])
                with self.server.bootstrap_lock:
                    if len(token) != 1 or self.server.bootstrap is None or time.monotonic() > self.server.bootstrap_deadline or not hmac.compare_digest(token[0], self.server.bootstrap):
                        raise SetupError("This setup handoff expired or was already used. Reopen setup from Pimcamp.")
                    self.server.bootstrap = None
                    self.server.launch_file.unlink(missing_ok=True)
                self.reply(303, b"", extra=(("Location", "/"), ("Set-Cookie", f"pimcamp_setup={self.server.session}; HttpOnly; SameSite=Lax; Path=/")))
                return
            if self.path != "/api/action":
                self.reply(404, {"error": {"message": "Action not found."}})
                return
            if not self.authenticated() or self.headers.get_all("Origin") != [self.server.origin] or not hmac.compare_digest(self.headers.get("X-Pimcamp-CSRF", ""), self.server.csrf):
                self.reply(403, {"error": {"message": "The setup session is not authorized."}})
                return
            if self.headers.get_content_type() != "application/json":
                raise SetupError("Account settings must be sent as JSON.")
            request = json.loads(self.read_body())
            if not isinstance(request, dict) or set(request) - {"action", "setupId", "draft", "accountId", "credentialsJson"}:
                raise SetupError("Invalid setup action.")
            action, service = request.get("action"), self.server.service
            if action == "listAccounts":
                result = service.list_accounts()
            elif action == "googleApplicationStatus":
                result = service.google_application_status()
            elif action == "configureGoogleApplication":
                result = service.configure_google_application(request.get("credentialsJson"))
            elif action == "beginSetup":
                result = service.begin(request.get("accountId"))
            elif action == "cancelSetup":
                result = service.cancel(request.get("setupId"))
            elif action == "beginOAuth":
                result = service.begin_oauth(request.get("setupId"))
            elif action == "oauthStatus":
                result = service.oauth_result(request.get("setupId"))
            elif action == "cancelOAuth":
                result = service.cancel_oauth(request.get("setupId"))
            elif action in ("checkIncoming", "checkOutgoing"):
                result = service.check(request.get("setupId"), request.get("draft"), "imap" if action == "checkIncoming" else "smtp")
            elif action == "commitAccount":
                result = service.commit(request.get("setupId"), request.get("draft"))
            elif action == "saveStatus":
                result = service.save_status(request.get("setupId"), request.get("draft"))
            elif action == "observationReadiness":
                result = service.observation(request.get("accountId"))
            elif action == "checkAccount":
                result = service.check_existing(request.get("accountId"))
            else:
                raise SetupError("This setup action is not available.")
            self.reply(200, result)
        except SetupValidationError as error:
            self.reply(400, {"error": {"message": str(error), "fields": error.fields}})
        except (SetupError, AccountStoreError, CredentialError, OAuthSetupError) as error:
            detail = {"message": str(error)}
            if isinstance(error, SetupError) and error.code:
                detail["code"] = error.code
            elif isinstance(error, CredentialError):
                detail["code"] = "credential_storage"
            self.reply(400, {"error": detail})
        except (ValueError, TypeError):
            self.reply(400, {"error": {"message": "Check the account settings and try again."}})
        except Exception:
            self.reply(500, {"error": {"message": "Setup could not complete this step. Check the installation and retry."}})
