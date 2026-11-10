"""Credentialed real-adapter acceptance journey.

This module must never replace absent credentials or lower tools with invented
Himalaya responses or Mirador events.
"""

import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
import warnings

from tests.support.live_evidence import (
    EVIDENCE_ENV,
    EvidenceRecorder,
    SUPPORT_PATH,
    human_return_provenance_matches,
    observed_version,
)


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "pimcamp"
EXTERNAL_BOUND_SECONDS = 120
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _prompt_for_client_credential() -> str:
    # Drop the former secret environment variable before any child environment
    # is copied, including when an older runner still supplies it.
    os.environ.pop("PIMCAMP_LIVE_CREDENTIAL", None)
    if not sys.stdin.isatty():
        raise unittest.SkipTest(
            "real journey needs an interactive terminal for hidden credential input"
        )

    try:
        with warnings.catch_warnings():
            # getpass otherwise falls back to echoing input when hidden input is
            # unavailable. Treat that fallback as a skip before reading input.
            warnings.simplefilter("error", getpass.GetPassWarning)
            credential = getpass.getpass("Pimcamp live-test client credential: ")
    except getpass.GetPassWarning:
        raise unittest.SkipTest(
            "terminal cannot accept the client credential without echo"
        ) from None

    if not credential:
        raise unittest.SkipTest("no client credential was entered")
    return credential


