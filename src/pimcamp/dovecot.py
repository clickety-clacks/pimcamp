"""Opt-in Dovecot X-GUID identity through Himalaya's existing raw IMAP seam.

Raw-command/stdin support is sourced from Himalaya ca88bee08ad2e92127b46dc6200d1e8201885156.
No local UID snapshot or RFC Message-ID is used as durable identity. Protocol
parsing and hosted-provider continuity still require real lower-adapter evidence.
"""

import json
import re
from email.utils import getaddresses
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
        if not selected:
            return [], False
        # Structured envelopes decode each field without exposing raw literal
        # framing. Bracket that separate call with native identity/generation
        # checks so a reused UID cannot bind an old summary to different mail.
        before = self.fetch_many({folder: selected}, None, deadline)[folder]
        summaries = self.envelopes(folder, selected, deadline)
        after = self.fetch_many({folder: selected}, None, deadline)[folder]
        if before["generation"] != after["generation"]:
            raise PimcampError("conflict", "The mailbox generation changed during listing.")
        records = []
        for uid in selected:
            first = before["messages"].get(uid)
            last = after["messages"].get(uid)
            if first is None or last is None or uid not in summaries:
                continue
            if first["guid"] != last["guid"]:
                raise PimcampError("conflict", "The message changed during listing.")
            guid = last["guid"]
            records.append({
                "adapter_id": _handle(folder, uid, guid), **summaries[uid],
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
        candidates = self.search_many(self.folders(deadline), guid, deadline)
        current = self.fetch_many({folder: uids for folder, uids in candidates.items() if uids}, None, deadline)
        for folder, uids in candidates.items():
            for uid in uids:
                item = current[folder]["messages"].get(uid)
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
        return self.search_many([folder], guid, deadline)[folder]

    def search_many(self, folders: list[str], guid: str | None, deadline: float):
        query = "ALL" if guid is None else "X-GUID " + _quoted(guid)
        commands = []
        for folder in folders:
            commands.extend(["EXAMINE " + _quoted(folder), "UID SEARCH " + query])
        groups = self._command_groups(commands, deadline)
        result = {}
        for index, folder in enumerate(folders):
            searches = [row for row in groups[2 * index + 1] if len(row) >= 2 and row[1] == b"SEARCH"]
            if len(searches) != 1:
                raise unavailable("The IMAP search was incomplete.")
            uids = [_uid(_text(value)) for value in searches[0][2:]]
            if len(uids) != len(set(uids)):
                raise unavailable("The IMAP search contains duplicate UIDs.")
            result[folder] = uids
        return result

    def fetch(self, folder: str, uid: str, body: str | None, deadline: float):
        return self.fetch_many({folder: [uid]}, body, deadline)[folder]["messages"].get(uid)

    def fetch_many(self, requests: dict[str, list[str]], body: str | None, deadline: float):
        if not requests:
            return {}
        fields = "UID X-GUID"
        if body == "body":
            fields += " BODY.PEEK[]"
        commands = []
        for folder, uids in requests.items():
            if not uids:
                raise unavailable("An empty IMAP fetch was requested.")
            commands.extend(["EXAMINE " + _quoted(folder),
                             "UID FETCH " + ",".join(_uid(uid) for uid in uids) + " (" + fields + ")"])
        groups = self._command_groups(commands, deadline)
        result = {}
        for index, (folder, uids) in enumerate(requests.items()):
            generations = [row[2] for row in groups[2 * index]
                           if len(row) == 3 and row[1] == b"UIDVALIDITY"]
            if len(generations) != 1:
                raise unavailable("The IMAP mailbox generation is missing.")
            result[folder] = {"generation": _uid(_text(generations[0])),
                              "messages": _fetch_records(groups[2 * index + 1], uids, body)}
        return result

    def envelopes(self, folder: str, uids: list[str], deadline: float):
        # Himalaya ca88bee src/imap/fetch.rs serializes structured fields after
        # parsing wire literals. Lossy field text cannot shift IMAP framing here.
        output = self.run_json(["imap", "fetch", ",".join(_uid(uid) for uid in uids),
                                "--mailbox", folder, "--envelope", "--flags"], None, deadline)
        if not isinstance(output, dict) or set(output) != {"messages"} or not isinstance(output["messages"], list):
            raise unavailable("Himalaya returned invalid IMAP envelope results.")
        result = {}
        for item in output["messages"]:
            if not isinstance(item, dict) or type(item.get("uid")) is not int:
                raise unavailable("Himalaya returned invalid IMAP envelope metadata.")
            uid = _uid(str(item["uid"]))
            envelope, flags = item.get("envelope"), item.get("flags")
            if uid not in uids or uid in result or not isinstance(envelope, dict):
                raise unavailable("Himalaya returned unexpected IMAP envelope identity.")
            if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
                raise unavailable("Himalaya returned invalid IMAP envelope flags.")
            sender, subject, date = envelope.get("from"), envelope.get("subject"), envelope.get("date")
            if (not isinstance(sender, list) or any(not isinstance(value, str) for value in sender)
                    or subject is not None and not isinstance(subject, str)
                    or date is not None and not isinstance(date, str)):
                raise unavailable("Himalaya returned invalid IMAP envelope fields.")
            result[uid] = {"from": [{"name": name or None, "address": address}
                                    for name, address in getaddresses(sender) if address],
                           "subject": subject or "", "received_at": date,
                           "unread": "\\seen" not in [flag.lower() for flag in flags]}
        return result

    def _commands(self, commands: list[str], deadline: float) -> list[list]:
        return [row for group in self._command_groups(commands, deadline) for row in group]

    def _command_groups(self, commands: list[str], deadline: float) -> list[list[list]]:
        tags = [f"pc{i}" for i in range(len(commands))]
        raw = "".join(f"{tag} {command}\r\n" for tag, command in zip(tags, commands))
        output = self.run_json(["imap", "raw"], raw.encode("utf-8"), deadline)
        if not isinstance(output, dict) or set(output) != {"message"} or not isinstance(output["message"], str):
            raise unavailable("Himalaya returned invalid raw IMAP output.")
        return parse_response(output["message"].encode("utf-8"), tags, grouped=True)


def _fetch_records(rows: list[list], uids: list[str], body: str | None):
    found = {}
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
        if b"X-GUID" not in attrs and b"BODY[]" not in attrs:
            if b"FLAGS" in attrs and set(attrs).issubset({b"UID", b"FLAGS"}):
                continue
            raise unavailable("The IMAP fetch omitted native identity.")
        uid = _uid(_text(attrs.get(b"UID")))
        if uid not in uids or uid in found:
            raise unavailable("The IMAP fetch returned unexpected UIDs.")
        guid = _text(attrs.get(b"X-GUID"))
        _quoted(guid)
        raw = attrs.get(b"BODY[]") if body is not None else None
        if body is not None and not isinstance(raw, bytes):
            raise unavailable("The IMAP fetch omitted message data.")
        found[uid] = {"guid": guid, "body": raw}
    return found


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


def parse_response(raw: bytes, tags: list[str], *, grouped: bool = False) -> list:
    """Check every completion (exit zero alone is insufficient for raw IMAP)."""
    reader = _Reader(raw)
    pending = {tag.encode("ascii") for tag in tags}
    rows = []
    groups = []
    try:
        while reader.pos < len(raw):
            end = raw.index(b"\r\n", reader.pos)
            line = raw[reader.pos:end]
            words = line.split(b" ", 2)
            if words[0] in pending:
                if len(words) < 2 or words[1].upper() != b"OK":
                    raise ValueError()
                # Commands depend on the selected folder. Only ordered tagged
                # boundaries establish attribution; never guess on reordering.
                if grouped and words[0] != tags[len(groups)].encode("ascii"):
                    raise ValueError()
                groups.append(rows)
                rows = []
                pending.remove(words[0])
                reader.pos = end + 2
                continue
            if len(words) < 2 or words[0] != b"*":
                raise ValueError()
            if words[1].upper() in (b"BYE", b"NO", b"BAD"):
                raise ValueError()
            if words[1].upper() in (b"OK", b"PREAUTH"):
                generation = re.fullmatch(rb"\* OK \[UIDVALIDITY ([1-9][0-9]*)\].*", line, re.IGNORECASE)
                if generation:
                    rows.append([b"*", b"UIDVALIDITY", generation[1]])
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
        if rows:
            raise ValueError()
        return groups if grouped else [row for group in groups for row in group]
    except (ValueError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        raise unavailable("The IMAP response was invalid or incomplete.") from exc
