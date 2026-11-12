"""Synthetic, non-live checks for private evidence redaction and storage.

These fixtures exercise formatting helpers only; they are not lower-adapter output.
"""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from tests.support.live_evidence import (
    EvidenceRecorder,
    human_return_provenance_matches,
    observed_version,
    redact_command,
    redact_lower_output,
    validate_enter_elapsed,
)
from tests.support.live_timing import (
    ENTER_CLOCK_FD_ENV,
    JourneyWindow,
    read_enter_timestamp,
)


class LiveEvidenceTests(unittest.TestCase):
    def test_synthetic_enter_elapsed_requires_final_list_within_600_seconds(
        self,
    ) -> None:
        synthetic = [
            {
                "kind": "journey_complete",
                "elapsed_anchor": "single_enter",
                "elapsed_seconds_from_enter": 599.75,
                "final_list_assertion": True,
                "journey_window_seconds": 600,
            }
        ]

        summary = validate_enter_elapsed(synthetic)

        self.assertEqual("single_enter", summary["elapsed_anchor"])
        self.assertEqual(599.75, summary["elapsed_seconds_from_enter"])
        self.assertTrue(summary["final_list_assertion"])
        self.assertEqual(600, summary["journey_window_seconds"])
        for changes in (
            {"elapsed_anchor": "runner_start"},
            {"elapsed_seconds_from_enter": 600.01},
            {"final_list_assertion": False},
            {"journey_window_seconds": 120},
        ):
            invalid = [{**synthetic[0], **changes}]
            with self.subTest(changes=changes), self.assertRaises(AssertionError):
                validate_enter_elapsed(invalid)

    def test_synthetic_clock_pipe_preserves_the_enter_anchor_after_delay(self) -> None:
        entered_at = time.monotonic() - 5.5
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, f"{entered_at!r}\n".encode("ascii"))
        finally:
            os.close(write_fd)
        environment = {ENTER_CLOCK_FD_ENV: str(read_fd)}

        received = read_enter_timestamp(environment)
        window = JourneyWindow(received, 600)

        self.assertEqual(entered_at, received)
        self.assertGreater(window.elapsed(), 5)
        self.assertAlmostEqual(600 - window.elapsed(), window.remaining(), places=4)
        self.assertNotIn(ENTER_CLOCK_FD_ENV, environment)

    def test_human_return_requires_one_sender_from_distinct_recipient(self) -> None:
        selected = "selected@example.test"
        recipient = "recipient@example.test"
        returned = {"from": [{"name": "Responder", "address": recipient}]}
        wrong_sender = {"from": [{"name": "Selected", "address": selected}]}
        multiple_senders = {
            "from": [
                {"name": None, "address": recipient},
                {"name": None, "address": selected},
            ]
        }

        self.assertTrue(human_return_provenance_matches(returned, recipient, selected))
        self.assertFalse(
            human_return_provenance_matches(wrong_sender, recipient, selected)
        )
        self.assertFalse(human_return_provenance_matches(returned, selected, selected))
        self.assertFalse(
            human_return_provenance_matches(multiple_senders, recipient, selected)
        )

    def test_command_redaction_keeps_lower_syntax_and_hides_values(self) -> None:
        command = [
            "/private/bin/himalaya",
            "--json",
            "--account",
            "selected-account-sentinel",
            "smtp",
            "send",
            "--mail-from",
            "sender-sentinel@example.test",
            "--rcpt-to",
            "recipient-sentinel@example.test",
        ]

        redacted = redact_command(command, "himalaya")

        self.assertIn("--json", redacted)
        self.assertIn("smtp", redacted)
        self.assertIn("send", redacted)
        for private_value in command:
            if "sentinel" in private_value or private_value.startswith("/"):
                self.assertNotIn(private_value, redacted)

    def test_numeric_message_id_is_redacted_and_pagination_is_preserved(self) -> None:
        read = redact_command(
            [
                "/private/bin/himalaya",
                "--json",
                "--account",
                "selected-account",
                "message",
                "read",
                "764321",
                "--mailbox",
                "INBOX",
            ],
            "himalaya",
        )
        listing = redact_command(
            [
                "/private/bin/himalaya",
                "--json",
                "envelope",
                "list",
                "--page",
                "4",
                "--page-size",
                "100",
            ],
            "himalaya",
        )

        self.assertNotIn("764321", read)
        self.assertIn("--page", listing)
        self.assertIn("4", listing)
        self.assertIn("--page-size", listing)
        self.assertIn("100", listing)

    def test_observed_version_is_extracted_from_synthetic_tool_output(self) -> None:
        self.assertEqual(
            "v1.2.3",
            observed_version("1.2.3", "Himalaya v1.2.3 (synthetic output)\n"),
        )
        with self.assertRaises(ValueError):
            observed_version("1.2.3", "Himalaya version unavailable\n")

    def test_synthetic_rfc_content_is_removed_before_capture(self) -> None:
        synthetic = (
            "From: sender-sentinel@example.test\r\n"
            "To: recipient-sentinel@example.test\r\n"
            "Subject: subject-sentinel\r\n"
            "Message-ID: <message-sentinel@example.test>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: 8bit\r\n"
            " X-Folded-Sentinel\r\n"
            "\r\n"
            "body-sentinel\r\n"
        )
        output = json.dumps({"message": synthetic})

        redacted = redact_lower_output("himalaya", output)

        self.assertIn('"message":"From: <redacted>', redacted)
        self.assertIn("Content-Type: text/plain", redacted)
        for private_value in (
            "sender-sentinel",
            "recipient-sentinel",
            "subject-sentinel",
            "message-sentinel",
            "body-sentinel",
            "X-Folded-Sentinel",
        ):
            self.assertNotIn(private_value, redacted)

    def test_synthetic_lower_json_keeps_keys_and_redacts_values(self) -> None:
        output = json.dumps(
            {
                "envelopes": [
                    {
                        "id": 764321,
                        "subject": "subject-sentinel",
                        "from": [
                            {
                                "name": "name-sentinel",
                                "address": "address-sentinel",
                            }
                        ],
                    }
                ]
            }
        )

        redacted = redact_lower_output("himalaya", output)
        parsed = json.loads(redacted)

        self.assertEqual({"envelopes"}, set(parsed))
        self.assertEqual({"id", "subject", "from"}, set(parsed["envelopes"][0]))
        self.assertEqual("<redacted>", parsed["envelopes"][0]["id"])
        self.assertNotIn("sentinel", redacted)

    def test_evidence_file_is_new_private_and_summarizes_call_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            path = directory / "journey.jsonl"
            recorder = EvidenceRecorder.create(
                str(path), Path(__file__).resolve().parents[1]
            )
            recorder.record("client_call_start", operation="read")
            summary = recorder.finish(
                "failed",
                checks={"return_sender_matches_recipient": None},
                junk_mechanism="unobserved",
            )

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(["read"], summary["client_call_order"])
            self.assertFalse(summary["automatic_responder_behavior_proved"])
            self.assertFalse(summary["unattended_repeatability_proved"])
            self.assertEqual("journey_summary", records[-1]["kind"])

    def test_destination_inside_repository_is_rejected(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            EvidenceRecorder.validate_destination(
                str(root / "evidence.jsonl"), root
            )


if __name__ == "__main__":
    unittest.main()
