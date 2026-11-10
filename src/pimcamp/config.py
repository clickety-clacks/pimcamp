"""Private owner-readable deployment configuration."""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

from . import ALL_CAPABILITIES
from .errors import unavailable
from .jsonio import canonical_json, loads_one


@dataclass(frozen=True)
class Client:
    identity: str
    grants: frozenset[str]
    account_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AdapterConfig:
    kind: str
    command: tuple[str, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class HimalayaConfig:
    kind: str
    executable: str
    account: str
    config_paths: tuple[str, ...]
    inbox: str
    junk_mailbox: str | None
    sender: dict[str, str | None]
    raw: dict[str, Any]
    backend: str | None = None


@dataclass(frozen=True)
class MiradorConfig:
    kind: str
    executable: str
    account: str
    backend: str
    config_paths: tuple[str, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class MailAccountConfig:
    account_id: str
    label: str
    operations: AdapterConfig | HimalayaConfig
    operations_fingerprint: str
    primary_from: dict[str, str | None] | None
    selectable_from: tuple[dict[str, Any], ...]
    alias_discovery_state: str
    identity_capability: str
    address: str | None = None


@dataclass(frozen=True)
class Config:
    credentials: dict[str, Client]
    adapter_wait_seconds: float
    state_path: Path
    operations: AdapterConfig | HimalayaConfig
    observation: AdapterConfig | MiradorConfig
    operations_fingerprint: str
    mail_accounts: dict[str, MailAccountConfig]
    mail_origin: str | None
    mail_principals: dict[str, frozenset[str]] = field(default_factory=dict)

    def authenticate(self, credential: str) -> Client | None:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        return self.credentials.get(digest)


def default_path() -> Path:
    configured = os.environ.get("PIMCAMP_CONFIG")
    if configured:
        return Path(configured)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "pimcamp" / "config.json"


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise unavailable("Pimcamp deployment configuration is unavailable.") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o077:
        raise unavailable("Pimcamp deployment configuration is not owner-only.")
    try:
        raw = loads_one(path.read_bytes())
    except OSError as exc:
        raise unavailable("Pimcamp deployment configuration is unavailable.") from exc
    except Exception as exc:
        raise unavailable("Pimcamp deployment configuration is invalid.") from exc
    if not isinstance(raw, dict) or not {
        "credentials",
        "adapter_wait_seconds",
        "state_path",
        "operations_adapter",
        "observation_adapter",
    }.issubset(raw) or not set(raw).issubset(
        {
            "credentials",
            "adapter_wait_seconds",
            "state_path",
            "operations_adapter",
            "observation_adapter",
            "mail_accounts",
            "mail_origin",
            "mail_principals",
        }
    ):
        raise unavailable("Pimcamp deployment configuration is invalid.")

    mail_accounts = _mail_accounts(raw.get("mail_accounts", {}))
    credentials = _credentials(raw["credentials"], set(mail_accounts))
    wait = raw["adapter_wait_seconds"]
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait <= 0:
        raise unavailable("Pimcamp adapter wait bound is invalid.")
    state_path = raw["state_path"]
    if not isinstance(state_path, str) or not state_path:
        raise unavailable("Pimcamp state path is invalid.")
    operations = _adapter(raw["operations_adapter"], "operations")
    observation = _adapter(raw["observation_adapter"], "observation")
    fingerprint = hashlib.sha256(canonical_json(operations.raw)).hexdigest()
    mail_origin = _mail_origin(raw.get("mail_origin"))
    return Config(
        credentials=credentials,
        adapter_wait_seconds=float(wait),
        state_path=Path(state_path),
        operations=operations,
        observation=observation,
        operations_fingerprint=fingerprint,
        mail_accounts=mail_accounts,
        mail_origin=mail_origin,
        mail_principals=_mail_principals(raw.get("mail_principals", {}), set(mail_accounts)),
    )


def _credentials(value: Any, account_ids: set[str]) -> dict[str, Client]:
    if not isinstance(value, dict):
        raise unavailable("Pimcamp credential configuration is invalid.")
    clients: dict[str, Client] = {}
    for digest, item in value.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(item, dict)
            or not {"client_identity", "grants"}.issubset(item)
            or not set(item).issubset({"client_identity", "grants", "account_ids"})
            or not isinstance(item["client_identity"], str)
            or not item["client_identity"]
            or not isinstance(item["grants"], list)
            or any(not isinstance(grant, str) for grant in item["grants"])
        ):
            raise unavailable("Pimcamp credential configuration is invalid.")
        grants = frozenset(item["grants"])
        if not grants.issubset(ALL_CAPABILITIES) or len(grants) != len(item["grants"]):
            raise unavailable("Pimcamp credential grants are invalid.")
        raw_accounts = item.get("account_ids", [])
        if (
            not isinstance(raw_accounts, list)
            or any(not isinstance(account_id, str) or not account_id for account_id in raw_accounts)
            or len(raw_accounts) != len(set(raw_accounts))
            or not set(raw_accounts).issubset(account_ids)
        ):
            raise unavailable("Pimcamp credential account access is invalid.")
        clients[digest] = Client(item["client_identity"], grants, frozenset(raw_accounts))
    return clients


def _mail_accounts(value: Any) -> dict[str, MailAccountConfig]:
    if not isinstance(value, dict):
        raise unavailable("Pimcamp mail-account configuration is invalid.")
    accounts: dict[str, MailAccountConfig] = {}
    for account_id, item in value.items():
        if (
            not isinstance(account_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", account_id) is None
            or not isinstance(item, dict)
            or not {
                "label",
                "operations_adapter",
                "primary_from",
                "selectable_from",
                "alias_discovery_state",
                "identity_capability",
            }.issubset(item)
            or not set(item).issubset({
                "label", "operations_adapter", "primary_from", "selectable_from",
                "alias_discovery_state", "identity_capability", "address",
            })
        ):
            raise unavailable("Pimcamp mail-account configuration is invalid.")
        label = item["label"]
        address = item.get("address")
        if address is not None and (not isinstance(address, str) or not _sendable_address(address)):
            raise unavailable("Pimcamp account address is invalid.")
        if not isinstance(label, str) or not label:
            raise unavailable("Pimcamp mail-account label is invalid.")
        operations = _adapter(item["operations_adapter"], "operations", mail_account=True)
        primary_from = _mail_address(item["primary_from"], optional=True)
        selectable = item["selectable_from"]
        if not isinstance(selectable, list):
            raise unavailable("Pimcamp selectable sender configuration is invalid.")
        selectable_from = tuple(_selectable_sender(sender) for sender in selectable)
        alias_state = item["alias_discovery_state"]
        if alias_state not in {
            "supported-complete",
            "supported-partial",
            "unknown",
            "unsupported",
        }:
            raise unavailable("Pimcamp sender-discovery state is invalid.")
        identity_capability = item["identity_capability"]
        if identity_capability not in {"native", "mapped", "unknown", "unsupported"}:
            raise unavailable("Pimcamp identity capability is invalid.")
        fingerprint = hashlib.sha256(canonical_json(operations.raw)).hexdigest()
        accounts[account_id] = MailAccountConfig(
            account_id=account_id,
            label=label,
            operations=operations,
            operations_fingerprint=fingerprint,
            primary_from=primary_from,
            selectable_from=selectable_from,
            alias_discovery_state=alias_state,
            identity_capability=identity_capability,
            address=address,
        )
    return accounts


def _mail_principals(value: Any, account_ids: set[str]) -> dict[str, frozenset[str]]:
    # The approved deployment has one operator. An absent mapping grants nothing.
    if not isinstance(value, dict) or len(value) > 1:
        raise unavailable("Pimcamp mail principal mapping is invalid.")
    result = {}
    for principal, accounts in value.items():
        if (
            not isinstance(principal, str) or not principal or principal.strip() != principal
            or not isinstance(accounts, list)
            or any(not isinstance(account, str) for account in accounts)
            or len(set(accounts)) != len(accounts)
            or not set(accounts).issubset(account_ids)
        ):
            raise unavailable("Pimcamp mail principal mapping is invalid.")
        result[principal] = frozenset(accounts)
    return result


def _mail_address(value: Any, *, optional: bool = False) -> dict[str, str | None] | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "address"}
        or (value["name"] is not None and not isinstance(value["name"], str))
        or (value["name"] is not None and ("\r" in value["name"] or "\n" in value["name"]))
        or not isinstance(value["address"], str)
        or not _sendable_address(value["address"])
    ):
        raise unavailable("Pimcamp account sender is invalid.")
    return {"name": value["name"], "address": value["address"]}


