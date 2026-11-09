"""Provider-independent setup input validation; no mail or credential writes."""

from dataclasses import dataclass, field
import re


class SetupValidationError(ValueError):
    """Safe field errors suitable for presentation to the account owner."""

    def __init__(self, fields: dict[str, str]):
        super().__init__("Check the highlighted account settings.")
        self.fields = fields


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    security: str
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class ImapSetup:
    email: str
    account_name: str
    incoming: ServerSettings
    outgoing: ServerSettings

    def review(self) -> dict:
        """Explicit allowlist: secrets never enter the review response."""
        def public(server):
            return {"host": server.host, "port": server.port,
                    "security": server.security, "username": server.username}
        return {"email": self.email, "account_name": self.account_name,
                "incoming": public(self.incoming), "outgoing": public(self.outgoing)}


def validate_imap_setup(value: object) -> ImapSetup:
    errors: dict[str, str] = {}
    if not isinstance(value, dict):
        raise SetupValidationError({"form": "Enter your account settings."})

    def text(source, key):
        raw = source.get(key, "")
        return raw.strip() if isinstance(raw, str) else ""

    email = text(value, "email")
    if not re.fullmatch(r"[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+\.[^\s@\x00-\x1f\x7f]+", email):
        errors["email"] = "Enter an email address, such as alex@example.com."
    name = text(value, "account_name") or email
    if len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        errors["account_name"] = "Use an account name of at most 80 characters, without control characters."
    same_login = value.get("same_login", True)
    if not isinstance(same_login, bool):
        errors["same_login"] = "Choose whether outgoing mail uses the same login."

    def server(key, default_port, login=None):
        raw = value.get(key, {})
        if not isinstance(raw, dict):
            raw = {}
        host = text(raw, "host")
        labels = host.rstrip(".").split(".")
        if len(host) > 253 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            errors[key + ".host"] = "Enter a server hostname without a URL prefix or port."
        security = raw.get("security", "tls")
        if security not in ("tls", "starttls"):
            errors[key + ".security"] = "Choose TLS or STARTTLS. Unencrypted login is not supported."
        if security == "starttls":
            default_port = 143 if key == "incoming" else 587
        port = raw.get("port", default_port)
        if isinstance(port, str) and port.isascii() and port.isdecimal():
            port = int(port) if len(port) <= 5 else 0
        if type(port) is not int or not 1 <= port <= 65535:
            errors[key + ".port"] = "Enter a port from 1 to 65535."
            port = default_port
        username = login.username if login else text(raw, "username") or email
        password = login.password if login else raw.get("password", "")
        if not username or any(ord(char) < 32 or ord(char) == 127 for char in username):
            errors[key + ".username"] = "Enter the username supplied by your email provider."
        if not isinstance(password, str) or not password or any(c in password for c in "\x00\r\n"):
            errors[key + ".password"] = "Enter your password or provider-issued app password."
            password = ""
        return ServerSettings(host, port, security, username, password)

    incoming = server("incoming", 993)
    outgoing = server("outgoing", 465, incoming if same_login is True else None)
    if errors:
        raise SetupValidationError(errors)
    return ImapSetup(email, name, incoming, outgoing)
