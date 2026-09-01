"""Separate-process acceptance checks for the public stdio boundary."""

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "pimcamp"
PORT_ADAPTER = ROOT / "tests" / "support" / "operations_adapter.py"
OBSERVATION_ADAPTER = ROOT / "tests" / "support" / "observation_adapter.py"
ALL_GRANTS = ["list", "read", "compose", "reply", "send", "junk", "subscribe_new_mail"]


class BoundaryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.adapter_state = self.root / "adapter.json"
        self.calls = self.root / "calls.jsonl"
        self.calls.write_text("")
        self.observation_event = self.root / "observation-event"
        self.credential = "pimcamp-test-client-secret"
        self.write_adapter_state([message("lower-id-1", "Original body")])
        self.write_config(ALL_GRANTS)

    def write_config(self, grants: list[str], wait_seconds: float = 2) -> None:
        digest = hashlib.sha256(self.credential.encode()).hexdigest()
        self.state_path = self.root / "state" / "pimcamp.sqlite3"
        config = {
            "credentials": {
                digest: {"client_identity": "boundary-test", "grants": grants}
            },
            "adapter_wait_seconds": wait_seconds,
            "state_path": str(self.state_path),
            "operations_adapter": {
                "kind": "command",
                "command": [sys.executable, str(PORT_ADAPTER), str(self.adapter_state), str(self.calls)],
            },
            "observation_adapter": {
                "kind": "command",
                "command": [
                    sys.executable,
                    str(OBSERVATION_ADAPTER),
                    str(self.adapter_state),
                    str(self.calls),
                ],
            },
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(config))
        os.chmod(self.config_path, 0o600)

    def write_adapter_state(self, messages: list[dict[str, object]]) -> None:
        self.adapter_state.write_text(
            json.dumps(
                {
                    "messages": messages,
                    "observation_event_file": str(self.observation_event),
                    "private_sentinel": "PRIVATE-OBSERVATION-SENTINEL",
                    "private_operations_sentinel": "PRIVATE-OPERATIONS-SENTINEL",
                }
            )
        )

    def update_adapter_state(self, **values: object) -> None:
        state = json.loads(self.adapter_state.read_text())
        state.update(values)
        self.adapter_state.write_text(json.dumps(state))

    def calls_for(self, operation: str) -> list[dict[str, object]]:
        return [
            value
            for line in self.calls.read_text().splitlines()
            if (value := json.loads(line))["operation"] == operation
        ]

    def invoke(self, operation: str, input_value: object, credential: str | None = None) -> subprocess.CompletedProcess[str]:
        envelope = {"contract_version": "pimcamp.v1", "input": input_value}
        envelope["client_credential"] = self.credential if credential is None else credential
        return self.invoke_raw(operation, json.dumps(envelope))

    def invoke_without_credential(self, operation: str, input_value: object) -> subprocess.CompletedProcess[str]:
        envelope = {"contract_version": "pimcamp.v1", "input": input_value}
        return self.invoke_raw(operation, json.dumps(envelope))

    def invoke_raw(self, operation: str, raw: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PIMCAMP_CONFIG"] = str(self.config_path)
        return subprocess.run(
            [str(EXECUTABLE), operation],
            input=raw,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=5,
        )

    def start(self, operation: str, input_value: object) -> subprocess.Popen[str]:
        environment = os.environ.copy()
        environment["PIMCAMP_CONFIG"] = str(self.config_path)
        envelope = {
            "contract_version": "pimcamp.v1",
            "client_credential": self.credential,
            "input": input_value,
        }
        process = subprocess.Popen(
            [str(EXECUTABLE), operation],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(envelope))
        process.stdin.close()
        process.stdin = None
        return process

    def wait_for_calls(self, operation: str, count: int) -> None:
        deadline = time.monotonic() + 3
        while len(self.calls_for(operation)) < count:
            if time.monotonic() >= deadline:
                self.fail(f"adapter never recorded {count} {operation} calls")
            time.sleep(0.005)

    def read_process_line(
        self, process: subprocess.Popen[str], timeout: float = 3
    ) -> str:
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(timeout), "process did not emit a line")
        line = process.stdout.readline()
        self.assertTrue(line, "process closed stdout before emitting a line")
        return line

    def value(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(1, len(result.stdout.splitlines()), result.stdout)
        return json.loads(result.stdout)

    def call_count(self) -> int:
        return len(self.calls.read_text().splitlines())

    def test_closed_json_and_unknown_command_are_invalid_before_adapter_io(self) -> None:
        unknown_field = self.invoke("list", {"limit": 1, "unexpected": True})
        self.assertEqual(1, unknown_field.returncode)
        self.assertEqual("invalid_request", self.value(unknown_field)["code"])

        duplicate = self.invoke_raw(
            "junk",
            '{"contract_version":"pimcamp.v1","client_credential":"pimcamp-test-client-secret",'
            '"input":{"message_ref":"M","mutation_id":"550e8400-e29b-41d4-a716-446655440000",'
            '"mutation_id":"550e8400-e29b-41d4-a716-446655440000"}}',
        )
        self.assertEqual("invalid_request", self.value(duplicate)["code"])

        second = self.invoke_raw(
            "compose",
            json.dumps(
                {
                    "contract_version": "pimcamp.v1",
                    "client_credential": self.credential,
                    "input": composition_input(),
                }
            )
            + " {}",
        )
        self.assertEqual("invalid_request", self.value(second)["code"])

        environment = os.environ.copy()
        environment["PIMCAMP_CONFIG"] = str(self.config_path)
        unknown = subprocess.run(
            [str(EXECUTABLE), "eighth_operation"],
            input="",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual("invalid_request", self.value(unknown)["code"])
        self.assertEqual(0, self.call_count())

    def test_permission_denial_precedes_adapter_and_state_io(self) -> None:
        self.write_config(["list"])
        inputs = {
            "read": {"message_ref": "M"},
            "compose": composition_input(),
            "reply": {"message_ref": "M", "body_text": "Acknowledged"},
            "send": {"composition": composition_value(), "mutation_id": "550e8400-e29b-41d4-a716-446655440000"},
            "junk": {"message_ref": "M", "mutation_id": "550e8400-e29b-41d4-a716-446655440001"},
            "subscribe_new_mail": {},
        }
        for operation, input_value in inputs.items():
            with self.subTest(operation=operation):
                result = self.invoke(operation, input_value)
                self.assertEqual(1, result.returncode)
                self.assertEqual("permission_denied", self.value(result)["code"])
        unknown = self.invoke("list", {"limit": 1}, credential="not-the-credential")
        missing = self.invoke_without_credential("list", {"limit": 1})
        self.assertEqual("permission_denied", self.value(unknown)["code"])
        self.assertEqual("permission_denied", self.value(missing)["code"])
        self.assertEqual(0, self.call_count())
        self.assertFalse(self.state_path.exists())

    def test_list_pages_without_exposing_adapter_ids(self) -> None:
        messages = [message(f"lower-{index}", f"Body {index}") for index in range(101)]
        self.write_adapter_state(messages)
        first = self.invoke("list", {"limit": 100})
        self.assertEqual(0, first.returncode, first.stderr)
        first_result = self.value(first)["result"]
        self.assertEqual(100, len(first_result["messages"]))
        self.assertIsInstance(first_result["next_cursor"], str)
        self.assertNotEqual("page:100", first_result["next_cursor"])
        self.assertNotIn("adapter_id", json.dumps(first_result))
        self.assertNotIn("lower-0", json.dumps(first_result))
        second = self.invoke("list", {"limit": 100, "cursor": first_result["next_cursor"]})
        second_result = self.value(second)["result"]
        self.assertEqual(1, len(second_result["messages"]))
        self.assertIsNone(second_result["next_cursor"])

    def test_read_queries_current_adapter_truth(self) -> None:
        listed = self.value(self.invoke("list", {"limit": 1}))["result"]
        message_ref = listed["messages"][0]["message_ref"]
        changed = message("lower-id-1", "Changed body")
        self.write_adapter_state([changed])
        read = self.invoke("read", {"message_ref": message_ref})
        self.assertEqual("Changed body", self.value(read)["result"]["body_parts"][0]["content_utf8"])
        self.write_adapter_state([])
        absent = self.invoke("read", {"message_ref": message_ref})
        self.assertEqual("not_found", self.value(absent)["code"])

    def test_compose_is_pure_and_rejects_empty_recipients(self) -> None:
        composed = self.invoke("compose", composition_input())
        self.assertEqual(0, composed.returncode, composed.stderr)
        self.assertEqual(composition_value(), self.value(composed)["result"])
        empty = composition_input()
        empty["to"] = []
        empty["cc"] = []
        empty["bcc"] = []
        rejected = self.invoke("compose", empty)
        self.assertEqual("invalid_request", self.value(rejected)["code"])
        self.assertEqual(0, self.call_count())
        self.assertFalse(self.state_path.exists())

    def test_reply_uses_current_source_without_read_grant(self) -> None:
        listed = self.value(self.invoke("list", {"limit": 1}))["result"]
        message_ref = listed["messages"][0]["message_ref"]
        self.write_config(["reply"])
        reply = self.invoke("reply", {"message_ref": message_ref, "body_text": "Acknowledged"})
        self.assertEqual(0, reply.returncode, reply.stderr)
        result = self.value(reply)["result"]
        self.assertEqual([{"name": None, "address": "reply@example.test"}], result["to"])
        self.assertEqual("Re: Status", result["subject"])
        self.assertEqual(message_ref, result["reply_to_message_ref"])
        self.write_adapter_state([message("lower-id-1", "Body", subject="re: Status")])
        again = self.invoke("reply", {"message_ref": message_ref, "body_text": "Again"})
        self.assertEqual("re: Status", self.value(again)["result"]["subject"])

    def test_send_replays_one_result_and_rejects_a_changed_request(self) -> None:
        input_value = send_input()
        first = self.invoke("send", input_value)
        second = self.invoke("send", input_value)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(self.value(first), self.value(second))
        self.assertEqual(1, len(self.calls_for("send")))

        changed = send_input()
        changed["composition"]["body_text"] = "Changed"
        conflict = self.invoke("send", changed)
        self.assertEqual("conflict", self.value(conflict)["code"])
        self.assertEqual(1, len(self.calls_for("send")))

    def test_concurrent_send_has_one_claim_and_canonical_request_equality(self) -> None:
        pause_file = self.root / "release-send"
        self.update_adapter_state(
            behavior={"send": "pause_success"}, pause_file=str(pause_file)
        )
        first_input = send_input()
        second_input = {
            "mutation_id": first_input["mutation_id"],
            "composition": dict(reversed(list(first_input["composition"].items()))),
        }
        first = self.start("send", first_input)
        self.wait_for_calls("send", 1)
        second = self.start("send", second_input)
        pause_file.touch()
        first_stdout, first_stderr = first.communicate(timeout=5)
        second_stdout, second_stderr = second.communicate(timeout=5)
        self.assertEqual(0, first.returncode, first_stderr)
        self.assertEqual(0, second.returncode, second_stderr)
        self.assertEqual(json.loads(first_stdout), json.loads(second_stdout))
        self.assertEqual(1, len(self.calls_for("send")))

    def test_reply_send_preserves_threading_and_lost_response_is_immutable(self) -> None:
        listed = self.value(self.invoke("list", {"limit": 1}))["result"]
        message_ref = listed["messages"][0]["message_ref"]
        reply = self.value(
            self.invoke("reply", {"message_ref": message_ref, "body_text": "Reply"})
        )["result"]
        self.update_adapter_state(behavior={"send": "lost_response"})
        input_value = {
            "composition": reply,
            "mutation_id": "550e8400-e29b-41d4-a716-446655440010",
        }
        first = self.invoke("send", input_value)
        second = self.invoke("send", input_value)
        self.assertEqual("outcome_unknown", self.value(first)["code"])
        self.assertEqual(self.value(first), self.value(second))
        calls = self.calls_for("send")
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0]["threading_present"])

    def test_forced_claimant_death_never_submits_twice(self) -> None:
        pause_file = self.root / "never-release"
        self.update_adapter_state(
            behavior={"send": "crash_pause"}, pause_file=str(pause_file)
        )
        input_value = send_input("550e8400-e29b-41d4-a716-446655440011")
        claimant = self.start("send", input_value)
        self.wait_for_calls("send", 1)
        with sqlite3.connect(self.state_path) as connection:
            began = connection.execute(
                "SELECT mutation_call_began FROM mutation_receipts WHERE mutation_id = ?",
                (input_value["mutation_id"],),
            ).fetchone()
        self.assertEqual((1,), began)
        claimant.kill()
        claimant.communicate(timeout=5)
        repeated = self.invoke("send", input_value)
        self.assertTrue(repeated.stdout, repeated.stderr)
        self.assertEqual(
            "outcome_unknown",
            self.value(repeated)["code"],
            repeated.stdout + repeated.stderr,
        )
        self.assertEqual(1, len(self.calls_for("send")))

    def test_junk_prefers_spam_reporting_and_replays_without_a_second_call(self) -> None:
        listed = self.value(self.invoke("list", {"limit": 1}))["result"]
        message_ref = listed["messages"][0]["message_ref"]
        self.update_adapter_state(
            junk_capabilities={"report_spam": True, "move_to_junk": True}
        )
        input_value = {
            "message_ref": message_ref,
            "mutation_id": "550e8400-e29b-41d4-a716-446655440020",
        }
        first = self.invoke("junk", input_value)
        second = self.invoke("junk", input_value)
        result = self.value(first)["result"]
        self.assertEqual("report_spam", result["mechanism"])
        self.assertEqual(self.value(first), self.value(second))
        self.assertEqual(1, len(self.calls_for("junk_capabilities")))
        self.assertEqual(1, len(self.calls_for("report_spam")))
        self.assertEqual(0, len(self.calls_for("move_to_junk")))
        self.assertEqual([], json.loads(self.adapter_state.read_text())["messages"])

    def test_junk_fallback_unsupported_and_ambiguity_are_bounded(self) -> None:
        listed = self.value(self.invoke("list", {"limit": 1}))["result"]
        message_ref = listed["messages"][0]["message_ref"]
        self.update_adapter_state(
            junk_capabilities={"report_spam": False, "move_to_junk": True}
        )
        moved = self.invoke(
            "junk",
            {
                "message_ref": message_ref,
                "mutation_id": "550e8400-e29b-41d4-a716-446655440021",
            },
        )
        self.assertEqual("move_to_junk", self.value(moved)["result"]["mechanism"])

        self.write_adapter_state([message("lower-id-1", "Body")])
        self.update_adapter_state(
            junk_capabilities={"report_spam": False, "move_to_junk": False}
        )
        unsupported_input = {
            "message_ref": message_ref,
            "mutation_id": "550e8400-e29b-41d4-a716-446655440022",
        }
        unsupported = self.invoke("junk", unsupported_input)
        self.assertEqual("unsupported", self.value(unsupported)["code"])

        self.update_adapter_state(
            junk_capabilities={"report_spam": False, "move_to_junk": True},
            behavior={"move_to_junk": "lost_response"},
        )
        unknown_input = {
            "message_ref": message_ref,
            "mutation_id": "550e8400-e29b-41d4-a716-446655440023",
        }
        unknown = self.invoke("junk", unknown_input)
        repeated = self.invoke("junk", unknown_input)
        self.assertEqual("outcome_unknown", self.value(unknown)["code"])
        self.assertEqual(self.value(unknown), self.value(repeated))
        self.assertEqual(2, len(self.calls_for("move_to_junk")))

    def test_hung_mutation_becomes_unknown_and_cannot_retry(self) -> None:
        self.write_config(ALL_GRANTS, wait_seconds=0.15)
        self.update_adapter_state(behavior={"send": "hang"})
        input_value = send_input("550e8400-e29b-41d4-a716-446655440030")
        first = self.invoke("send", input_value)
        second = self.invoke("send", input_value)
        self.assertEqual("outcome_unknown", self.value(first)["code"])
        self.assertEqual(self.value(first), self.value(second))
        self.assertEqual(1, len(self.calls_for("send")))

    def test_finite_query_timeout_and_cleanup_preserve_decidable_results(self) -> None:
        self.write_config(ALL_GRANTS, wait_seconds=0.15)
        self.update_adapter_state(behavior={"list": "hang"})
        timed_out = self.invoke("list", {"limit": 1})
        self.assertEqual("backend_unavailable", self.value(timed_out)["code"])

        self.update_adapter_state(behavior={"list": "cleanup_hang"})
        proved = self.invoke("list", {"limit": 1})
        self.assertEqual(0, proved.returncode, proved.stderr)
        self.assertEqual(1, len(self.value(proved)["result"]["messages"]))
        diagnostics = [json.loads(line) for line in proved.stderr.splitlines()]
        self.assertTrue(any(item.get("forced_termination") for item in diagnostics))

    def test_subscription_normalizes_signal_before_authoritative_query(self) -> None:
        subscription = self.start("subscribe_new_mail", {})
        ready = json.loads(self.read_process_line(subscription))
        self.assertEqual({"status": "subscribed"}, ready["result"])
        self.assertEqual(1, len(self.calls_for("observation_open")))
        self.assertEqual(0, len(self.calls_for("list")))

        self.observation_event.touch()
        event = json.loads(self.read_process_line(subscription))
        self.assertEqual(
            {"contract_version", "kind", "mailbox", "observed_at"}, set(event)
        )
        self.assertEqual("pimcamp.v1", event["contract_version"])
        self.assertEqual("new_mail", event["kind"])
        self.assertEqual("inbox", event["mailbox"])
        self.assertTrue(event["observed_at"].endswith("Z"))
        self.assertNotIn("PRIVATE-OBSERVATION-SENTINEL", json.dumps(event))
        self.assertEqual(0, len(self.calls_for("list")))

        listed = self.invoke("list", {"limit": 1})
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual(1, len(self.calls_for("list")))
        subscription.send_signal(signal.SIGTERM)
        remainder, diagnostics = subscription.communicate(timeout=5)
        self.assertEqual(0, subscription.returncode, diagnostics)
        self.assertEqual("", remainder)
        self.assertEqual(1, len(self.calls_for("observation_close")))

    def test_subscription_failure_is_normalized_without_private_content(self) -> None:
        self.update_adapter_state(observation_behavior="malformed_event")
        subscription = self.start("subscribe_new_mail", {})
        self.read_process_line(subscription)
        self.observation_event.touch()
        failure = json.loads(self.read_process_line(subscription))
        stdout, diagnostics = subscription.communicate(timeout=5)
        self.assertEqual(1, subscription.returncode)
        self.assertEqual("backend_unavailable", failure["code"])
        evidence = json.dumps(failure) + stdout + diagnostics
        self.assertNotIn("PRIVATE-OBSERVATION-SENTINEL", evidence)
        self.assertEqual(1, len(self.calls_for("observation_close")))

    def test_subscription_open_and_close_are_bounded(self) -> None:
        self.write_config(ALL_GRANTS, wait_seconds=0.15)
        self.update_adapter_state(observation_behavior="open_hang")
        timed_out = self.invoke("subscribe_new_mail", {})
        self.assertEqual(1, timed_out.returncode)
        self.assertEqual("backend_unavailable", self.value(timed_out)["code"])
        diagnostics = [json.loads(line) for line in timed_out.stderr.splitlines()]
        self.assertTrue(any(item.get("forced_termination") for item in diagnostics))

        self.update_adapter_state(observation_behavior="close_hang")
        subscription = self.start("subscribe_new_mail", {})
        self.read_process_line(subscription)
        subscription.send_signal(signal.SIGTERM)
        stdout, stderr = subscription.communicate(timeout=5)
        self.assertEqual(0, subscription.returncode, stderr)
        self.assertEqual("", stdout)
        diagnostics = [json.loads(line) for line in stderr.splitlines()]
        self.assertTrue(any(item.get("forced_termination") for item in diagnostics))
        self.assertEqual(1, len(self.calls_for("observation_close")))

    def test_diagnostics_keep_content_out_and_retain_safe_evidence(self) -> None:
        address_sentinel = "address-sentinel@example.test"
        subject_sentinel = "SUBJECT-SENTINEL-713"
        body_sentinel = "BODY-SENTINEL-829"
        credential_sentinel = "CREDENTIAL-SENTINEL-947"
        operations_sentinel = "PRIVATE-OPERATIONS-SENTINEL-263"
        observation_sentinel = "PRIVATE-OBSERVATION-SENTINEL-419"
        self.credential = credential_sentinel
        self.write_config(ALL_GRANTS)
        private_message = message("lower-id-1", body_sentinel, subject_sentinel)
        private_message["from"] = [{"name": None, "address": address_sentinel}]
        self.write_adapter_state([private_message])
        self.update_adapter_state(
            private_operations_sentinel=operations_sentinel,
            private_sentinel=observation_sentinel,
        )

        successful_query = self.invoke("list", {"limit": 1})
        self.assertEqual(0, successful_query.returncode, successful_query.stderr)

        self.update_adapter_state(behavior={"list": "private_violation"})
        failed_query = self.invoke("list", {"limit": 1})
        self.assertEqual("backend_unavailable", self.value(failed_query)["code"])

        self.update_adapter_state(behavior={"send": "success"})
        successful_input = send_input("550e8400-e29b-41d4-a716-446655440040")
        successful_input["composition"]["body_text"] = body_sentinel
        successful_mutation = self.invoke("send", successful_input)
        self.assertEqual(0, successful_mutation.returncode, successful_mutation.stderr)

        self.update_adapter_state(behavior={"send": "lost_response"})
        ambiguous_input = send_input("550e8400-e29b-41d4-a716-446655440041")
        ambiguous_input["composition"]["subject"] = subject_sentinel
        ambiguous_mutation = self.invoke("send", ambiguous_input)
        self.assertEqual("outcome_unknown", self.value(ambiguous_mutation)["code"])

        self.update_adapter_state(observation_behavior="malformed_event")
        subscription = self.start("subscribe_new_mail", {})
        self.read_process_line(subscription)
        self.observation_event.touch()
        subscription_error = self.read_process_line(subscription)
        subscription_stdout, subscription_stderr = subscription.communicate(timeout=5)
        self.assertEqual(1, subscription.returncode)

        evidence = "".join(
            (
                successful_query.stderr,
                failed_query.stdout,
                failed_query.stderr,
                successful_mutation.stderr,
                ambiguous_mutation.stdout,
                ambiguous_mutation.stderr,
                subscription_error,
                subscription_stdout,
                subscription_stderr,
            )
        )
        for sentinel in (
            address_sentinel,
            subject_sentinel,
            body_sentinel,
            credential_sentinel,
            operations_sentinel,
            observation_sentinel,
        ):
            self.assertNotIn(sentinel, evidence)

        diagnostics = [
            json.loads(line)
            for stderr in (
                successful_query.stderr,
                failed_query.stderr,
                successful_mutation.stderr,
                ambiguous_mutation.stderr,
                subscription_stderr,
            )
            for line in stderr.splitlines()
        ]
        self.assertTrue(
            any(
                item.get("operation") == "list"
                and item.get("result_code") == "success"
                and item.get("adapter_class") == "operations_command"
                for item in diagnostics
            )
        )
        self.assertTrue(
            any(
                item.get("operation") == "send"
                and item.get("result_code") == "outcome_unknown"
                and item.get("mutation_id") == ambiguous_input["mutation_id"]
                for item in diagnostics
            )
        )
        self.assertTrue(
            any(
                item.get("operation") == "subscribe_new_mail"
                and item.get("result_code") == "backend_unavailable"
                and item.get("adapter_class") == "observation_command"
                for item in diagnostics
            )
        )


