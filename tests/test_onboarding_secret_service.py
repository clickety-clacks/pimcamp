from pathlib import Path
import json
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

    def _status(self, answers, control=False):
        """answers: fake busctl replies keyed by D-Bus member; secret-tool calls are recorded."""
        self.tool_calls, stored = [], []
        store = SecretServiceCredentials("/usr/bin/secret-tool", "unix:path=/fixture/user-bus",
                                         control_directory="/fixture/keyring")
        def run(command, input=None, **kwargs):
            if command[0] == "/usr/bin/busctl":
                member = next(m for m in ("ReadAlias", "Locked") if m in command)
                answer = answers.get(member)
                if answer is None:
                    return subprocess.CompletedProcess(command, 1, b"")
                return subprocess.CompletedProcess(command, 0, json.dumps({"type": "o", "data": answer}).encode())
            self.tool_calls.append(command)
            if command[1] == "store":
                stored.append(input)
            return subprocess.CompletedProcess(command, 0, stored[-1] if command[1] == "lookup" else b"")
        with patch("pimcamp.onboarding_credentials.subprocess.run", side_effect=run), \
             patch("pimcamp.onboarding_credentials.os.path.exists", return_value=control):
            return store.storage_status()

    def test_storage_status_distinguishes_missing_locked_ready_and_unavailable(self):
        self.assertEqual(self._status({"ReadAlias": ["/"]}), {"state": "missing", "manageable": False})
        self.assertEqual(self._status({"ReadAlias": ["/org/freedesktop/secrets/collection/login"], "Locked": True}, control=True),
                         {"state": "locked", "manageable": True})
        self.assertEqual(self._status({"ReadAlias": ["/org/freedesktop/secrets/collection/login"], "Locked": False}),
                         {"state": "ready", "manageable": False})
        self.assertEqual([c[1] for c in self.tool_calls], ["store", "lookup", "clear"], "Ready is proven by a round trip, not an exit code")
        self.assertEqual(self._status({}), {"state": "unavailable", "manageable": False})
        self.assertEqual(self._status({"ReadAlias": ["/org/freedesktop/secrets/collection/login"]}), {"state": "unavailable", "manageable": False})

    def _unlock(self, state, password, create, outcome="ok", after=None, manageable=True):
        store = SecretServiceCredentials("/usr/bin/secret-tool", "unix:path=/fixture/user-bus",
                                         control_directory="/fixture/keyring")
        statuses = [{"state": state, "manageable": manageable}, {"state": after or "ready", "manageable": manageable}]
        self.unlock_calls = []
        def control(secret):
            self.unlock_calls.append(secret)
            return outcome
        with patch.object(store, "storage_status", side_effect=statuses), \
             patch.object(store, "_control_unlock", side_effect=control):
            return store.unlock_storage(password, create=create)

    def test_unlock_never_resets_an_existing_vault_or_unlocks_a_missing_one(self):
        with self.assertRaises(CredentialError) as exists:
            self._unlock("locked", "fixture-vault-password", True)
        self.assertEqual(exists.exception.code, "vault_exists")
        with self.assertRaises(CredentialError) as missing:
            self._unlock("missing", "fixture-vault-password", False)
        self.assertEqual(missing.exception.code, "vault_missing")
        with self.assertRaises(CredentialError) as empty:
            self._unlock("locked", "", False)
        self.assertEqual(empty.exception.code, "vault_password_required")
        with self.assertRaises(CredentialError) as unmanaged:
            self._unlock("locked", "fixture-vault-password", False, manageable=False)
        self.assertEqual(unmanaged.exception.code, "vault_unmanaged")
        with self.assertRaises(CredentialError) as down:
            self._unlock("unavailable", "fixture-vault-password", False)
        self.assertEqual(down.exception.code, "storage_unavailable")
        self.assertEqual(self.unlock_calls, [], "No password reaches the daemon when the request is refused")

    def test_unlock_reports_rejection_and_proves_readiness_afterwards(self):
        self.assertEqual(self._unlock("locked", "fixture-vault-password", False), {"state": "ready", "manageable": True})
        self.assertEqual(self.unlock_calls, ["fixture-vault-password"])
        self.assertEqual(self._unlock("missing", "fixture-vault-password", True), {"state": "ready", "manageable": True})
        with self.assertRaises(CredentialError) as rejected:
            self._unlock("locked", "fixture-vault-password", False, outcome="denied")
        self.assertEqual(rejected.exception.code, "vault_password_rejected")
        with self.assertRaises(CredentialError) as still_locked:
            self._unlock("locked", "fixture-vault-password", False, after="locked")
        self.assertEqual(still_locked.exception.code, "storage_unavailable")
        with self.assertRaises(CredentialError) as gone:
            self._unlock("missing", "fixture-vault-password", True, outcome="unreachable")
        self.assertEqual(gone.exception.code, "storage_unavailable")
        self.assertTrue(all("fixture-vault-password" not in str(e.exception) for e in (rejected, still_locked, gone)))

    def test_control_unlock_frames_the_request_and_reads_the_result(self):
        store = SecretServiceCredentials("/usr/bin/secret-tool", "unix:path=/fixture/user-bus",
                                         control_directory="/fixture/keyring")
        sent = []
        class Connection:
            def __init__(self, *a): self.reads = [b"\0\0\0\x08", b"\0\0\0\x01"]
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def settimeout(self, *_): pass
            def connect(self, path): sent.append(("connect", path))
            def sendall(self, data): sent.append(("send", data))
            def recv(self, n): return self.reads.pop(0)
        with patch("pimcamp.onboarding_credentials.socket.socket", Connection):
            self.assertEqual(store._control_unlock("pw"), "denied")
        self.assertEqual(sent, [("connect", "/fixture/keyring/control"),
                                ("send", b"\0" + b"\0\0\0\x0e" + b"\0\0\0\x01" + b"\0\0\0\x02" + b"pw")])
        def refused(*a):
            raise OSError("fixture")
        with patch("pimcamp.onboarding_credentials.socket.socket", side_effect=refused):
            self.assertEqual(store._control_unlock("pw"), "unreachable")

if __name__ == "__main__":
    unittest.main()
