from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pimcamp.onboarding_credentials import SecretServiceCredentials, CredentialError, CredentialRef


class SecretServiceCredentialTests(unittest.TestCase):
    def setUp(self):
        self.store = SecretServiceCredentials("/usr/bin/secret-tool", "unix:path=/fixture/user-bus")

    def test_store_uses_default_collection_and_secret_stdin_only(self):
        with patch("pimcamp.onboarding_credentials.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"")) as run:
            reference = self.store.create("revision-a", "imap", "  fixture password  ")
        args, kwargs = run.call_args
        self.assertEqual(kwargs["input"], b"  fixture password  ")
        self.assertNotIn("fixture password", str(args))
        self.assertNotIn("fixture password", str(kwargs["env"]))
        self.assertEqual(args[0][args[0].index("--collection") + 1], "default")
        self.assertEqual(reference.backend, "secret-service")
        self.assertIsNone(reference.serial)

    def test_repeated_creates_never_target_the_same_item(self):
        with patch("pimcamp.onboarding_credentials.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"")):
            one = self.store.create("revision-a", "imap", "fixture")
            two = self.store.create("revision-a", "imap", "fixture")
        self.assertNotEqual(one.entry, two.entry)

    def test_read_command_carries_only_nonsecret_routing_and_item_identity(self):
        command = self.store.command(CredentialRef("account:opaque:imap", None, "secret-service"))
        self.assertEqual(command[:3], ["/usr/bin/env", "DBUS_SESSION_BUS_ADDRESS=unix:path=/fixture/user-bus", "/usr/bin/secret-tool"])
        self.assertEqual(command[3:], ["lookup", "application", "pimcamp", "entry", "account:opaque:imap"])

    def test_missing_or_locked_keyring_fails_without_plaintext_fallback(self):
        with patch("pimcamp.onboarding_credentials.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(CredentialError):
                self.store.create("revision-a", "imap", "fixture")

    def test_cleanup_targets_only_the_owned_item(self):
        reference = CredentialRef("account:opaque:imap", None, "secret-service")
        with patch("pimcamp.onboarding_credentials.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"")) as run:
            self.assertTrue(self.store.remove(reference))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/secret-tool", "clear", "application", "pimcamp", "entry", reference.entry])
        self.assertFalse(self.store.remove(CredentialRef("kernel-key", 123)))


if __name__ == "__main__":
    unittest.main()
