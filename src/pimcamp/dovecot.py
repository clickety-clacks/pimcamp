"""Opt-in Dovecot X-GUID identity through Himalaya's existing raw IMAP seam.

Raw-command/stdin support is sourced from Himalaya ca88bee08ad2e92127b46dc6200d1e8201885156.
No local UID snapshot or RFC Message-ID is used as durable identity. Protocol
parsing and hosted-provider continuity still require real lower-adapter evidence.
"""

import json
import re
from typing import Any

from .errors import PimcampError, unavailable
from .identity import validate_identity_evidence


NAMESPACE = "dovecot-x-guid-v1"
_PREFIX = "dovecot:"


def _quoted(value: str) -> str:
    # Raw stdin avoids the lower CLI's inline backslash expansion. Preserve the
    # opaque server value, including quotes/backslashes, without reinterpreting it.
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
        raise unavailable("The IMAP identity or mailbox is invalid.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _uid(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > 4294967295:
        raise unavailable("The IMAP UID is invalid.")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, bytes):
        raise unavailable("The IMAP string is invalid.")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise unavailable("The IMAP string is undecodable.") from exc


def _handle(folder: str, uid: str, guid: str) -> str:
    return _PREFIX + json.dumps([folder, uid, guid], ensure_ascii=True, separators=(",", ":"))


def decode_handle(value: str) -> tuple[str, str, str]:
    try:
        if not isinstance(value, str) or not value.startswith(_PREFIX):
            raise ValueError()
        fields = json.loads(value[len(_PREFIX):])
        if not isinstance(fields, list) or len(fields) != 3:
            raise ValueError()
        folder, uid, guid = fields
        _quoted(folder)
        _uid(uid)
        _quoted(guid)
        return folder, uid, guid
    except (ValueError, TypeError) as exc:
        raise unavailable("The resolved IMAP location is invalid.") from exc


