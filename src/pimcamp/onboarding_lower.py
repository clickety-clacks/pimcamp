"""Setup uses Himalaya's own authentication checks, not another protocol client."""

import json
from pathlib import Path
import subprocess
import tomllib

from .onboarding import ImapSetup
from . import CAPABILITIES


def render_himalaya_account(setup: ImapSetup, account: str,
                           incoming_command: list[str], outgoing_command: list[str]) -> bytes:
    """Render a standalone account config containing credential references only.

    Matches Himalaya 2.1.0 / bbdfb09b's config.sample.toml. Caller supplies
    installation-owned commands, never executable paths supplied by the web UI.
    """
    def quote(value):
        return json.dumps(value, ensure_ascii=True)

    if not account or any(ord(c) < 32 for c in account):
        raise ValueError("Invalid internal account identifier")
    lines = [f"[accounts.{quote(account)}]", "default = true",
             f"email = {quote(setup.email)}", 'mailbox.alias.inbox = "INBOX"']
    for protocol, server, command in (
        ("imap", setup.incoming, incoming_command),
        ("smtp", setup.outgoing, outgoing_command),
    ):
        if not isinstance(command, list) or not command or not all(
            isinstance(arg, str) and arg and "\x00" not in arg for arg in command
        ) or not Path(command[0]).is_absolute():
            raise ValueError("Credential command must use an absolute executable")
        if server.security not in ("tls", "starttls"):
            raise ValueError("Unencrypted authentication is not supported")
        scheme = protocol + ("s" if server.security == "tls" else "")
        lines.append(f"{protocol}.server = {quote(f'{scheme}://{server.host}:{server.port}')}")
        if server.security == "starttls":
            lines.append(f"{protocol}.starttls = true")
        lines.extend([
            f"{protocol}.sasl.plain.username = {quote(server.username)}",
            f"{protocol}.sasl.plain.password.command = {quote(command)}",
        ])
    raw = "\n".join(lines) + "\n"
    tomllib.loads(raw)
    return raw.encode()


def render_carillon_account(setup: ImapSetup, account: str, incoming_command: list[str]) -> bytes:
    """Carillon has a strict schema: do not hand it a whole Himalaya config."""
    operations = render_himalaya_account(setup, account, incoming_command, incoming_command).decode()
    lines = [line for line in operations.splitlines()
             if line.startswith(("[accounts.", "default =", "imap."))]
    lines.append('imap.mailbox = "INBOX"')
    raw = "\n".join(lines) + "\n"
    tomllib.loads(raw)
    return raw.encode()


def render_pimcamp_profile(setup: ImapSetup, account: str, revision: Path,
                           state_path: Path, himalaya: str, carillon: str,
                           client_digest: str) -> bytes:
    """Both adapters target the same account and immutable configuration revision."""
    if len(client_digest) != 64 or any(c not in "0123456789abcdef" for c in client_digest):
        raise ValueError("Invalid client credential digest")
    if not all(Path(path).is_absolute() for path in (revision, state_path, himalaya, carillon)):
        raise ValueError("Installation paths must be absolute")
    value = {
        "credentials": {client_digest: {"client_identity": "local-owner", "grants": sorted(CAPABILITIES)}},
        "adapter_wait_seconds": 30,
        "state_path": str(state_path),
        "operations_adapter": {
            "kind": "himalaya", "executable": himalaya, "account": account,
            "config_paths": [str(revision / "himalaya.toml")], "inbox": "INBOX",
            "junk_mailbox": None, "from": {"name": None, "address": setup.email},
        },
        "observation_adapter": {
            "kind": "mirador", "executable": carillon, "account": account,
            "backend": "imap", "config_paths": [str(revision / "carillon.toml")],
        },
    }
    return (json.dumps(value, indent=2) + "\n").encode()


def render_google_accounts(email: str, account: str, token_command: list[str]) -> tuple[bytes, bytes]:
    """Give both lower tools the same renewable OAuth token command."""
    if (not token_command or not isinstance(token_command, list) or
            not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in token_command) or
            not Path(token_command[0]).is_absolute()):
        raise ValueError("Token command must use an absolute executable")
    if not account or any(ord(char) < 32 for char in account):
        raise ValueError("Invalid internal account identifier")
    quote = json.dumps
    common = [f"[accounts.{quote(account)}]", "default = true"]
    incoming = ['imap.server = "imaps://imap.gmail.com:993"',
                f"imap.sasl.xoauth2.username = {quote(email)}",
                f"imap.sasl.xoauth2.token.command = {quote(token_command)}"]
    operations = common + [f"email = {quote(email)}", 'mailbox.alias.inbox = "INBOX"',
                           f"gmail.auth.token.command = {quote(token_command)}"] + incoming + [
        'smtp.server = "smtps://smtp.gmail.com:465"',
        f"smtp.sasl.xoauth2.username = {quote(email)}",
        f"smtp.sasl.xoauth2.token.command = {quote(token_command)}"]
    observation = common + incoming + ['imap.mailbox = "INBOX"']
    outputs = tuple(("\n".join(lines) + "\n").encode() for lines in (operations, observation))
    for raw in outputs:
        tomllib.loads(raw.decode())
    return outputs


def google_account_identity(executable: str, config: Path, account: str) -> str:
    """Ask Gmail for the authorized identity without listing or reading messages."""
    if not Path(executable).is_absolute() or not config.is_absolute():
        raise ValueError("Installation paths must be absolute")
    try:
        result = subprocess.run(
            [executable, "--json", "--log-level", "off", "--config", str(config),
             "--account", account, "gmail", "profile", "get"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        value = json.loads(result.stdout) if result.returncode == 0 else None
        email = value.get("email") if isinstance(value, dict) else None
        if (not isinstance(email, str) or email.count("@") != 1 or len(email) > 254 or
                any(char.isspace() or ord(char) < 32 for char in email) or
                not all(email.split("@"))):
            raise ValueError("Invalid provider identity")
        return email
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise ValueError("Could not verify the Google account. Retry authorization.") from error


def check_himalaya_account(executable: str, config: Path, account: str,
                          backend: str, timeout: float = 30) -> dict:
    """Authenticate one backend without listing, reading, or sending mail."""
    if backend not in ("imap", "smtp"):
        raise ValueError("Unsupported setup backend")
    if not Path(executable).is_absolute() or not config.is_absolute():
        raise ValueError("Installation paths must be absolute")
    direction = "incoming" if backend == "imap" else "outgoing"
    try:
        result = subprocess.run(
            [executable, "--log-level", "off", "--config", str(config),
             "--account", account, "--backend", backend, "account", "check"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "timeout",
                "message": f"The {direction} mail check timed out. Check the server and connection settings."}
    except OSError:
        return {"ok": False, "code": "unavailable",
                "message": "The mail connection tool could not start. Check the installation."}
    if result.returncode != 0:
        return {"ok": False, "code": "connection_failed",
                "message": f"Could not connect or sign in to {direction} mail. Check the server, security, username and password."}
    return {"ok": True, "message": f"Signed in to {direction} mail. No mail was sent."}
