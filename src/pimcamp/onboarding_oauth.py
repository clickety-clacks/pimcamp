"""Google setup through Ortie 2.2's supported commands; no custom OAuth exchange."""

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess
import time
import tomllib
from urllib.parse import parse_qs, urlsplit


GMAIL_SCOPE = "https://mail.google.com/"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


class OAuthSetupError(Exception):
    pass


@dataclass(frozen=True)
class GoogleApplication:
    client_id: str
    ortie: str
    secret_command: tuple[str, ...] | None = field(default=None, repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.apps\.googleusercontent\.com", self.client_id):
            raise OAuthSetupError("Configure a valid Google OAuth client ID for this installation.")
        if not Path(self.ortie).is_absolute():
            raise OAuthSetupError("Ortie must have an absolute installation path.")
        if self.secret_command and (not Path(self.secret_command[0]).is_absolute() or
                                    not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in self.secret_command)):
            raise OAuthSetupError("The OAuth client secret must use an installation-owned credential command.")


def render_ortie_account(application: GoogleApplication, account: str, callback: str,
                         read_command: list[str], write_command: list[str]) -> bytes:
    quote = json.dumps
    uri = urlsplit(callback)
    if uri.scheme != "http" or uri.hostname != "127.0.0.1" or not uri.port or uri.query or uri.fragment or uri.username:
        raise OAuthSetupError("Configure a fixed local OAuth callback address.")
    lines = [f"[accounts.{quote(account)}]", "default = true", f"client-id = {quote(application.client_id)}",
             'grant = "authorization-code"', f"endpoints.authorization = {quote(GOOGLE_AUTH)}",
             f"endpoints.token = {quote(GOOGLE_TOKEN)}", f"endpoints.redirection = {quote(callback)}",
             f"scopes = [{quote(GMAIL_SCOPE)}]", 'pkce = "s256"', 'extras.access_type = "offline"',
             'extras.prompt = "consent select_account"', "auto-refresh = true",
             f"storage.read.command = {quote(read_command)}", f"storage.write.command = {quote(write_command)}"]
    if application.secret_command:
        lines.append(f"client-secret.command = {quote(list(application.secret_command))}")
    raw = "\n".join(lines) + "\n"
    tomllib.loads(raw)
    return raw.encode()


@dataclass
class GoogleGrant:
    authorization_url: str = field(repr=False)
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    callback: str
    created: float = field(default_factory=time.monotonic)

    @classmethod
    def from_ortie(cls, value: object, application: GoogleApplication, callback: str):
        if not isinstance(value, dict):
            raise OAuthSetupError("Ortie did not return an authorization request.")
        url, state, verifier = (value.get(key) for key in ("authorization_uri", "state", "pkce_code_verifier"))
        if not all(isinstance(item, str) for item in (url, state, verifier)):
            raise OAuthSetupError("Ortie returned an incomplete authorization request.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", state) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            raise OAuthSetupError("Ortie returned invalid authorization state.")
        uri = urlsplit(url)
        query = parse_qs(uri.query, strict_parsing=True)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        expected = {"client_id": application.client_id, "redirect_uri": callback, "response_type": "code",
                    "scope": GMAIL_SCOPE, "code_challenge_method": "S256", "code_challenge": challenge,
                    "state": state, "access_type": "offline", "prompt": "consent select_account"}
        if (f"{uri.scheme}://{uri.netloc}{uri.path}" != GOOGLE_AUTH or uri.fragment or
            any(query.get(key) != [wanted] for key, wanted in expected.items()) or
            {"code", "client_secret", "access_token", "refresh_token", "token", "code_verifier"}.intersection(query)):
            raise OAuthSetupError("Ortie returned an unexpected Google authorization request.")
        return cls(url, state, verifier, callback)

    def response_status(self, redirected_uri: str) -> str:
        if time.monotonic() - self.created > 900:
            return "expired"
        uri, expected = urlsplit(redirected_uri), urlsplit(self.callback)
        if any(char.isspace() for char in redirected_uri) or uri.fragment or (uri.scheme, uri.netloc, uri.path) != (expected.scheme, expected.netloc, expected.path):
            raise OAuthSetupError("The Google callback address does not match this setup.")
        values = parse_qs(uri.query, strict_parsing=True)
        state = values.get("state", [])
        if len(state) != 1 or not hmac.compare_digest(state[0], self.state):
            raise OAuthSetupError("The Google callback does not belong to this setup.")
        error = values.get("error", [])
        if error:
            if len(error) != 1:
                raise OAuthSetupError("Google returned an invalid authorization result.")
            if error[0] in ("admin_policy_enforced", "org_internal") or values.get("error_subtype") == ["admin_policy_enforced"]:
                return "denied-policy"
            if error[0] == "access_denied":
                return "cancelled"
            raise OAuthSetupError("Google did not authorize this account. Check the OAuth application settings.")
        if len(values.get("code", [])) != 1 or not values["code"][0]:
            raise OAuthSetupError("Google did not return an authorization code.")
        return "ready"

    def resume_input(self, redirected_uri: str) -> bytes:
        if self.response_status(redirected_uri) != "ready":
            raise OAuthSetupError("This authorization response cannot be exchanged.")
        return (f"auth resume --state={self.state} --pkce={self.verifier} --redirect-uri={self.callback} {redirected_uri}\n"
                "token inspect\nquit\n").encode()


class OrtieAuthorization:
    def __init__(self, application: GoogleApplication):
        self.application = application

    def _run(self, config: Path, account: str, arguments: list[str], payload=None):
        try:
            result = subprocess.run([self.application.ortie, "--log-level", "off", "--config", str(config),
                                     "--account", account, *arguments], input=payload,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45,
                                    check=False, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                                    **({"stdin": subprocess.DEVNULL} if payload is None else {}))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OAuthSetupError("The Google authorization helper could not complete this step. Retry or check the installation.") from error
        if result.returncode:
            raise OAuthSetupError("The Google authorization helper rejected this step. Check the application settings.")
        return result.stdout

    def begin(self, config: Path, account: str, callback: str) -> GoogleGrant:
        try:
            value = json.loads(self._run(config, account, ["--json", "auth", "get"]))
            return GoogleGrant.from_ortie(value, self.application, callback)
        except (ValueError, TypeError) as error:
            raise OAuthSetupError("Ortie did not return a valid Google authorization request.") from error

    def resume(self, config: Path, account: str, grant: GoogleGrant, redirected_uri: str) -> str:
        status = grant.response_status(redirected_uri)
        if status != "ready":
            return status
        # Ortie's supported REPL grammar travels over stdin. Authorization codes
        # and PKCE verifiers never become OS process arguments or shell history.
        raw = self._run(config, account, ["repl"], grant.resume_input(redirected_uri))
        text = raw.decode("utf-8", errors="replace")
        if "With refresh token: true" not in text or not any(
            line.startswith("With scope: ") and GMAIL_SCOPE in line.removeprefix("With scope: ").split()
            for line in text.splitlines()
        ):
            raise OAuthSetupError("Google did not provide renewable mail access. Check the application's scopes and consent settings, then retry.")
        return "authorized"
