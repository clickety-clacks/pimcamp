"""Private WSGI routes for the authenticated mail viewer and browser replies."""

from dataclasses import dataclass
from http import HTTPStatus
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Iterable, Protocol
from http.cookies import SimpleCookie
from urllib.parse import unquote
import uuid

from . import V2_CONTRACT_VERSION
from .config import Config
from .errors import PimcampError, invalid
from .jsonio import dumps_line, loads_one
from .mail_service import AccountMailService
from .schema import (
    validate_browser_prepare,
    validate_browser_session,
    validate_browser_submit,
)
from .state import State


class IdentityAssertionVerifier(Protocol):
    def authenticate(self, headers: dict[str, str]) -> str | None: ...


@dataclass(frozen=True)
class SessionPolicy:
    idle_seconds: float
    absolute_seconds: float

    def __post_init__(self) -> None:
        if self.idle_seconds <= 0 or self.absolute_seconds <= 0:
            raise ValueError("mail session lifetimes must be positive")


@dataclass
class _Session:
    principal_id: str
    csrf_token: str
    created_at: float
    last_seen: float


class MailSessionStore:
    def __init__(self, policy: SessionPolicy, clock: Callable[[], float] = time.time):
        self.policy = policy
        self.clock = clock
        self._sessions: dict[str, _Session] = {}

    def create(self, principal_id: str) -> tuple[str, _Session]:
        now = self.clock()
        token = secrets.token_urlsafe(32)
        session = _Session(principal_id, secrets.token_urlsafe(24), now, now)
        self._sessions[token] = session
        return token, session

    def get(self, token: str) -> _Session | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        now = self.clock()
        if (
            now - session.last_seen >= self.policy.idle_seconds
            or now - session.created_at >= self.policy.absolute_seconds
        ):
            self._sessions.pop(token, None)
            return None
        session.last_seen = now
        return session


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class MailHTTPApplication:
    def __init__(
        self,
        config: Config,
        identity_verifier: IdentityAssertionVerifier,
        account_authorizer: Callable[[str], Iterable[str]],
        session_policy: SessionPolicy,
        *,
        page_path: Path | None = None,
        status_reconciler: Callable[[str, str, str], str | None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.identity_verifier = identity_verifier
        self.account_authorizer = account_authorizer
        self.sessions = MailSessionStore(session_policy, clock)
        self.page_path = page_path or Path(__file__).resolve().parents[2] / "ui" / "mail" / "index.html"
        self.status_reconciler = status_reconciler

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | str = b"",
    ) -> HTTPResponse:
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        try:
            if method == "POST" and path == "/mail/api/session":
                return self._create_session(headers, body)
            if method == "GET" and path.startswith("/mail/m/"):
                return self._page(path, headers)
            if method == "GET" and path.startswith("/mail/api/v2/messages/"):
                locator = _one_path_segment(path, "/mail/api/v2/messages/")
                principal, accounts = self._browser_principal(headers)
                state = State.open_read_only(self.config.state_path)
                try:
                    result = self._service(state, principal, accounts).get_message(locator)
                finally:
                    state.close()
                return self._json(200, result)
            if method == "POST" and path == "/mail/api/v2/replies/prepare":
                principal, accounts, session = self._mutation_principal(headers)
                self._require_origin(headers)
                self._require_csrf(headers, session)
                request = validate_browser_prepare(_json_body(body))
                state = State(self.config.state_path)
                try:
                    result = self._service(state, principal, accounts).prepare_reply(
                        request["locator"], request["body_text"]
                    )
                finally:
                    state.close()
                return self._json(200, result)
            if method == "POST" and path.startswith("/mail/api/v2/replies/") and path.endswith("/submit"):
                operation_id = _one_path_segment(
                    path, "/mail/api/v2/replies/", suffix="/submit"
                )
                try:
                    operation_id = str(uuid.UUID(operation_id))
                except ValueError as exc:
                    raise invalid("The reply operation ID is invalid.") from exc
                principal, accounts, session = self._mutation_principal(headers)
                self._require_origin(headers)
                self._require_csrf(headers, session)
                request = validate_browser_submit(_json_body(body))
                state = State(self.config.state_path)
                try:
                    result = self._service(state, principal, accounts).submit_reply(
                        operation_id,
                        request,
                        reauthorize=lambda account_id: self._reauthorize(
                            principal, account_id
                        ),
                    )
                finally:
                    state.close()
                return self._json(200, result)
            if method == "GET" and path.startswith("/mail/api/v2/accounts/") and "/send-status/" in path:
                account_id, operation_id = _account_and_operation(path)
                try:
                    operation_id = str(uuid.UUID(operation_id))
                except ValueError as exc:
                    raise invalid("The reply operation ID is invalid.") from exc
                principal, accounts = self._browser_principal(headers)
                state = State.open_read_only(self.config.state_path)
                try:
                    result = self._service(state, principal, accounts).send_status(
                        account_id, operation_id
                    )
                finally:
                    state.close()
                return self._json(200, result)
            return self._json(404, {"contract_version": V2_CONTRACT_VERSION, "code": "not_found", "message": "The route is unavailable."})
        except PimcampError as exc:
            return self._json(_status_for(exc), exc.public_value(V2_CONTRACT_VERSION))
        except Exception:
            error = PimcampError("backend_unavailable", "The mail service could not complete the request.")
            return self._json(503, error.public_value(V2_CONTRACT_VERSION))

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        length = environ.get("CONTENT_LENGTH", "0")
        try:
            body = environ["wsgi.input"].read(max(0, int(length)))
        except (ValueError, OSError):
            body = b""
        headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_") and isinstance(value, str)
        }
        for key in ("CONTENT_TYPE",):
            if isinstance(environ.get(key), str):
                headers["content-type"] = environ[key]
        response = self.handle(
            str(environ.get("REQUEST_METHOD", "GET")).upper(),
            str(environ.get("PATH_INFO", "/")),
            headers,
            body,
        )
        try:
            reason = HTTPStatus(response.status).phrase
        except ValueError:
            reason = "Unknown"
        start_response(
            f"{response.status} {reason}",
            [(key, value) for key, value in response.headers],
        )
        return [response.body]

    def _create_session(self, headers: dict[str, str], body: bytes | str) -> HTTPResponse:
        self._require_origin(headers)
        validate_browser_session(_json_body(body))
        principal = self._verified_principal(headers)
        accounts = self._authorized_accounts(principal)
        if not accounts:
            raise PimcampError("permission_denied", "No mail accounts are authorized.")
        token, session = self.sessions.create(principal)
        result = {
            "status": "authenticated",
            "csrf_token": session.csrf_token,
            "accounts": [
                {"account_id": account_id, "label": self.config.mail_accounts[account_id].label}
                for account_id in sorted(accounts)
                if account_id in self.config.mail_accounts
            ],
        }
        response = self._json(200, result)
        cookie = (
            f"__Secure-pimcamp_mail_session={token}; Path=/mail; HttpOnly; Secure; "
            "SameSite=Strict"
        )
        return HTTPResponse(
            response.status,
            (*response.headers, ("Set-Cookie", cookie)),
            response.body,
        )

    def _page(self, path: str, headers: dict[str, str]) -> HTTPResponse:
        locator = _one_path_segment(path, "/mail/m/")
        principal = self._verified_principal(headers)
        accounts = self._authorized_accounts(principal)
        state = State.open_read_only(self.config.state_path)
        try:
            service = AccountMailService(self.config, state, principal, accounts)
            account_id = service.locator_account(locator)
            service.account_info(account_id)
        finally:
            state.close()
        try:
            page = self.page_path.read_bytes()
        except OSError as exc:
            raise PimcampError("backend_unavailable", "The mail page is unavailable.") from exc
        return HTTPResponse(
            200,
            self._base_headers("text/html; charset=utf-8")
            + (("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),),
            page,
        )

    def _browser_principal(self, headers: dict[str, str]) -> tuple[str, frozenset[str]]:
        principal = self._verified_principal(headers)
        token = _cookie_value(headers.get("cookie", ""))
        session = self.sessions.get(token) if token else None
        if session is None or session.principal_id != principal:
            raise PimcampError("permission_denied", "An authenticated mail session is required.")
        return principal, self._authorized_accounts(principal)

    def _mutation_principal(
        self, headers: dict[str, str]
    ) -> tuple[str, frozenset[str], _Session]:
        principal, accounts = self._browser_principal(headers)
        token = _cookie_value(headers.get("cookie", ""))
        session = self.sessions.get(token) if token else None
        if session is None:
            raise PimcampError("permission_denied", "An authenticated mail session is required.")
        return principal, accounts, session

    def _verified_principal(self, headers: dict[str, str]) -> str:
        try:
            principal = self.identity_verifier.authenticate(headers)
        except Exception as exc:
            raise PimcampError("permission_denied", "The authenticated identity is unavailable.") from exc
        if not isinstance(principal, str) or not principal:
            raise PimcampError("permission_denied", "An authenticated operator identity is required.")
        return principal

    def _authorized_accounts(self, principal: str) -> frozenset[str]:
        try:
            values = frozenset(self.account_authorizer(principal))
        except Exception as exc:
            raise PimcampError("backend_unavailable", "Mail account authorization is unavailable.") from exc
        return frozenset(
            account_id
            for account_id in values
            if isinstance(account_id, str) and account_id in self.config.mail_accounts
        )

    def _reauthorize(self, principal: str, account_id: str) -> None:
        if account_id not in self._authorized_accounts(principal):
            raise PimcampError("permission_denied", "The account is not permitted.")

    def _service(
        self, state: State, principal: str, accounts: Iterable[str]
    ) -> AccountMailService:
        return AccountMailService(
            self.config,
            state,
            principal,
            frozenset(accounts),
            status_reconciler=self.status_reconciler,
        )

    def _require_origin(self, headers: dict[str, str]) -> None:
        if self.config.mail_origin is None or headers.get("origin") != self.config.mail_origin:
            raise PimcampError("permission_denied", "The request origin is not permitted.")

    @staticmethod
    def _require_csrf(headers: dict[str, str], session: _Session) -> None:
        supplied = headers.get("x-pimcamp-csrf", "")
        if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
            raise PimcampError("permission_denied", "The request token is invalid.")

    def _json(self, status: int, value: dict[str, Any]) -> HTTPResponse:
        return HTTPResponse(
            status,
            self._base_headers("application/json; charset=utf-8"),
            dumps_line(value).encode("utf-8"),
        )

    @staticmethod
    def _base_headers(content_type: str) -> tuple[tuple[str, str], ...]:
        return (
            ("Content-Type", content_type),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        )


def _json_body(body: bytes | str) -> Any:
    if isinstance(body, bytes):
        return loads_one(body)
    return loads_one(body)


def _cookie_value(cookie_header: str) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get("__Secure-pimcamp_mail_session")
    return None if morsel is None else morsel.value


def _one_path_segment(path: str, prefix: str, suffix: str = "") -> str:
    if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
        raise invalid("The route path is invalid.")
    value = path[len(prefix) : len(path) - len(suffix) if suffix else None]
    value = unquote(value)
    if not value or "/" in value or "\\" in value:
        raise invalid("The route path is invalid.")
    return value


def _account_and_operation(path: str) -> tuple[str, str]:
    prefix = "/mail/api/v2/accounts/"
    marker = "/send-status/"
    if not path.startswith(prefix) or marker not in path:
        raise invalid("The route path is invalid.")
    account_raw, operation_raw = path[len(prefix) :].split(marker, 1)
    account_id = unquote(account_raw)
    operation_id = unquote(operation_raw)
    if not account_id or "/" in account_id or not operation_id or "/" in operation_id:
        raise invalid("The route path is invalid.")
    return account_id, operation_id


def _status_for(error: PimcampError) -> int:
    return {
        "invalid_request": 400,
        "permission_denied": 403,
        "not_found": 404,
        "conflict": 409,
        "outcome_unknown": 409,
        "unsupported": 501,
        "backend_unavailable": 503,
    }.get(error.code, 500)