def _selectable_sender(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"from", "provenance", "validity"}:
        raise unavailable("Pimcamp selectable sender configuration is invalid.")
    sender = _mail_address(value["from"])
    provenance = value["provenance"]
    validity = value["validity"]
    if (
        not isinstance(provenance, str)
        or not provenance
        or validity not in {"valid", "unknown", "invalid"}
    ):
        raise unavailable("Pimcamp selectable sender authority is invalid.")
    return {"from": sender, "provenance": provenance, "validity": validity}


def _mail_origin(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise unavailable("Pimcamp mail origin is invalid.")
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise unavailable("Pimcamp mail origin is invalid.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise unavailable("Pimcamp mail origin must be an HTTPS origin.")
    return f"https://{parsed.netloc}"


def _adapter(
    value: Any, expected: str, *, mail_account: bool = False
) -> AdapterConfig | HimalayaConfig | MiradorConfig:
    if expected == "operations" and isinstance(value, dict) and value.get("kind") == "himalaya":
        return _himalaya(value, mail_account=mail_account)
    if expected == "observation" and isinstance(value, dict) and value.get("kind") == "mirador":
        return _mirador(value)
    if not isinstance(value, dict) or value.get("kind") != "command" or set(value) != {
        "kind",
        "command",
    }:
        raise unavailable(f"Pimcamp {expected} adapter configuration is invalid.")
    command = value["command"]
    if not isinstance(command, list) or not command or any(
        not isinstance(part, str) or not part for part in command
    ):
        raise unavailable(f"Pimcamp {expected} adapter command is invalid.")
    return AdapterConfig(kind="command", command=tuple(command), raw=value)


def _mirador(value: dict[str, Any]) -> MiradorConfig:
    if set(value) != {
        "kind",
        "executable",
        "account",
        "backend",
        "config_paths",
    }:
        raise unavailable("Pimcamp Mirador adapter configuration is invalid.")
    executable = value["executable"]
    account = value["account"]
    backend = value["backend"]
    config_paths = value["config_paths"]
    if any(not isinstance(item, str) or not item for item in (executable, account)):
        raise unavailable("Pimcamp Mirador adapter configuration is invalid.")
    if backend not in {"imap", "jmap", "maildir"}:
        raise unavailable("Pimcamp Mirador mail backend is invalid.")
    if not isinstance(config_paths, list) or not config_paths or any(
        not isinstance(path, str) or not path or ":" in path
        for path in config_paths
    ):
        raise unavailable("Pimcamp Mirador config paths are invalid.")
    return MiradorConfig(
        kind="mirador",
        executable=executable,
        account=account,
        backend=backend,
        config_paths=tuple(config_paths),
        raw=value,
    )


def _himalaya(value: dict[str, Any], *, mail_account: bool = False) -> HimalayaConfig:
    required = {
        "kind",
        "executable",
        "account",
        "config_paths",
        "inbox",
        "junk_mailbox",
        "from",
    }
    allowed = required | ({"backend"} if mail_account else set())
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise unavailable("Pimcamp Himalaya adapter configuration is invalid.")
    # Native paths require an explicit selector; Auto must not change identity.
    # Selecting it does not establish account identity or enable durable links.
    backend = value.get("backend")
    if "backend" in value and backend not in {"gmail", "dovecot"}:
        raise unavailable("Pimcamp native mail backend is unsupported.")
    executable = value["executable"]
    account = value["account"]
    config_paths = value["config_paths"]
    inbox = value["inbox"]
    junk = value["junk_mailbox"]
    sender = value["from"]
    if any(
        not isinstance(item, str) or not item
        for item in (executable, account, inbox)
    ):
        raise unavailable("Pimcamp Himalaya adapter configuration is invalid.")
    if backend is not None and account == "default":
        raise unavailable("Pimcamp native mail requires an explicit account selector.")
    if not isinstance(config_paths, list) or any(
        not isinstance(path, str) or not path for path in config_paths
    ):
        raise unavailable("Pimcamp Himalaya config paths are invalid.")
    if junk is not None and (not isinstance(junk, str) or not junk):
        raise unavailable("Pimcamp Himalaya junk mailbox is invalid.")
    if (
        not isinstance(sender, dict)
        or set(sender) != {"name", "address"}
        or (
            sender["name"] is not None
            and not isinstance(sender["name"], str)
        )
        or not isinstance(sender["address"], str)
        or not _sendable_address(sender["address"])
        or (
            sender["name"] is not None
            and ("\r" in sender["name"] or "\n" in sender["name"])
        )
    ):
        raise unavailable("Pimcamp Himalaya sender is invalid.")
    return HimalayaConfig(
        kind="himalaya",
        executable=executable,
        account=account,
        config_paths=tuple(config_paths),
        inbox=inbox,
        junk_mailbox=junk,
        sender={"name": sender["name"], "address": sender["address"]},
        raw=value,
        backend=backend,
    )


def _sendable_address(value: str) -> bool:
    if value.count("@") != 1 or any(
        ord(char) <= 32 or ord(char) == 127 for char in value
    ):
        return False
    local, domain = value.split("@")
    return bool(local and domain)
