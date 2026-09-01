"""Private owner-readable deployment configuration."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from . import CAPABILITIES
from .errors import unavailable
from .jsonio import canonical_json, loads_one


@dataclass(frozen=True)
class Client:
    identity: str
    grants: frozenset[str]


@dataclass(frozen=True)
class AdapterConfig:
    kind: str
    command: tuple[str, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Config:
    credentials: dict[str, Client]
    adapter_wait_seconds: float
    state_path: Path
    operations: AdapterConfig
    observation: AdapterConfig
    operations_fingerprint: str

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
    if not isinstance(raw, dict) or set(raw) != {
        "credentials",
        "adapter_wait_seconds",
        "state_path",
        "operations_adapter",
        "observation_adapter",
    }:
        raise unavailable("Pimcamp deployment configuration is invalid.")

    credentials = _credentials(raw["credentials"])
    wait = raw["adapter_wait_seconds"]
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait <= 0:
        raise unavailable("Pimcamp adapter wait bound is invalid.")
    state_path = raw["state_path"]
    if not isinstance(state_path, str) or not state_path:
        raise unavailable("Pimcamp state path is invalid.")
    operations = _adapter(raw["operations_adapter"], "operations")
    observation = _adapter(raw["observation_adapter"], "observation")
    fingerprint = hashlib.sha256(canonical_json(operations.raw)).hexdigest()
    return Config(
        credentials=credentials,
        adapter_wait_seconds=float(wait),
        state_path=Path(state_path),
        operations=operations,
        observation=observation,
        operations_fingerprint=fingerprint,
    )


def _credentials(value: Any) -> dict[str, Client]:
    if not isinstance(value, dict):
        raise unavailable("Pimcamp credential configuration is invalid.")
    clients: dict[str, Client] = {}
    for digest, item in value.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(item, dict)
            or set(item) != {"client_identity", "grants"}
            or not isinstance(item["client_identity"], str)
            or not item["client_identity"]
            or not isinstance(item["grants"], list)
            or any(not isinstance(grant, str) for grant in item["grants"])
        ):
            raise unavailable("Pimcamp credential configuration is invalid.")
        grants = frozenset(item["grants"])
        if not grants.issubset(CAPABILITIES) or len(grants) != len(item["grants"]):
            raise unavailable("Pimcamp credential grants are invalid.")
        clients[digest] = Client(item["client_identity"], grants)
    return clients


def _adapter(value: Any, expected: str) -> AdapterConfig:
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
