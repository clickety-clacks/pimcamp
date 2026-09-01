"""Strict JSON parsing and content-safe wire output."""

import json
from typing import Any

from .errors import PimcampError, invalid


class _DuplicateMember(ValueError):
    pass


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateMember(key)
        value[key] = item
    return value


def loads_one(raw: bytes | str) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise invalid("The request is not valid UTF-8 JSON.") from exc

    decoder = json.JSONDecoder(object_pairs_hook=_closed_object)
    try:
        value, end = decoder.raw_decode(text)
    except (_DuplicateMember, json.JSONDecodeError, ValueError) as exc:
        raise invalid("The request is not one valid JSON value.") from exc
    if text[end:].strip():
        raise invalid("The request contains a second JSON value.")
    return value


def dumps_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def canonical_json(value: object) -> bytes:
    """RFC 8785 bytes for the closed mutation schemas.

    Mutation inputs contain fixed ASCII member names and no JSON numbers.
    Python's minimal UTF-8 string encoding and sorted compact object form are
    therefore the RFC 8785 form for every accepted mutation value.
    """

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PimcampError("invalid_request", "The mutation input is not canonical JSON.") from exc
    return text.encode("utf-8")
