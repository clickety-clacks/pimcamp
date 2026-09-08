"""No live keys or real secrets: verify the adapted helper boundary with fakes."""
from pathlib import Path
import runpy
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class AccountCredentialTests(unittest.TestCase):
    def setUp(self):
        self.reader = runpy.run_path(str(ROOT / "scripts/pimcamp-keyctl-read"))
        self.writer = runpy.run_path(str(ROOT / "scripts/pimcamp-keyctl-write"))

    def test_accounts_and_purposes_have_independent_key_names(self):
        entries = [f"account:{account * 64}:{purpose}"
                   for account in ("a", "b") for purpose in ("imap", "smtp", "oauth", "client")]
        names = [self.writer["entry_description"](entry) for entry in entries]
        self.assertEqual(len(set(names)), 8)
        for entry, name in zip(entries, names):
            self.assertEqual(self.reader["entry_description"](entry), name)

    def test_legacy_and_unscoped_names_are_rejected(self):
        for entry in ("imap", "gmail-oauth", "pimcamp-client", "../account", "account:x:imap",
                      "account:" + "a" * 64 + ":other", None):
            for helper in (self.reader, self.writer):
                with self.subTest(entry=entry), self.assertRaises(ValueError):
                    helper["entry_description"](entry)

    def test_secret_only_enters_subprocess_stdin(self):
        secret = b"fixture-only-secret"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"123")) as run:
            self.writer["run_keyctl"](["padd", "user", "opaque-description", "@u"], secret)
        args, kwargs = run.call_args
        self.assertNotIn(secret.decode(), str(args))
        self.assertNotIn(secret.decode(), str(kwargs["env"]))
        self.assertEqual(kwargs["input"], secret)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_create_only_never_updates_existing_key(self):
        store = self.writer["store"]
        with patch.dict(store.__globals__, {"matching_serials": lambda *_: ["123"],
                                            "run_keyctl": lambda *_: self.fail("unexpected write")}):
            with self.assertRaises(self.writer["HelperError"]):
                store("@u", "account:" + "a" * 64 + ":imap", b"fixture", create_only=True)

    def test_duplicate_matching_keys_fail_closed(self):
        store = self.writer["store"]
        with patch.dict(store.__globals__, {"matching_serials": lambda *_: ["123", "456"],
                                            "run_keyctl": lambda *_: self.fail("unexpected write")}):
            with self.assertRaises(self.writer["HelperError"]):
                store("@u", "account:" + "a" * 64 + ":imap", b"fixture")

    def test_secret_normalization_preserves_whitespace(self):
        self.assertEqual(self.writer["normalize_secret"](b"  fixture  \n"), b"  fixture  ")


if __name__ == "__main__":
    unittest.main()
