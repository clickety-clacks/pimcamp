"""Private, opt-in capture support for the real-adapter acceptance runner.

The hook observes real lower-tool subprocesses. It redacts their data before
writing and is installed only in the runner's Pimcamp child processes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
from collections import Counter, defaultdict
from typing import Any


EVIDENCE_ENV = "PIMCAMP_LIVE_EVIDENCE_FILE"
SUPPORT_PATH = str(Path(__file__).resolve().parent)

_SAFE_COMMAND_TOKENS = {
    "--json",
    "envelope",
    "list",
    "message",
    "read",
    "smtp",
    "send",
    "move",
    "--raw",
    "watch",
    "--page",
    "--page-size",
}
_REDACTED_ARGUMENTS = {
    "--account": "<selected-account>",
    "--config": "<redacted-config-paths>",
    "--mailbox": "<selected-mailbox>",
    "--mail-from": "<selected-sender>",
    "--rcpt-to": "<configured-recipient>",
    "--from": "<selected-mailbox>",
    "--to": "<junk-mailbox>",
    "--backend": "<backend>",
}
_SAFE_FLAGS = {
    "Seen",
    "Answered",
    "Flagged",
    "Deleted",
    "Draft",
    "Recent",
}
_SAFE_MESSAGES = {
    "Message successfully sent",
    "1 message successfully moved",
}
_SENSITIVE_JSON_KEYS = {
    "id",
    "message_id",
    "message-id",
    "subject",
    "address",
    "email",
    "name",
    "body",
    "content",
    "date",
    "sent_at",
    "received_at",
    "references",
    "in_reply_to",
}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_TRANSFER_ENCODINGS = {"7bit", "8bit", "binary", "base64", "quoted-printable"}
_VERSION_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?(?![A-Za-z0-9])"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _append_record(path: str | Path, record: dict[str, Any]) -> None:
    data = _canonical({**record, "observed_monotonic_ns": time.monotonic_ns()}) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class EvidenceRecorder:
    """Create and summarize one private JSONL record for a live journey."""

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def validate_destination(cls, destination: str, repository_root: Path) -> Path:
        requested = Path(destination)
        if not requested.is_absolute() or requested.name in {"", ".", ".."}:
            raise ValueError("evidence path must be an absolute file path")
        parent = requested.parent.resolve(strict=True)
        if not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise ValueError("evidence directory must be private")
        try:
            parent.relative_to(repository_root.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("evidence file must be outside the repository")

        path = parent / requested.name
        if path.exists():
            raise ValueError("evidence file already exists")
        return path

    @classmethod
    def create(cls, destination: str, repository_root: Path) -> "EvidenceRecorder":
        path = cls.validate_destination(destination, repository_root)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
                raise ValueError("evidence file permissions are not private")
        finally:
            os.close(descriptor)
        return cls(path)

    def record(self, kind: str, **values: Any) -> None:
        _append_record(self.path, {"kind": kind, **values})

    def records(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines if line]
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("private live evidence could not be read") from error

    def finish(
        self,
        outcome: str,
        *,
        checks: dict[str, bool | None],
        junk_mechanism: str,
    ) -> dict[str, Any]:
        records = self.records()
        client_calls = [
            item for item in records if item.get("kind") == "client_call_start"
        ]
        client_results = [
            item for item in records if item.get("kind") == "client_call_end"
        ]
        lower_calls = [
            item for item in records if item.get("kind") == "lower_adapter_call_start"
        ]
        captures = [
            item for item in records if item.get("kind") == "lower_adapter_capture"
        ]
        client_counts = Counter(str(item.get("operation")) for item in client_calls)
        lower_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for item in lower_calls:
            lower_counts[str(item.get("adapter"))][str(item.get("operation"))] += 1
        metadata = next(
            (item for item in records if item.get("kind") == "journey_metadata"),
            {},
        )
        if outcome == "passed":
            if (
                metadata.get("schema") != "pimcamp.live-evidence.v1"
                or not isinstance(metadata.get("source_commit"), str)
                or not _COMMIT.fullmatch(metadata["source_commit"])
                or metadata.get("integration_ref") != "origin/main"
                or not metadata.get("python_version")
                or not metadata.get("sqlite_version")
            ):
                raise AssertionError(
                    "journey evidence omitted exact source or runtime versions (output suppressed)"
                )
            version_records = [
                item for item in records if item.get("kind") == "tool_version"
            ]
            if {item.get("tool") for item in version_records} != {
                "Himalaya",
                "Mirador/Carillon",
            } or any(
                not isinstance(item.get("version"), str)
                or not _VERSION_TOKEN.fullmatch(item["version"])
                or not isinstance(item.get("expected_version"), str)
                or not _VERSION_TOKEN.fullmatch(item["expected_version"])
                or item["version"].removeprefix("v")
                != item["expected_version"].removeprefix("v")
                or not _SHA256.fullmatch(str(item.get("output_sha256", "")))
                for item in version_records
            ):
                raise AssertionError(
                    "journey evidence omitted exact lower-tool versions (output suppressed)"
                )
            required_checks = {
                "return_token_in_subject",
                "return_token_in_body",
                "matching_message_filed",
                "matching_subject_absent_after_filing",
                "bounded_subscription_teardown",
            }
            if not required_checks.issubset(
                {name for name, passed in checks.items() if passed is True}
            ):
                raise AssertionError(
                    "journey evidence omitted a required acceptance check (output suppressed)"
                )
            if metadata.get("human_return_mode") and checks.get(
                "configured_distinct_sender"
            ) is not True:
                raise AssertionError(
                    "human-return provenance evidence is incomplete (output suppressed)"
                )
            if metadata.get("human_return_mode"):
                provenance = next(
                    (
                        item
                        for item in records
                        if item.get("kind") == "return_provenance"
                    ),
                    {},
                )
                if any(
                    provenance.get(field) != expected
                    for field, expected in {
                        "return_token_in_subject": True,
                        "return_token_in_body": True,
                        "configured_recipient_source": "PIMCAMP_LIVE_RECIPIENT",
                        "selected_sender_source": "operations_adapter.from.address",
                        "observed_sender_source": "read-result.from",
                        "selected_account_placeholder": "A",
                        "configured_recipient_placeholder": "B",
                        "observed_return_sender_placeholder": "B",
                        "addresses_redacted": True,
                        "configured_recipient_differs_from_selected_sender": True,
                        "return_sender_matches_configured_recipient": True,
                    }.items()
                ):
                    raise AssertionError(
                        "human-return provenance record is incomplete (output suppressed)"
                    )
            started = Counter(str(item.get("operation")) for item in client_calls)
            ended = Counter(str(item.get("operation")) for item in client_results)
            if started != ended:
                raise AssertionError(
                    "content-free client call trace is incomplete (output suppressed)"
                )
            if any(
                item.get("outcome") not in {"success", "stopped"}
                for item in client_results
            ):
                raise AssertionError(
                    "content-free client trace contains a failed operation (output suppressed)"
                )
            required_client_operations = {
                "subscribe_new_mail",
                "compose",
                "send",
                "list",
                "read",
                "reply",
                "junk",
            }
            if not required_client_operations.issubset(started):
                raise AssertionError(
                    "content-free client trace did not cover all operations (output suppressed)"
                )
            required_client_counts = {
                "subscribe_new_mail": 1,
                "compose": 1,
                "send": 2,
                "read": 1,
                "reply": 1,
                "junk": 1,
            }
            if any(
                started.get(operation, 0) != count
                for operation, count in required_client_counts.items()
            ):
                raise AssertionError(
                    "content-free client trace did not cover the complete journey (output suppressed)"
                )
            order = [str(item.get("operation")) for item in client_calls]
            try:
                read_index = order.index("read")
            except ValueError:
                read_index = -1
            if (
                order[:3] != ["subscribe_new_mail", "compose", "send"]
                or read_index < 4
                or order[read_index - 1] != "list"
                or order[read_index + 1 : read_index + 4]
                != ["reply", "send", "junk"]
                or order[-1] != "list"
            ):
                raise AssertionError(
                    "content-free client trace did not preserve the acceptance journey order (output suppressed)"
                )
        if outcome == "passed" and len(captures) != len(lower_calls):
            raise AssertionError(
                "real lower-adapter capture was incomplete (content suppressed)"
            )
        if outcome == "passed" and Counter(
            (str(item.get("adapter")), str(item.get("operation")))
            for item in lower_calls
        ) != Counter(
            (str(item.get("adapter")), str(item.get("operation")))
            for item in captures
        ):
            raise AssertionError(
                "real lower-adapter captures did not match observed calls (content suppressed)"
            )
        if outcome == "passed" and not any(
            item.get("adapter") == "carillon"
            and item.get("output") == '{"event":"new_mail"}'
            for item in records
            if item.get("kind") == "lower_adapter_output_line"
        ):
            raise AssertionError(
                "real observation output capture omitted the accepted event (content suppressed)"
            )
        if outcome == "passed" and (
            lower_counts["himalaya"].get("send", 0) != 2
            or lower_counts["himalaya"].get("list", 0) < 1
            or lower_counts["himalaya"].get("read", 0) != 1
            or lower_counts["himalaya"].get("move_to_junk", 0) != 1
            or lower_counts["carillon"].get("watch", 0) != 1
        ):
            raise AssertionError(
                "real lower-adapter call evidence did not cover the journey (content suppressed)"
            )
        if outcome == "passed" and any(
            item.get("timed_out") is True
            or (
                item.get("adapter") == "himalaya"
                and item.get("returncode") != 0
            )
            or (
                item.get("adapter") == "carillon"
                and item.get("returncode") not in {0, -signal.SIGTERM}
            )
            for item in captures
        ):
            raise AssertionError(
                "a real lower-adapter capture did not complete successfully (content suppressed)"
            )
        summary = {
            "kind": "journey_summary",
            "outcome": outcome,
            "human_return_mode": metadata.get("human_return_mode", False),
            "automatic_responder_behavior_proved": False,
            "unattended_repeatability_proved": False,
            "event_order": [
                {
                    "kind": item.get("kind"),
                    "operation": item.get("operation"),
                    "adapter": item.get("adapter"),
                    "output_kind": item.get("output_kind"),
                }
                for item in records
                if item.get("kind")
                in {
                    "client_call_start",
                    "client_call_end",
                    "subscription_output",
                    "lower_adapter_call_start",
                    "lower_adapter_output_line",
                    "lower_adapter_capture",
                }
            ],
            "client_call_order": [str(item.get("operation")) for item in client_calls],
            "client_call_counts": dict(sorted(client_counts.items())),
            "client_call_results": [
                {"operation": item.get("operation"), "outcome": item.get("outcome")}
                for item in client_results
            ],
            "lower_call_order": [
                {"adapter": item.get("adapter"), "operation": item.get("operation")}
                for item in lower_calls
            ],
            "lower_call_counts": {
                adapter: dict(sorted(counts.items()))
                for adapter, counts in sorted(lower_counts.items())
            },
            "capture_sha256": [str(item["capture_sha256"]) for item in captures],
            "checks": checks,
            "junk_mechanism": junk_mechanism,
        }
        self.record(**summary)
        return summary


def redact_command(command: list[str] | tuple[str, ...], adapter: str) -> list[str]:
    """Keep command syntax and replace local identities, addresses and paths."""
    values = [str(item) for item in command]
    if not values:
        return []
    result = [f"<{adapter}>"]
    index = 1
    while index < len(values):
        item = values[index]
        if item in _REDACTED_ARGUMENTS:
            result.extend((item, _REDACTED_ARGUMENTS[item]))
            index += 2
        elif item in {"--page", "--page-size"}:
            result.append(item)
            if index + 1 < len(values) and values[index + 1].isdigit():
                result.append(values[index + 1])
                index += 2
            else:
                index += 1
        elif item in _SAFE_COMMAND_TOKENS:
            result.append(item)
            index += 1
        elif item.startswith("--"):
            result.append(item)
            index += 1
        else:
            result.append("<redacted-value>")
            index += 1
    return result


def human_return_provenance_matches(
    message: Any, configured_recipient: str, selected_sender: str
) -> bool:
    """Require one read-result sender matching a distinct configured address."""
    recipient = configured_recipient.strip().casefold()
    selected = selected_sender.strip().casefold()
    if not recipient or not selected or recipient == selected:
        return False
    senders = message.get("from") if isinstance(message, dict) else None
    if not isinstance(senders, list) or len(senders) != 1:
        return False
    sender = senders[0]
    if not isinstance(sender, dict):
        return False
    address = sender.get("address")
    return isinstance(address, str) and address.strip().casefold() == recipient


def observed_version(expected: str, output: str) -> str:
    """Extract the version token from the tool output that confirms the pin."""
    expected_tokens = list(_VERSION_TOKEN.finditer(expected))
    if not expected_tokens:
        raise ValueError(
            "configured tool version did not contain an exact version token"
        )
    expected_token = expected_tokens[-1].group(0)
    for line in output.splitlines():
        start = line.find(expected)
        if start < 0:
            continue
        end = start + len(expected)
        for match in _VERSION_TOKEN.finditer(line):
            if (
                match.start() < end
                and match.end() > start
                and match.group(0).removeprefix("v")
                == expected_token.removeprefix("v")
            ):
                return match.group(0)
    raise ValueError("tool version output did not contain an exact version token")


def _sanitize_rfc822(message: str) -> str:
    """Retain RFC header structure while removing header values and the body."""
    lines = message.splitlines(keepends=True)
    if not lines:
        return "<redacted-message>"

    result: list[str] = []
    skip_folded = False
    body_started = False
    for line in lines:
        if line in {"\r\n", "\n", "\r"}:
            result.append(line)
            result.append("<redacted-body>\r\n")
            body_started = True
            break
        if line[:1] in {" ", "\t"}:
            if not skip_folded:
                result.append(line)
            continue

        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        raw_header = line[: -len(ending)] if ending else line
        name, separator, value = raw_header.partition(":")
        if not separator or not _HEADER_NAME.fullmatch(name):
            return "<redacted-message>"
        normalized_name = name.casefold()
        if normalized_name == "content-type":
            media_type = value.strip().split(";", 1)[0]
            if not _MEDIA_TYPE.fullmatch(media_type):
                return "<redacted-message>"
            result.append(f"{name}: {media_type}{ending}")
            skip_folded = True
        elif normalized_name == "mime-version" and value.strip() == "1.0":
            result.append(f"{name}: 1.0{ending}")
            skip_folded = True
        elif (
            normalized_name == "content-transfer-encoding"
            and value.strip().casefold() in _TRANSFER_ENCODINGS
        ):
            result.append(f"{name}: {value.strip().casefold()}{ending}")
            skip_folded = True
        else:
            result.append(f"{name}: <redacted>{ending}")
            skip_folded = True

    if not body_started:
        return "<redacted-message>"
    return "".join(result)


def _redact_json(value: Any, key: str | None = None) -> Any:
    if key in _SENSITIVE_JSON_KEYS and not isinstance(value, (dict, list)):
        return "<redacted>"
    if isinstance(value, dict):
        return {name: _redact_json(item, name.casefold()) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, key) for item in value]
    if isinstance(value, str):
        if key == "message" and value in _SAFE_MESSAGES:
            return value
        if key == "message":
            return _sanitize_rfc822(value)
        if key == "flag" or key == "flags":
            return value if value in _SAFE_FLAGS else "<redacted>"
        if key in {"media_type", "kind", "event"} and value in {
            "text/plain",
            "text/html",
            "new_mail",
        }:
            return value
        return "<redacted>"
    return value


def redact_lower_output(adapter: str, output: bytes | str) -> str:
    """Return syntactically valid, content-redacted lower output."""
    raw = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return "<redacted-non-json-output>"
    if adapter == "carillon":
        if isinstance(value, dict) and value == {"event": "new_mail"}:
            return json.dumps(value, separators=(",", ":"), sort_keys=True)
        return "<redacted-carillon-output>"
    return json.dumps(_redact_json(value), ensure_ascii=False, separators=(",", ":"))


def _classify(command: Any) -> str | None:
    if isinstance(command, str):
        values = command.split()
    else:
        try:
            values = [str(item) for item in command]
        except TypeError:
            return None
    if "--json" in values and any(item in values for item in ("envelope", "message", "smtp")):
        return "himalaya"
    if "watch" in values and "--backend" in values:
        return "carillon"
    return None


def _operation(command: list[str] | tuple[str, ...], adapter: str) -> str:
    values = [str(item) for item in command]
    if adapter == "carillon":
        return "watch"
    if "envelope" in values and "list" in values:
        return "list"
    if "smtp" in values and "send" in values:
        return "send"
    if "message" in values and "read" in values:
        return "read"
    if "message" in values and "move" in values:
        return "move_to_junk"
    return "unknown"


def _clean_child_environment(kwargs: dict[str, Any]) -> None:
    environment = kwargs.get("env")
    clean = os.environ.copy() if environment is None else environment.copy()
    for name in tuple(clean):
        if name.startswith("PIMCAMP_"):
            clean.pop(name, None)
    parts = clean.get("PYTHONPATH", "").split(os.pathsep)
    parts = [item for item in parts if item and os.path.abspath(item) != SUPPORT_PATH]
    if parts:
        clean["PYTHONPATH"] = os.pathsep.join(parts)
    else:
        clean.pop("PYTHONPATH", None)
    kwargs["env"] = clean


def install_if_requested() -> None:
    """Patch subprocess only in a Pimcamp child with a private output file."""
    path = os.environ.get(EVIDENCE_ENV)
    if not path:
        return

    original = subprocess.Popen

    class CapturingPopen(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any):
            command = kwargs.get("args", args[0] if args else [])
            adapter = _classify(command)
            self._capture_adapter = adapter
            self._capture_path = path if adapter else None
            self._capture_command = (
                [str(item) for item in command]
                if not isinstance(command, str)
                else command.split()
            )
            self._capture_operation = (
                _operation(self._capture_command, adapter) if adapter else ""
            )
            self._capture_lines: list[str] = []
            self._capture_finished = False
            if adapter:
                _clean_child_environment(kwargs)
                safe_command = redact_command(self._capture_command, adapter)
                _append_record(
                    path,
                    {
                        "kind": "lower_adapter_call_start",
                        "adapter": adapter,
                        "operation": self._capture_operation,
                        "command": safe_command,
                        "command_sha256": hashlib.sha256(
                            _canonical(safe_command)
                        ).hexdigest(),
                    },
                )
            super().__init__(*args, **kwargs)
            if adapter == "carillon" and self.stdout is not None:
                self.stdout = CapturingStream(self.stdout, self)

        def communicate(self, *args: Any, **kwargs: Any):
            adapter = getattr(self, "_capture_adapter", None)
            if adapter != "himalaya":
                return super().communicate(*args, **kwargs)
            input_value = kwargs.get("input", args[0] if args else None)
            try:
                output = super().communicate(*args, **kwargs)
            except subprocess.TimeoutExpired as error:
                stdout = error.output or b""
                self._record_himalaya_capture(input_value, stdout, timed_out=True)
                raise
            stdout = output[0] if output else b""
            self._record_himalaya_capture(input_value, stdout, timed_out=False)
            return output

        def _record_himalaya_capture(
            self, input_value: Any, stdout: Any, *, timed_out: bool
        ) -> None:
            if self._capture_finished:
                return
            self._capture_finished = True
            if isinstance(input_value, bytes):
                safe_input = _sanitize_rfc822(input_value.decode("utf-8", errors="replace"))
            elif input_value is None:
                safe_input = None
            else:
                safe_input = "<redacted-input>"
            safe_output = redact_lower_output("himalaya", stdout or b"")
            payload = {
                "kind": "lower_adapter_capture",
                "adapter": "himalaya",
                "operation": self._capture_operation,
                "command": redact_command(self._capture_command, "himalaya"),
                "stdin": safe_input,
                "stdout": safe_output,
                "returncode": None if timed_out else self.returncode,
                "timed_out": timed_out,
            }
            payload["capture_sha256"] = hashlib.sha256(
                _canonical(payload)
            ).hexdigest()
            _append_record(self._capture_path, payload)

        def poll(self):
            result = super().poll()
            if getattr(self, "_capture_adapter", None) == "carillon" and result is not None:
                self._finish_carillon(result)
            return result

        def wait(self, *args: Any, **kwargs: Any):
            result = super().wait(*args, **kwargs)
            if getattr(self, "_capture_adapter", None) == "carillon":
                self._finish_carillon(result)
            return result

        def _finish_carillon(self, returncode: int) -> None:
            if self._capture_finished:
                return
            self._capture_finished = True
            command = redact_command(self._capture_command, "carillon")
            stdout = "".join(self._capture_lines)
            payload = {
                "kind": "lower_adapter_capture",
                "adapter": "carillon",
                "operation": "watch",
                "command": command,
                "stdout": stdout,
                "stdout_lines": len(self._capture_lines),
                "capture_scope": "observed-output-lines",
                "returncode": returncode,
            }
            payload["capture_sha256"] = hashlib.sha256(
                _canonical(payload)
            ).hexdigest()
            _append_record(self._capture_path, payload)

    class CapturingStream:
        def __init__(self, stream: Any, process: Any):
            self._stream = stream
            self._process = process

        def readline(self, *args: Any, **kwargs: Any) -> bytes:
            line = self._stream.readline(*args, **kwargs)
            if not line:
                return line
            safe = redact_lower_output("carillon", line)
            self._process._capture_lines.append(safe + "\n")
            _append_record(
                path,
                {
                    "kind": "lower_adapter_output_line",
                    "adapter": "carillon",
                    "operation": "watch",
                    "output": safe,
                    "output_sha256": hashlib.sha256(safe.encode("utf-8")).hexdigest(),
                },
            )
            return line

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    subprocess.Popen = CapturingPopen  # type: ignore[assignment]