def composition_input() -> dict[str, object]:
    return {
        "to": [{"name": "Recipient", "address": "recipient@example.test"}],
        "cc": [{"name": None, "address": "copy@example.test"}],
        "bcc": [],
        "subject": "Roadmap",
        "body_text": "Ready",
    }


def composition_value() -> dict[str, object]:
    return {"kind": "new", **composition_input(), "reply_to_message_ref": None}


def send_input(
    mutation_id: str = "550e8400-e29b-41d4-a716-446655440000",
) -> dict[str, object]:
    return {"composition": composition_value(), "mutation_id": mutation_id}


def message(adapter_id: str, body: str, subject: str = "Status") -> dict[str, object]:
    return {
        "adapter_id": adapter_id,
        "from": [{"name": "Author", "address": "author@example.test"}],
        "to": [{"name": None, "address": "recipient@example.test"}],
        "cc": [],
        "reply_to": [{"name": None, "address": "reply@example.test"}],
        "subject": subject,
        "received_at": "2026-09-01T20:00:00Z",
        "sent_at": "2026-09-01T19:59:00Z",
        "unread": True,
        "body_parts": [{"media_type": "text/plain", "content_utf8": body}],
        "threading": {"message_id": "thread-token", "references": []},
    }


if __name__ == "__main__":
    unittest.main()
