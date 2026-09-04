"""Credentialed AC-12 journey with a direct IMAP APPEND stimulus.

The test is disabled unless explicitly enabled. It uses the deployment's
owner-only key helper and prints only fixed result fields and digests.
"""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
import hashlib
import imaplib
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import subprocess
import time
import tomllib
import unittest
import uuid
from urllib.parse import urlsplit


BOUND_SECONDS = 120
START_SECONDS = 30
SAFE_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}


class HarnessFailure(Exception):
    """A fixed, non-sensitive live-harness failure."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def parse_server(value: str) -> tuple[str, int]:
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    elif value.startswith("[") and "]" in value:
        host, _, suffix = value[1:].partition("]")
        port = int(suffix[1:]) if suffix.startswith(":") else 993
    else:
        host, separator, suffix = value.rpartition(":")
        if not separator or not suffix.isdecimal():
            host, port = value, 993
        else:
            port = int(suffix)
    if not host or not 1 <= port <= 65535:
        raise HarnessFailure("invalid_imap_server")
    return host, port


def read_secret(helper: Path, entry: str) -> bytes:
    result = subprocess.run(
        [str(helper), entry],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        env=SAFE_ENV,
    )
    if result.returncode or not result.stdout:
        raise HarnessFailure("credential_unavailable")
    return result.stdout


def runtime_config(config_path: Path, himalaya_path: Path) -> tuple[str, str, str, str, int]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        operations = config["operations_adapter"]
        account = operations["account"]
        inbox = operations["inbox"]
        if operations["kind"] != "himalaya":
            raise HarnessFailure("invalid_runtime_config")
        lower = tomllib.loads(himalaya_path.read_text(encoding="utf-8"))
        account_config = lower["accounts"][account]
        imap_config = account_config["imap"]
        username = imap_config["sasl"]["plain"]["username"]
        server, port = parse_server(imap_config["server"])
        if not all(isinstance(value, str) and value for value in (account, inbox, username)):
            raise HarnessFailure("invalid_runtime_config")
        return account, inbox, username, server, port
    except HarnessFailure:
        raise
    except Exception as exc:
        raise HarnessFailure("invalid_runtime_config") from exc


def invoke(
    executable: Path,
    operation: str,
    value: dict[str, object],
    credential: bytes,
    environment: dict[str, str],
    deadline: float,
) -> dict[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HarnessFailure("journey_timeout")
    request = {
        "contract_version": "pimcamp.v1",
        "client_credential": credential.decode("utf-8"),
        "input": value,
    }
    result = subprocess.run(
        [str(executable), operation],
        input=canonical(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=remaining,
        check=False,
        env=environment,
    )
    if result.returncode:
        raise HarnessFailure(f"{operation}_failed")
    try:
        envelope = json.loads(result.stdout)
    except Exception as exc:
        raise HarnessFailure(f"{operation}_invalid_output") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"contract_version", "result"}
        or envelope["contract_version"] != "pimcamp.v1"
        or not isinstance(envelope["result"], dict)
    ):
        raise HarnessFailure(f"{operation}_invalid_output")
    return envelope["result"]


def read_subscription_line(process: subprocess.Popen[bytes], deadline: float) -> dict[str, object]:
    if process.stdout is None:
        raise HarnessFailure("subscription_output_unavailable")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HarnessFailure("journey_timeout")
    with selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        if not ready.select(remaining):
            raise HarnessFailure("event_timeout")
    line = process.stdout.readline()
    if not line:
        raise HarnessFailure("subscription_stopped")
    try:
        value = json.loads(line)
    except Exception as exc:
        raise HarnessFailure("subscription_invalid_output") from exc
    if not isinstance(value, dict):
        raise HarnessFailure("subscription_invalid_output")
    return value


def stop_subscription(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise HarnessFailure("subscription_cleanup_failed") from exc


def build_message(label: str) -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = "Pimcamp AC-12 <pimcamp-ac12@example.invalid>"
    message["To"] = "Pimcamp AC-12 <pimcamp-ac12@example.invalid>"
    message["Subject"] = label
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = f"<{uuid.uuid4().hex}@pimcamp.invalid>"
    message.set_content("Non-sensitive Pimcamp AC-12 IMAP APPEND test. " + label)
    return message.as_bytes()


def imap_ok(result: tuple[str, list[bytes | None]]) -> list[bytes | None]:
    status, data = result
    if status != "OK":
        raise HarnessFailure("imap_failed")
    return data


def exact_uid(session: imaplib.IMAP4_SSL, label: str) -> str | None:
    data = imap_ok(session.uid("SEARCH", None, "HEADER", "Subject", label))
    raw = data[0] if len(data) == 1 else None
    if raw is None:
        raise HarnessFailure("imap_search_failed")
    uids = raw.split()
    if len(uids) > 1:
        raise HarnessFailure("append_message_not_unique")
    return uids[0].decode("ascii") if uids else None


def cleanup_message(session: imaplib.IMAP4_SSL, label: str) -> None:
    uid = exact_uid(session, label)
    if uid is None:
        return
    imap_ok(session.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"))
    imap_ok(session.uid("EXPUNGE", uid))
    if exact_uid(session, label) is not None:
        raise HarnessFailure("append_cleanup_failed")


class AppendHarnessUnitTests(unittest.TestCase):
    def test_server_without_port_uses_imap_tls_default(self) -> None:
        self.assertEqual(("imap.example", 993), parse_server("imap.example"))

    def test_server_with_port(self) -> None:
        self.assertEqual(("imap.example", 1993), parse_server("imap.example:1993"))


class RealAppendJourney(unittest.TestCase):
    def test_real_imap_append_journey(self) -> None:
        required = {
            "PIMCAMP_APPEND_CONFIG": os.environ.get("PIMCAMP_APPEND_CONFIG"),
            "PIMCAMP_APPEND_HIMALAYA_CONFIG": os.environ.get("PIMCAMP_APPEND_HIMALAYA_CONFIG"),
            "PIMCAMP_APPEND_LIVE_ROOT": os.environ.get("PIMCAMP_APPEND_LIVE_ROOT"),
            "PIMCAMP_APPEND_EXECUTABLE": os.environ.get("PIMCAMP_APPEND_EXECUTABLE"),
            "PIMCAMP_APPEND_STAGE": os.environ.get("PIMCAMP_APPEND_STAGE"),
        }
        missing = [name for name, value in required.items() if not value]
        if os.environ.get("PIMCAMP_APPEND_ENABLE") != "1" or missing:
            detail = ", ".join(missing) if missing else "explicit live enable"
            self.skipTest("real IMAP APPEND journey not enabled; missing " + detail)

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        executable = Path(str(required["PIMCAMP_APPEND_EXECUTABLE"]))
        stage = Path(str(required["PIMCAMP_APPEND_STAGE"]))
        live_root = Path(str(required["PIMCAMP_APPEND_LIVE_ROOT"]))
        config_path = Path(str(required["PIMCAMP_APPEND_CONFIG"]))
        himalaya_path = Path(str(required["PIMCAMP_APPEND_HIMALAYA_CONFIG"]))
        _, inbox, username, server, port = runtime_config(config_path, himalaya_path)
        helper = live_root / "bin" / "pimcamp-keyctl-read"
        client_credential = read_secret(helper, "pimcamp-client")
        imap_password = read_secret(helper, "imap")
        environment = {**SAFE_ENV, "PIMCAMP_CONFIG": str(config_path)}
        subscription: subprocess.Popen[bytes] | None = None
        session: imaplib.IMAP4_SSL | None = None
        label = "PIMCAMP AC-12 APPEND " + uuid.uuid4().hex
        body = "Non-sensitive Pimcamp AC-12 IMAP APPEND test. " + label
        deadline = time.monotonic() + BOUND_SECONDS
        cleanup_error: HarnessFailure | None = None
        try:
            subscription = subprocess.Popen(
                [str(executable), "subscribe_new_mail"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
            assert subscription.stdin is not None
            subscription.stdin.write(
                canonical(
                    {
                        "contract_version": "pimcamp.v1",
                        "client_credential": client_credential.decode("utf-8"),
                        "input": {},
                    }
                )
            )
            subscription.stdin.close()
            started = read_subscription_line(subscription, min(deadline, time.monotonic() + START_SECONDS))
            if started != {"contract_version": "pimcamp.v1", "result": {"status": "started"}}:
                raise HarnessFailure("subscription_start_invalid")

            session = imaplib.IMAP4_SSL(server, port, timeout=10)
            imap_ok(session.login(username, imap_password.decode("utf-8")))
            imap_ok(session.select(inbox, readonly=False))
            imap_ok(session.append(inbox, None, None, build_message(label)))

            event = read_subscription_line(subscription, deadline)
            if (
                set(event) != {"contract_version", "kind", "mailbox", "observed_at"}
                or event.get("contract_version") != "pimcamp.v1"
                or event.get("kind") != "new_mail"
                or event.get("mailbox") != "inbox"
                or not isinstance(event.get("observed_at"), str)
            ):
                raise HarnessFailure("normalized_event_invalid")

            summaries = invoke(executable, "list", {"limit": 100}, client_credential, environment, deadline)
            messages = summaries.get("messages")
            if not isinstance(messages, list):
                raise HarnessFailure("list_invalid_output")
            matches = [item for item in messages if isinstance(item, dict) and item.get("subject") == label]
            if len(matches) != 1 or not isinstance(matches[0].get("message_ref"), str):
                raise HarnessFailure("authoritative_message_not_found")
            message = invoke(
                executable,
                "read",
                {"message_ref": matches[0]["message_ref"]},
                client_credential,
                environment,
                deadline,
            )
            parts = message.get("body_parts")
            if message.get("subject") != label or not isinstance(parts, list) or not any(
                isinstance(part, dict)
                and isinstance(part.get("content_utf8"), str)
                and part["content_utf8"].rstrip("\r\n") == body
                for part in parts
            ):
                raise HarnessFailure("authoritative_read_mismatch")
        except (HarnessFailure, OSError, imaplib.IMAP4.error, subprocess.SubprocessError) as exc:
            if isinstance(exc, HarnessFailure):
                failure = exc
            else:
                failure = HarnessFailure("journey_failed")
            raise failure
        finally:
            if session is not None:
                try:
                    cleanup_message(session, label)
                except (HarnessFailure, OSError, imaplib.IMAP4.error) as exc:
                    if not isinstance(exc, HarnessFailure):
                        exc = HarnessFailure("imap_cleanup_failed")
                    cleanup_error = exc
                try:
                    session.logout()
                except (OSError, imaplib.IMAP4.error):
                    cleanup_error = cleanup_error or HarnessFailure("imap_logout_failed")
            try:
                stop_subscription(subscription)
            except HarnessFailure as exc:
                cleanup_error = cleanup_error or exc
            if stage.exists():
                trashed = subprocess.run(
                    ["gio", "trash", str(stage)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    env=SAFE_ENV,
                )
                if trashed.returncode:
                    cleanup_error = cleanup_error or HarnessFailure("stage_cleanup_failed")
            if cleanup_error is not None:
                raise cleanup_error


if __name__ == "__main__":
    unittest.main()