class RealAdapterJourney(unittest.TestCase):
    def test_real_single_account_journey(self) -> None:
        # Do not let a legacy secret variable reach version probes or child tools.
        os.environ.pop("PIMCAMP_LIVE_CREDENTIAL", None)
        self.evidence: EvidenceRecorder | None = None
        self.tool_versions: dict[str, str] = {}
        self.tool_version_expectations: dict[str, str] = {}
        self.tool_version_digests: dict[str, str] = {}
        self.human_return_mode = os.environ.get("PIMCAMP_LIVE_HUMAN_RETURN") == "1"
        required = {
            "PIMCAMP_LIVE_CONFIG": os.environ.get("PIMCAMP_LIVE_CONFIG"),
            "PIMCAMP_LIVE_RECIPIENT": os.environ.get("PIMCAMP_LIVE_RECIPIENT"),
            "PIMCAMP_LIVE_HIMALAYA_VERSION": os.environ.get(
                "PIMCAMP_LIVE_HIMALAYA_VERSION"
            ),
            "PIMCAMP_LIVE_MIRADOR_VERSION": os.environ.get(
                "PIMCAMP_LIVE_MIRADOR_VERSION"
            ),
            EVIDENCE_ENV: os.environ.get(EVIDENCE_ENV),
        }
        missing = [name for name, value in required.items() if not value]
        if os.environ.get("PIMCAMP_LIVE_ENABLE") != "1" or missing:
            detail = ", ".join(missing) if missing else "explicit live enable"
            self.skipTest(
                "real Himalaya/Mirador journey not enabled; missing "
                f"{detail}; no lower-adapter fixture is substituted"
            )

        config_path = Path(str(required["PIMCAMP_LIVE_CONFIG"]))
        if not config_path.is_file():
            self.skipTest(
                "real Pimcamp configuration is absent; no lower-adapter fixture "
                "is substituted"
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        operations = config.get("operations_adapter", {})
        observation = config.get("observation_adapter", {})
        self.assertEqual("himalaya", operations.get("kind"))
        self.assertEqual("mirador", observation.get("kind"))
        self.recipient = str(required["PIMCAMP_LIVE_RECIPIENT"]).strip()
        selected_sender = operations.get("from", {})
        self.selected_sender = (
            str(selected_sender.get("address", "")).strip()
            if isinstance(selected_sender, dict)
            else ""
        )
        if self.human_return_mode:
            self.assertTrue(
                self.selected_sender,
                "human-return mode needs the selected account's configured sender",
            )
            self.assertTrue(
                self.recipient.casefold() != self.selected_sender.casefold(),
                "human-return recipient must differ from the selected account "
                "sender (values suppressed)",
            )
        self._prove_tool_version(
            str(operations.get("executable", "")),
            str(required["PIMCAMP_LIVE_HIMALAYA_VERSION"]),
            "Himalaya",
        )
        self._prove_tool_version(
            str(observation.get("executable", "")),
            str(required["PIMCAMP_LIVE_MIRADOR_VERSION"]),
            "Mirador/Carillon",
        )
        try:
            EvidenceRecorder.validate_destination(str(required[EVIDENCE_ENV]), ROOT)
        except (OSError, ValueError):
            self.skipTest(
                "private live-evidence destination must be a new file in an "
                "owner-only directory outside the repository"
            )
        self.source_commit = self._source_commit()
        wait_bound = config.get("adapter_wait_seconds")
        self.assertIsInstance(wait_bound, (int, float))
        self.assertGreater(wait_bound, 0)
        self.teardown_timeout = float(wait_bound) + 5

        self.credential = _prompt_for_client_credential()
        try:
            self.evidence = EvidenceRecorder.create(str(required[EVIDENCE_ENV]), ROOT)
        except (OSError, ValueError):
            self.skipTest("private live-evidence destination is unavailable")
        self.environment = os.environ.copy()
        self.environment["PIMCAMP_CONFIG"] = str(config_path)
        self.evidence.record(
            "journey_metadata",
            schema="pimcamp.live-evidence.v1",
            journey_command=[
                "python3",
                "-m",
                "unittest",
                "tests.test_live_adapters",
                "-v",
            ],
            source_commit=self.source_commit,
            integration_ref="origin/main",
            human_return_mode=self.human_return_mode,
            python_implementation=platform.python_implementation(),
            python_version=platform.python_version(),
            sqlite_version=sqlite3.sqlite_version,
        )
        for tool, version in sorted(self.tool_versions.items()):
            executable_name = "carillon" if tool == "Mirador/Carillon" else "himalaya"
            self.evidence.record(
                "tool_version",
                tool=tool,
                command=[f"<{executable_name}>", "--version"],
                version=version,
                expected_version=self.tool_version_expectations[tool],
                output_sha256=self.tool_version_digests[tool],
            )
        self.return_subject_token_matched = False
        self.return_body_token_matched = False
        self.return_sender_matches_recipient = False
        self.message_filed = False
        self.subject_absent_after_filing = False
        self.subscription_teardown = False
        deadline = time.monotonic() + EXTERNAL_BOUND_SECONDS
        subscription: subprocess.Popen[str] | None = None
        body_passed = False

        try:
            subscription = self._start_subscription()
            started = self._read_line(subscription, deadline)
            self.assertEqual(
                "pimcamp.v1",
                started.get("contract_version"),
                "real subscription returned an invalid contract (content suppressed)",
            )
            self.assertEqual(
                {"status": "started"},
                started.get("result"),
                "real subscription did not start (content suppressed)",
            )

            token = f"pimcamp-live-{uuid.uuid4()}"
            composed = self._success(
                self._invoke(
                    "compose",
                    {
                        "to": [{"name": None, "address": self.recipient}],
                        "cc": [],
                        "bcc": [],
                        "subject": token,
                        "body_text": token,
                    },
                    deadline,
                )
            )
            sent = self._success(
                self._invoke(
                    "send",
                    {"composition": composed, "mutation_id": str(uuid.uuid4())},
                    deadline,
                )
            )
            self.assertEqual("sent", sent.get("status"), "unique-token send failed")

            event = self._read_line(subscription, deadline)
            self.assertEqual(
                {"contract_version", "kind", "mailbox", "observed_at"},
                set(event),
                "real event had an invalid shape (content suppressed)",
            )
            self.assertEqual("new_mail", event["kind"], "unexpected event kind")
            self.assertEqual("inbox", event["mailbox"], "unexpected event mailbox")

            incoming = self._find_message(token, deadline)
            self.return_subject_token_matched = True
            message = self._success(
                self._invoke(
                    "read", {"message_ref": incoming["message_ref"]}, deadline
                )
            )
            self.return_body_token_matched = any(
                token in part["content_utf8"] for part in message["body_parts"]
            )
            self.assertTrue(self.return_body_token_matched)
            if self.human_return_mode:
                self.return_sender_matches_recipient = human_return_provenance_matches(
                    message,
                    self.recipient,
                    self.selected_sender,
                )
                self.assertTrue(
                    self.return_sender_matches_recipient,
                    "human return did not match the configured distinct sender "
                    "(values suppressed)",
                )
            assert self.evidence is not None
            self.evidence.record(
                "return_provenance",
                return_token_in_subject=self.return_subject_token_matched,
                return_token_in_body=self.return_body_token_matched,
                configured_recipient_source=(
                    "PIMCAMP_LIVE_RECIPIENT" if self.human_return_mode else None
                ),
                selected_sender_source=(
                    "operations_adapter.from.address"
                    if self.human_return_mode
                    else None
                ),
                observed_sender_source=(
                    "read-result.from" if self.human_return_mode else None
                ),
                selected_account_placeholder=("A" if self.human_return_mode else None),
                configured_recipient_placeholder=("B" if self.human_return_mode else None),
                observed_return_sender_placeholder=(
                    "B" if self.return_sender_matches_recipient else None
                ),
                addresses_redacted=True,
                configured_recipient_differs_from_selected_sender=(
                    self.recipient.casefold() != self.selected_sender.casefold()
                    if self.human_return_mode
                    else None
                ),
                return_sender_matches_configured_recipient=(
                    self.return_sender_matches_recipient
                    if self.human_return_mode
                    else None
                ),
            )

            reply = self._success(
                self._invoke(
                    "reply",
                    {
                        "message_ref": incoming["message_ref"],
                        "body_text": f"reply-{token}",
                    },
                    deadline,
                )
            )
            reply_sent = self._success(
                self._invoke(
                    "send",
                    {"composition": reply, "mutation_id": str(uuid.uuid4())},
                    deadline,
                )
            )
            self.assertEqual("sent", reply_sent.get("status"), "reply send failed")

            filed = self._success(
                self._invoke(
                    "junk",
                    {
                        "message_ref": incoming["message_ref"],
                        "mutation_id": str(uuid.uuid4()),
                    },
                    deadline,
                )
            )
            self.assertEqual("filed", filed.get("status"), "matching message was not filed")
            self.assertIn(
                filed.get("mechanism"),
                {"report_spam", "move_to_junk"},
                "matching message used an unsupported junk mechanism",
            )
            self.junk_mechanism = filed["mechanism"]
            self.message_filed = True

            remaining = self._all_summaries(deadline)
            self.assertFalse(any(token in item["subject"] for item in remaining))
            self.subject_absent_after_filing = True
            body_passed = True
        finally:
            cleanup_passed = False
            try:
                if subscription is not None:
                    if subscription.poll() is None:
                        subscription.send_signal(signal.SIGTERM)
                    try:
                        stdout, _stderr = subscription.communicate(
                            timeout=self.teardown_timeout
                        )
                    except subprocess.TimeoutExpired:
                        subscription.kill()
                        try:
                            subscription.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        raise AssertionError(
                            "real subscription did not honor bounded Pimcamp teardown "
                            "(output suppressed)"
                        ) from None
                    for line in stdout.splitlines():
                        event = json.loads(line)
                        self.assertEqual(
                            {"contract_version", "kind", "mailbox", "observed_at"},
                            set(event),
                            "real event had an invalid shape (content suppressed)",
                        )
                    self.assertEqual(
                        0,
                        subscription.returncode,
                        "real subscription failed (output suppressed)",
                    )
                    assert self.evidence is not None
                    self.evidence.record(
                        "client_call_end",
                        operation="subscribe_new_mail",
                        outcome="stopped",
                    )
                    self.subscription_teardown = True
                cleanup_passed = True
            finally:
                if self.evidence is not None:
                    self.evidence.finish(
                        "passed" if body_passed and cleanup_passed else "failed",
                        checks={
                            "return_token_in_subject": self.return_subject_token_matched,
                            "return_token_in_body": self.return_body_token_matched,
                            "configured_distinct_sender": (
                                self.return_sender_matches_recipient
                                if self.human_return_mode
                                else None
                            ),
                            "matching_message_filed": self.message_filed,
                            "matching_subject_absent_after_filing": (
                                self.subject_absent_after_filing
                            ),
                            "bounded_subscription_teardown": self.subscription_teardown,
                        },
                        junk_mechanism=str(getattr(self, "junk_mechanism", "unobserved")),
                    )

    def _prove_tool_version(
        self, executable: str, expected: str, label: str
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            self.skipTest(
                f"real {label} executable is absent; no lower-adapter fixture "
                "is substituted"
            )
        completed = subprocess.run(
            [resolved, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            env=self._process_environment(),
        )
        self.assertEqual(0, completed.returncode, f"{label} --version failed")
        version_output = completed.stdout + completed.stderr
        self.assertTrue(
            expected in version_output,
            f"{label} version did not match the configured exact version",
        )
        try:
            self.tool_versions[label] = observed_version(expected, version_output)
        except ValueError:
            self.fail(f"{label} version output did not identify an exact version token")
        self.tool_version_expectations[label] = expected
        self.tool_version_digests[label] = hashlib.sha256(
            version_output.encode("utf-8")
        ).hexdigest()

    def _start_subscription(self) -> subprocess.Popen[str]:
        assert self.evidence is not None
        self.evidence.record("client_call_start", operation="subscribe_new_mail")
        process = subprocess.Popen(
            [str(EXECUTABLE), "subscribe_new_mail"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._child_environment(),
        )
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "contract_version": "pimcamp.v1",
                    "client_credential": self.credential,
                    "input": {},
                }
            )
        )
        process.stdin.close()
        process.stdin = None
        return process

    def _invoke(
        self, operation: str, input_value: dict[str, object], deadline: float
    ) -> dict[str, object]:
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            evidence.record("client_call_start", operation=operation)
        remaining = deadline - time.monotonic()
        self.assertGreater(remaining, 0, "real journey exceeded its external bound")
        try:
            completed = subprocess.run(
                [str(EXECUTABLE), operation],
                input=json.dumps(
                    {
                        "contract_version": "pimcamp.v1",
                        "client_credential": self.credential,
                        "input": input_value,
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._child_environment(),
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            if evidence is not None:
                evidence.record(
                    "client_call_end", operation=operation, outcome="timed_out"
                )
            raise AssertionError(
                "Pimcamp request timed out (output suppressed)"
            ) from None
        try:
            self.assertEqual(
                1,
                len(completed.stdout.splitlines()),
                "Pimcamp emitted an unexpected response count (output suppressed)",
            )
            value = json.loads(completed.stdout)
            self.assertEqual(
                0, completed.returncode, "Pimcamp request failed (output suppressed)"
            )
        except (AssertionError, json.JSONDecodeError):
            if evidence is not None:
                evidence.record(
                    "client_call_end", operation=operation, outcome="failed"
                )
            raise
        if evidence is not None:
            evidence.record("client_call_end", operation=operation, outcome="success")
        return value

    def _read_line(
        self, process: subprocess.Popen[str], deadline: float
    ) -> dict[str, object]:
        assert process.stdout is not None
        remaining = deadline - time.monotonic()
        self.assertGreater(remaining, 0, "real journey exceeded its external bound")
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(
                ready.select(remaining),
                "real observation emitted no event before the external bound",
            )
        line = process.stdout.readline()
        self.assertTrue(line, "real observation process stopped before emitting a line")
        value = json.loads(line)
        self.assertIsInstance(value, dict)
        if self.evidence is not None:
            result = value.get("result")
            kind = value.get("kind")
            self.evidence.record(
                "subscription_output",
                output_kind=(
                    "started"
                    if isinstance(result, dict) and result.get("status") == "started"
                    else "new_mail"
                    if kind == "new_mail"
                    else "other"
                ),
                mailbox="inbox" if value.get("mailbox") == "inbox" else None,
            )
        return value

    def _find_message(
        self, token: str, deadline: float
    ) -> dict[str, object]:
        while time.monotonic() < deadline:
            summaries = self._all_summaries(deadline)
            found = next(
                (item for item in summaries if token in item["subject"]), None
            )
            if found is not None:
                return found
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        self.fail("the event arrived but authoritative list never returned the live token")

    def _all_summaries(self, deadline: float) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        cursor: object = None
        while True:
            request: dict[str, object] = {"limit": 100}
            if cursor is not None:
                request["cursor"] = cursor
            result = self._success(self._invoke("list", request, deadline))
            summaries.extend(result["messages"])
            cursor = result["next_cursor"]
            if cursor is None:
                return summaries

    def _success(self, envelope: dict[str, object]) -> dict[str, object]:
        self.assertEqual(
            "pimcamp.v1",
            envelope.get("contract_version"),
            "Pimcamp returned an invalid contract (content suppressed)",
        )
        result = envelope.get("result")
        self.assertIsInstance(
            result, dict, "Pimcamp returned an invalid result (content suppressed)"
        )
        return result

    def _child_environment(self) -> dict[str, str]:
        environment = self.environment.copy()
        for name in (
            "PIMCAMP_LIVE_CREDENTIAL",
            "PIMCAMP_LIVE_ENABLE",
            "PIMCAMP_LIVE_CONFIG",
            "PIMCAMP_LIVE_RECIPIENT",
            "PIMCAMP_LIVE_HIMALAYA_VERSION",
            "PIMCAMP_LIVE_MIRADOR_VERSION",
            "PIMCAMP_LIVE_HUMAN_RETURN",
        ):
            environment.pop(name, None)
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            environment[EVIDENCE_ENV] = str(evidence.path)
            python_path = environment.get("PYTHONPATH", "")
            entries = [item for item in python_path.split(os.pathsep) if item]
            if SUPPORT_PATH not in entries:
                entries.insert(0, SUPPORT_PATH)
            environment["PYTHONPATH"] = os.pathsep.join(entries)
        return environment

    def _process_environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PIMCAMP_")
        }

    def _source_commit(self) -> str:
        environment = self._process_environment()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, commit.returncode, "live source revision is unavailable")
        value = commit.stdout.strip()
        self.assertRegex(value, _COMMIT, "live source revision is invalid")
        integrated = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        self.assertEqual(
            0,
            integrated.returncode,
            "live source revision is not integrated on main",
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, status.returncode, "live source status is unavailable")
        self.assertEqual("", status.stdout.strip(), "live source tree must be clean")
        return value


if __name__ == "__main__":
    unittest.main()
