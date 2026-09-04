"""Himalaya 2.1 mail-operations adapter.

The lower JSON shapes follow the reviewed Himalaya source at commit
bbdfb09b8b8841a509df463a80066531fb81af04 and its locked pimalaya-cli 0.2.4
printer. Parser fixtures must still come from a real credentialed capture.
"""

from datetime import datetime, timezone
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime
import json
import os
import re
import signal
import subprocess
import time
from typing import Any

from .config import HimalayaConfig
from .diagnostics import emit
from .errors import PimcampError, unavailable
from .jsonio import loads_one


_MESSAGE_ID = re.compile(r"<[^<>\s]+>")


class HimalayaOperationsAdapter:
    adapter_class = "himalaya"

    def __init__(self, config: HimalayaConfig):
        self.config = config

    def call(
        self,
        operation: str,
        payload: dict[str, Any],
        deadline: float,
        *,
        mutation: bool = False,
    ) -> Any:
        if operation == "list":
            return self._list(payload, deadline)
        if operation == "read":
            return self._read(payload, deadline)
        if operation == "send":
            return self._send(payload, deadline)
        if operation == "junk_capabilities":
            return {
                "report_spam": False,
                "move_to_junk": self.config.junk_mailbox is not None,
            }
        if operation == "report_spam":
            raise PimcampError("unsupported", "Himalaya has no proved spam-reporting call.")
        if operation == "move_to_junk":
            return self._move_to_junk(payload, deadline)
        raise PimcampError("unsupported", "The Himalaya operation is unsupported.")

    def _list(self, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        page = _page(payload.get("cursor"))
        output = self._run_json(
            [
                "envelope",
                "list",
                "--mailbox",
                self.config.inbox,
                "--page",
                str(page),
                "--page-size",
                str(payload["limit"]),
            ],
            None,
            deadline,
        )
        if not isinstance(output, dict) or set(output) != {"envelopes"}:
            raise unavailable("Himalaya returned an unknown envelope-list shape.")
        envelopes = output["envelopes"]
        if not isinstance(envelopes, list):
            raise unavailable("Himalaya returned an unknown envelope-list shape.")
        messages = [_summary(item) for item in envelopes]
        next_cursor = f"page:{page + 1}" if len(messages) == payload["limit"] else None
        return {"messages": messages, "next_cursor": next_cursor}

    def _read(self, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        output = self._run_json(
            [
                "message",
                "read",
                payload["adapter_id"],
                "--mailbox",
                self.config.inbox,
                "--raw",
            ],
            None,
            deadline,
            missing_is_not_found=True,
        )
        if not isinstance(output, dict) or set(output) != {"message"}:
            raise unavailable("Himalaya returned an unknown raw-message shape.")
        raw = output["message"]
        if not isinstance(raw, str):
            raise unavailable("Himalaya returned an unknown raw-message shape.")
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))
        except Exception as exc:
            raise unavailable("Himalaya returned an invalid RFC 5322 message.") from exc
        body_parts: list[dict[str, str]] = []
        for part in message.walk():
            media_type = part.get_content_type()
            if part.is_multipart() or media_type not in {"text/plain", "text/html"}:
                continue
            if part.get_content_disposition() == "attachment":
                continue
            try:
                content = part.get_content()
            except Exception as exc:
                raise unavailable("Himalaya returned an undecodable message body.") from exc
            if not isinstance(content, str):
                raise unavailable("Himalaya returned an undecodable message body.")
            body_parts.append({"media_type": media_type, "content_utf8": content})
        message_id = _one_message_id(message.get("Message-ID"))
        references = _message_ids(" ".join(message.get_all("References", [])))
        return {
            "from": _addresses(message.get_all("From", [])),
            "to": _addresses(message.get_all("To", [])),
            "cc": _addresses(message.get_all("Cc", [])),
            "reply_to": _addresses(message.get_all("Reply-To", [])),
            "subject": str(message.get("Subject", "")),
            "sent_at": _mail_time(message.get("Date")),
            "body_parts": body_parts,
            "threading": {"message_id": message_id, "references": references},
        }

    def _send(self, payload: dict[str, Any], deadline: float) -> dict[str, str]:
        composition = payload["composition"]
        try:
            raw = self._message(composition, payload["threading"])
        except PimcampError:
            raise
        except Exception as exc:
            raise unavailable("Pimcamp could not build the RFC 5322 message.") from exc
        recipients = [
            item["address"]
            for field in ("to", "cc", "bcc")
            for item in composition[field]
        ]
        args = ["smtp", "send", "--mail-from", self.config.sender["address"]]
        for recipient in recipients:
            args.extend(("--rcpt-to", recipient))
        output = self._run_json(args, raw, deadline, mutation=True)
        if output != {"message": "Message successfully sent"}:
            raise PimcampError("outcome_unknown", "The Himalaya send outcome is unknown.")
        return {"status": "sent"}

    def _move_to_junk(
        self, payload: dict[str, Any], deadline: float
    ) -> dict[str, bool]:
        junk = self.config.junk_mailbox
        if junk is None:
            raise PimcampError("unsupported", "No configured junk mailbox is available.")
        output = self._run_json(
            [
                "message",
                "move",
                "--from",
                self.config.inbox,
                "--to",
                junk,
                payload["adapter_id"],
            ],
            None,
            deadline,
            mutation=True,
        )
        return {"filed": output == {"message": "1 message successfully moved"}}

    def _message(
        self, composition: dict[str, Any], threading_context: dict[str, Any] | None
    ) -> bytes:
        message = EmailMessage(policy=policy.SMTP)
        sender = self.config.sender
        message["From"] = str(
            Address(display_name=sender["name"] or "", addr_spec=sender["address"])
        )
        for header, field in (("To", "to"), ("Cc", "cc")):
            addresses = composition[field]
            if addresses:
                message[header] = ", ".join(_render_address(item) for item in addresses)
        message["Subject"] = composition["subject"]
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid()
        if composition["kind"] == "reply":
            if not isinstance(threading_context, dict) or set(threading_context) != {
                "message_id",
                "references",
            }:
                raise unavailable("The reply threading context is invalid.")
            source_id = threading_context["message_id"]
            references = threading_context["references"]
            if source_id is not None and not isinstance(source_id, str):
                raise unavailable("The reply threading context is invalid.")
            if not isinstance(references, list) or any(
                not isinstance(item, str) for item in references
            ):
                raise unavailable("The reply threading context is invalid.")
            if source_id is not None:
                message["In-Reply-To"] = source_id
                message["References"] = " ".join([*references, source_id])
        message.set_content(composition["body_text"])
        return message.as_bytes()

    def _run_json(
        self,
        args: list[str],
        input_bytes: bytes | None,
        deadline: float,
        *,
        mutation: bool = False,
        missing_is_not_found: bool = False,
    ) -> Any:
        command = self._base_command() + args
        try:
            timeout = _remaining(deadline)
        except PimcampError as exc:
            raise _after_attempt(mutation, "Himalaya exceeded its wait bound.") from exc
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise unavailable("Himalaya could not start.") from exc
        try:
            stdout, _ = process.communicate(input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill(process)
            emit(adapter_class=self.adapter_class, forced_termination=True)
            raise _after_attempt(mutation, "Himalaya exceeded its wait bound.") from exc
        if process.returncode != 0:
            if missing_is_not_found and _is_not_found(stdout):
                raise PimcampError("not_found", "The message no longer exists.")
            raise _after_attempt(mutation, "Himalaya returned a normalized failure.")
        try:
            return loads_one(stdout)
        except PimcampError as exc:
            raise _after_attempt(mutation, "Himalaya returned invalid JSON.") from exc

    def _base_command(self) -> list[str]:
        command = [
            self.config.executable,
            "--json",
            "--account",
            self.config.account,
        ]
        if self.config.config_paths:
            command.extend(("--config", ":".join(self.config.config_paths)))
        return command


def _page(value: Any) -> int:
    if value is None:
        return 1
    if not isinstance(value, str) or not value.startswith("page:"):
        raise PimcampError("invalid_request", "The Himalaya page cursor is invalid.")
    try:
        page = int(value.removeprefix("page:"))
    except ValueError as exc:
        raise PimcampError("invalid_request", "The Himalaya page cursor is invalid.") from exc
    if page < 1:
        raise PimcampError("invalid_request", "The Himalaya page cursor is invalid.")
    return page


def _summary(value: Any) -> dict[str, Any]:
    expected = {
        "id",
        "message-id",
        "in-reply-to",
        "flags",
        "subject",
        "from",
        "to",
        "date",
        "size",
        "has-attachment",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise unavailable("Himalaya returned an unknown envelope shape.")
    if not isinstance(value["id"], str) or not isinstance(value["subject"], str):
        raise unavailable("Himalaya returned an unknown envelope shape.")
    flags = value["flags"]
    if not isinstance(flags, list) or any(
        not isinstance(flag, dict)
        or set(flag) != {"raw", "iana"}
        or not isinstance(flag["raw"], str)
        or flag["iana"] is not None
        and not isinstance(flag["iana"], str)
        for flag in flags
    ):
        raise unavailable("Himalaya returned an unknown flag shape.")
    return {
        "adapter_id": value["id"],
        "from": _lower_addresses(value["from"]),
        "subject": value["subject"],
        "received_at": _iso_time(value["date"]),
        "unread": not any(flag["iana"] == "seen" for flag in flags),
    }


def _lower_addresses(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        raise unavailable("Himalaya returned an unknown address shape.")
    result = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "email"}
            or (
                item["name"] is not None
                and not isinstance(item["name"], str)
            )
            or not isinstance(item["email"], str)
        ):
            raise unavailable("Himalaya returned an unknown address shape.")
        result.append({"name": item["name"], "address": item["email"]})
    return result


def _addresses(headers: list[Any]) -> list[dict[str, str | None]]:
    return [
        {"name": name or None, "address": address}
        for name, address in getaddresses([str(header) for header in headers])
        if address
    ]


def _iso_time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise unavailable("Himalaya returned an invalid message time.")
    try:
        parsed = (
            parsedate_to_datetime(value)
            if "," in value
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _mail_time(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _message_ids(value: str) -> list[str]:
    return _MESSAGE_ID.findall(value)


def _one_message_id(value: Any) -> str | None:
    matches = _message_ids(str(value or ""))
    return matches[0] if matches else None


def _render_address(value: dict[str, Any]) -> str:
    return str(Address(display_name=value["name"] or "", addr_spec=value["address"]))


def _is_not_found(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict) or set(value) != {"error", "sources", "backtrace"}:
        return False
    error = value["error"]
    return isinstance(error, str) and "message" in error.lower() and "not found" in error.lower()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise unavailable("Himalaya exceeded its wait bound.")
    return remaining


def _after_attempt(mutation: bool, message: str) -> PimcampError:
    if mutation:
        return PimcampError("outcome_unknown", "The Himalaya mutation outcome is unknown.")
    return unavailable(message)


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