class DovecotIdentity:
    """Small normalized private ports; tests replace these, not CLI captures."""

    def __init__(self, run_json):
        self.run_json = run_json

    def list_page(self, folder: str, page: int, limit: int, deadline: float):
        uids = sorted(self.search(folder, None, deadline), key=int, reverse=True)
        start = (page - 1) * limit
        selected = uids[start:start + limit]
        records = []
        for uid in selected:
            # Headers and GUID belong to the same FETCH, even if the earlier
            # search's UID has disappeared or been reused after a rebuild.
            item = self.fetch(folder, uid, "header", deadline)
            if item is None:
                continue
            guid = item["guid"]
            records.append({
                "adapter_id": _handle(folder, uid, guid),
                "headers": item["body"], "unread": item["unread"],
                "identity_evidence": {
                    "namespace": NAMESPACE, "key": guid, "continuity": "native",
                    "backend": "dovecot", "folder": folder, "generation": None,
                },
            })
        return records, start + limit < len(uids)

    def resolve(self, value: Any, deadline: float) -> dict[str, Any]:
        evidence = validate_identity_evidence(value)
        if evidence is None or any(evidence[k] != v for k, v in {
            "namespace": NAMESPACE, "continuity": "native", "backend": "dovecot",
            "generation": None,
        }.items()):
            raise PimcampError("unsupported", "The Dovecot identity context is unsupported.")
        guid = evidence["key"]
        _quoted(guid)
        matches = []
        # LIST, not LSUB: unsubscribed folders also count. Do not suppress a
        # failed EXAMINE/SEARCH or pick one occurrence of a copied GUID.
        for folder in self.folders(deadline):
            for uid in self.search(folder, guid, deadline):
                item = self.fetch(folder, uid, None, deadline)
                if item is None or item["guid"] != guid:
                    raise PimcampError("conflict", "The message changed during identity lookup.")
                matches.append({"adapter_id": _handle(folder, uid, guid)})
        return {"matches": matches}

    def read(self, folder: str, uid: str, guid: str, deadline: float) -> bytes:
        item = self.fetch(folder, uid, "body", deadline)
        if item is None:
            raise PimcampError("not_found", "The resolved message no longer exists.")
        if item["guid"] != guid:
            raise PimcampError("conflict", "The resolved message identity changed.")
        return item["body"]

    def folders(self, deadline: float) -> list[str]:
        rows = self._commands(['LIST "" "*"'], deadline)
        folders = []
        for row in rows:
            if len(row) >= 2 and row[1] == b"LIST":
                if len(row) != 5 or not isinstance(row[2], list):
                    raise unavailable("The IMAP folder list is invalid.")
                flags = [_text(flag).lower() for flag in row[2]]
                if "\\noselect" in flags or "\\nonexistent" in flags:
                    continue
                folder = _text(row[4])
                _quoted(folder)
                if folder in folders:
                    raise unavailable("The IMAP folder list contains duplicate entries.")
                folders.append(folder)
        if not folders:
            raise unavailable("No selectable IMAP folders were established.")
        return folders

    def search(self, folder: str, guid: str | None, deadline: float) -> list[str]:
        query = "ALL" if guid is None else "X-GUID " + _quoted(guid)
        rows = self._commands(["EXAMINE " + _quoted(folder), "UID SEARCH " + query], deadline)
        searches = [row for row in rows if len(row) >= 2 and row[1] == b"SEARCH"]
        if len(searches) != 1:
            raise unavailable("The IMAP search was incomplete.")
        uids = [_uid(_text(value)) for value in searches[0][2:]]
        if len(uids) != len(set(uids)):
            raise unavailable("The IMAP search contains duplicate UIDs.")
        return uids

    def fetch(self, folder: str, uid: str, body: str | None, deadline: float):
        fields = "UID X-GUID"
        if body == "header":
            fields += " FLAGS BODY.PEEK[HEADER]"
        elif body == "body":
            fields += " BODY.PEEK[]"
        rows = self._commands([
            "EXAMINE " + _quoted(folder), "UID FETCH " + _uid(uid) + " (" + fields + ")",
        ], deadline)
        found = []
        for row in rows:
            if len(row) < 3 or row[2] != b"FETCH":
                continue
            if len(row) != 4 or not isinstance(row[3], list) or len(row[3]) % 2:
                raise unavailable("The IMAP fetch is invalid.")
            pairs = row[3]
            if any(not isinstance(key, bytes) for key in pairs[::2]):
                raise unavailable("The IMAP fetch attributes are invalid.")
            attrs = dict(zip(pairs[::2], pairs[1::2]))
            if len(attrs) * 2 != len(pairs):
                raise unavailable("The IMAP fetch has duplicate attributes.")
            # Ignore unsolicited flag-only FETCH updates, never an alternate UID.
            if b"X-GUID" not in attrs and b"BODY[]" not in attrs and b"BODY[HEADER]" not in attrs:
                if b"FLAGS" in attrs and set(attrs).issubset({b"UID", b"FLAGS"}):
                    continue
                raise unavailable("The IMAP fetch omitted native identity.")
            if _uid(_text(attrs.get(b"UID"))) != uid:
                raise unavailable("The IMAP fetch returned a different UID.")
            guid = _text(attrs.get(b"X-GUID"))
            _quoted(guid)
            raw = None
            unread = False
            if body is not None:
                raw = attrs.get(b"BODY[HEADER]" if body == "header" else b"BODY[]")
                if not isinstance(raw, bytes):
                    raise unavailable("The IMAP fetch omitted message data.")
            if body == "header":
                flags = attrs.get(b"FLAGS")
                if not isinstance(flags, list):
                    raise unavailable("The IMAP fetch omitted message flags.")
                unread = "\\seen" not in [_text(flag).lower() for flag in flags]
            found.append({"guid": guid, "body": raw, "unread": unread})
        if len(found) > 1:
            raise unavailable("The IMAP fetch returned multiple messages.")
        return found[0] if found else None

    def _commands(self, commands: list[str], deadline: float) -> list[list]:
        tags = [f"pc{i}" for i in range(len(commands))]
        raw = "".join(f"{tag} {command}\r\n" for tag, command in zip(tags, commands))
        output = self.run_json(["imap", "raw"], raw.encode("utf-8"), deadline)
        if not isinstance(output, dict) or set(output) != {"message"} or not isinstance(output["message"], str):
            raise unavailable("Himalaya returned invalid raw IMAP output.")
        return parse_response(output["message"].encode("utf-8"), tags)


