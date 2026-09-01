"""Separate-process acceptance checks for the public stdio boundary."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "pimcamp"
PORT_ADAPTER = ROOT / "tests" / "support" / "operations_adapter.py"
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
        self.credential = "pimcamp-test-client-secret"
        self.write_adapter_state([message("lower-id-1", "Original body")])
        self.write_config(ALL_GRANTS)

    def write_config(self, grants: list[str]) -> None:
        digest = hashlib.sha256(self.credential.encode()).hexdigest()
        self.state_path = self.root / "state" / "pimcamp.sqlite3"
        config = {
            "credentials": {
                digest: {"client_identity": "boundary-test", "grants": grants}
            },
            "adapter_wait_seconds": 2,
            "state_path": str(self.state_path),
            "operations_adapter": {
                "kind": "command",
                "command": [sys.executable, str(PORT_ADAPTER), str(self.adapter_state), str(self.calls)],
            },
            "observation_adapter": {
                "kind": "command",
                "command": [sys.executable, str(PORT_ADAPTER), str(self.adapter_state), str(self.calls)],
            },
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(config))
        os.chmod(self.config_path, 0o600)

    def write_adapter_state(self, messages: list[dict[str, object]]) -> None:
        self.adapter_state.write_text(json.dumps({"messages": messages}))

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
