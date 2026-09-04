"""Checks that do not invent or replay lower-adapter output."""

from email import policy
from email.parser import BytesParser
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pimcamp.config import HimalayaConfig
from pimcamp.himalaya import HimalayaOperationsAdapter


class HimalayaConstructionCase(unittest.TestCase):
    def setUp(self) -> None:
        config = HimalayaConfig(
            kind="himalaya",
            executable="/missing/himalaya",
            account="live-test",
            config_paths=("/private/himalaya.toml",),
            inbox="INBOX",
            junk_mailbox="Junk",
            sender={"name": "Sender", "address": "sender@example.test"},
            raw={},
        )
        self.adapter = HimalayaOperationsAdapter(config)

    def test_list_accepts_the_published_envelopes_only_object(self) -> None:
        with patch.object(
            self.adapter,
            "_run_json",
            return_value={"envelopes": []},
        ):
            result = self.adapter.call("list", {"limit": 25}, time.monotonic() + 1)

        self.assertEqual({"messages": [], "next_cursor": None}, result)

    def test_bcc_stays_in_the_smtp_envelope_and_out_of_the_message_headers(self) -> None:
        composition = {
            "kind": "new",
            "to": [{"name": "Recipient", "address": "to@example.test"}],
            "cc": [{"name": None, "address": "cc@example.test"}],
            "bcc": [{"name": None, "address": "bcc@example.test"}],
            "subject": "Boundary",
            "body_text": "Body",
            "reply_to_message_ref": None,
        }
        raw = self.adapter._message(composition, None)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual("Sender <sender@example.test>", str(message["From"]))
        self.assertEqual("Recipient <to@example.test>", str(message["To"]))
        self.assertEqual("cc@example.test", str(message["Cc"]))
        self.assertIsNone(message["Bcc"])
        self.assertEqual("Body\r\n", message.get_content())

    def test_reply_headers_come_only_from_current_threading_context(self) -> None:
        composition = {
            "kind": "reply",
            "to": [{"name": None, "address": "to@example.test"}],
            "cc": [],
            "bcc": [],
            "subject": "Re: Boundary",
            "body_text": "Reply",
            "reply_to_message_ref": "opaque-public-reference",
        }
        raw = self.adapter._message(
            composition,
            {
                "message_id": "<source@example.test>",
                "references": ["<ancestor@example.test>"],
            },
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual("<source@example.test>", str(message["In-Reply-To"]))
        self.assertEqual(
            "<ancestor@example.test> <source@example.test>",
            str(message["References"]),
        )
        self.assertNotIn("opaque-public-reference", raw.decode("ascii"))


if __name__ == "__main__":
    unittest.main()