class _Reader:
    """Only the IMAP data grammar needed by LIST/SEARCH/FETCH; literal-aware."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.pos = 0

    def value(self):
        raw = self.raw
        if self.pos >= len(raw):
            raise ValueError()
        char = raw[self.pos:self.pos + 1]
        self.pos += 1
        if char == b"(":
            values = []
            while True:
                while raw[self.pos:self.pos + 1] == b" ":
                    self.pos += 1
                if raw[self.pos:self.pos + 1] == b")":
                    self.pos += 1
                    return values
                values.append(self.value())
        if char == b'"':
            value = bytearray()
            while self.pos < len(raw):
                char = raw[self.pos:self.pos + 1]
                self.pos += 1
                if char == b'"':
                    return bytes(value)
                if char == b"\\":
                    char = raw[self.pos:self.pos + 1]
                    self.pos += 1
                    if char not in (b"\\", b'"'):
                        raise ValueError()
                if char in (b"\r", b"\n", b"\0"):
                    raise ValueError()
                value.extend(char)
            raise ValueError()
        if char == b"{":
            end = raw.index(b"}\r\n", self.pos)
            length = raw[self.pos:end]
            if not length.isdigit():
                raise ValueError()
            self.pos = end + 3
            end = self.pos + int(length)
            if end > len(raw):
                raise ValueError()
            value = raw[self.pos:end]
            self.pos = end
            return value
        start = self.pos - 1
        while self.pos < len(raw) and raw[self.pos:self.pos + 1] not in (b" ", b"(", b")", b"\r", b"\n"):
            self.pos += 1
        value = raw[start:self.pos]
        if not value or char in (b")", b"\r", b"\n", b" "):
            raise ValueError()
        return None if value.upper() == b"NIL" else value


def parse_response(raw: bytes, tags: list[str]) -> list[list]:
    """Check every completion (exit zero alone is insufficient for raw IMAP)."""
    reader = _Reader(raw)
    pending = {tag.encode("ascii") for tag in tags}
    rows = []
    try:
        while reader.pos < len(raw):
            end = raw.index(b"\r\n", reader.pos)
            line = raw[reader.pos:end]
            words = line.split(b" ", 2)
            if words[0] in pending:
                if len(words) < 2 or words[1].upper() != b"OK":
                    raise ValueError()
                pending.remove(words[0])
                reader.pos = end + 2
                continue
            if len(words) < 2 or words[0] != b"*":
                raise ValueError()
            if words[1].upper() in (b"BYE", b"NO", b"BAD"):
                raise ValueError()
            if words[1].upper() in (b"OK", b"PREAUTH"):
                reader.pos = end + 2
                continue
            row = []
            while raw[reader.pos:reader.pos + 2] != b"\r\n":
                if raw[reader.pos:reader.pos + 1] == b" ":
                    reader.pos += 1
                else:
                    row.append(reader.value())
            reader.pos += 2
            # Protocol names, unlike mailbox names/GUIDs/literals, ignore case.
            if len(row) > 1 and isinstance(row[1], bytes):
                row[1] = row[1].upper()
            if len(row) > 2 and isinstance(row[2], bytes):
                row[2] = row[2].upper()
            if len(row) == 4 and row[2] == b"FETCH" and isinstance(row[3], list):
                row[3][::2] = [key.upper() for key in row[3][::2]]
            rows.append(row)
        if pending:
            raise ValueError()
        return rows
    except (ValueError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        raise unavailable("The IMAP response was invalid or incomplete.") from exc
