"""Closed request and public-value validation."""

from datetime import datetime
import uuid
from typing import Any

from . import CONTRACT_VERSION
from .errors import invalid


_OPERATIONS = {
    "list": ({"limit"}, {"cursor"}),
    "read": ({"message_ref"}, set()),
    "compose": ({"to", "cc", "bcc", "subject", "body_text"}, set()),
    "reply": ({"message_ref", "body_text"}, set()),
    "send": ({"composition", "mutation_id"}, set()),
    "junk": ({"message_ref", "mutation_id"}, set()),
    "subscribe_new_mail": (set(), set()),
}


def _object(value: Any, required: set[str], optional: set[str] = set()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid()
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise invalid()
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise invalid()
    return value


def _nullable_string(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise invalid()
    return value


def _address(value: Any) -> dict[str, str | None]:
    item = _object(value, {"name", "address"})
    name = _nullable_string(item["name"])
    address = _string(item["address"])
    if name is not None and ("\r" in name or "\n" in name):
        raise invalid("An address name contains a forbidden line break.")
    if address.count("@") != 1:
        raise invalid("An address is not sendable.")
    local, domain = address.split("@")
    if not local or not domain:
        raise invalid("An address is not sendable.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in address):
        raise invalid("An address is not sendable.")
    return {"name": name, "address": address}


def _addresses(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        raise invalid()
    return [_address(item) for item in value]


def _subject(value: Any) -> str:
    subject = _string(value)
    if "\r" in subject or "\n" in subject:
        raise invalid("The subject contains a forbidden line break.")
    return subject


def _composition(value: Any) -> dict[str, Any]:
    item = _object(
        value,
        {"kind", "to", "cc", "bcc", "subject", "body_text", "reply_to_message_ref"},
    )
    kind = _string(item["kind"])
    if kind not in {"new", "reply"}:
        raise invalid()
    to = _addresses(item["to"])
    cc = _addresses(item["cc"])
    bcc = _addresses(item["bcc"])
    if not to and not cc and not bcc:
        raise invalid("A composition needs at least one recipient.")
    reply_ref = _nullable_string(item["reply_to_message_ref"])
    if (kind == "new" and reply_ref is not None) or (kind == "reply" and reply_ref is None):
        raise invalid()
    return {
        "kind": kind,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": _subject(item["subject"]),
        "body_text": _string(item["body_text"]),
        "reply_to_message_ref": reply_ref,
    }


def validate_envelope(operation: str, value: Any) -> tuple[str | None, dict[str, Any]]:
    envelope = _object(value, {"contract_version", "input"}, {"client_credential"})
    if envelope["contract_version"] != CONTRACT_VERSION:
        raise invalid("The contract version is unsupported.")
    credential = envelope.get("client_credential")
    if credential is not None and not isinstance(credential, str):
        raise invalid()
    if operation not in _OPERATIONS:
        raise invalid("The operation is unsupported.")
    required, optional = _OPERATIONS[operation]
    raw_input = _object(envelope["input"], required, optional)
    return credential, validate_input(operation, raw_input)


def validate_input(operation: str, value: dict[str, Any]) -> dict[str, Any]:
    if operation == "list":
        limit = value["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise invalid()
        result: dict[str, Any] = {"limit": limit}
        if "cursor" in value:
            result["cursor"] = _string(value["cursor"])
        return result
    if operation == "read":
        return {"message_ref": _string(value["message_ref"])}
    if operation == "compose":
        composition = _composition({"kind": "new", "reply_to_message_ref": None, **value})
        return {key: composition[key] for key in ("to", "cc", "bcc", "subject", "body_text")}
    if operation == "reply":
        return {
            "message_ref": _string(value["message_ref"]),
            "body_text": _string(value["body_text"]),
        }
    if operation == "send":
        return {
            "composition": _composition(value["composition"]),
            "mutation_id": _uuid(value["mutation_id"]),
        }
    if operation == "junk":
        return {
            "message_ref": _string(value["message_ref"]),
            "mutation_id": _uuid(value["mutation_id"]),
        }
    if operation == "subscribe_new_mail":
        return {}
    raise invalid()


def _uuid(value: Any) -> str:
    text = _string(value)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise invalid("The mutation ID is not a UUID.") from exc
    return str(parsed)


def validate_public_address(value: Any) -> dict[str, str | None]:
    return _address(value)


def validate_rfc3339_utc(value: Any) -> str | None:
    if value is None:
        return None
    text = _string(value)
    if not text.endswith("Z"):
        raise invalid("The adapter returned an invalid UTC time.")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise invalid("The adapter returned an invalid UTC time.") from exc
    return text
