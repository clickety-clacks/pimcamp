from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pimcamp.onboarding_store import AccountStore, AccountStoreError


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name) / "setup")
        self.identifier = str(uuid.uuid4())
        self.metadata = {"address": "alex@example.test", "name": "Work", "method": "imap"}
        self.files = {"himalaya.toml": b"# references only\n", "config.json": b"{}\n"}

    def test_new_account_is_additive_and_private(self):
        first = self.store.publish(self.identifier, self.metadata, self.files)
        other = str(uuid.uuid4())
        self.store.publish(other, {**self.metadata, "address": "other@example.test"}, self.files)
        self.assertEqual(len(self.store.list_accounts()), 2)
        self.assertEqual(self.store._revision(self.identifier), first)
        self.assertEqual((first / "himalaya.toml").stat().st_mode & 0o777, 0o600)

    def test_parent_directory_alias_preserves_revision_containment(self):
        parent = Path(self.temp.name)
        alias = parent / "parent-alias"
        alias.symlink_to(parent, target_is_directory=True)
        store = AccountStore(alias / "another-store")
        revision = store.publish(self.identifier, self.metadata, self.files)
        self.assertEqual(store._revision(self.identifier), revision)
        self.assertEqual(store.list_accounts()[0]["address"], self.metadata["address"])

    def test_duplicate_account_and_address_preserve_original(self):
        first = self.store.publish(self.identifier, self.metadata, self.files)
        for identifier in (self.identifier, str(uuid.uuid4())):
            with self.assertRaises(AccountStoreError):
                self.store.publish(identifier, self.metadata, self.files)
        self.assertEqual(self.store._revision(self.identifier), first)

    def test_publish_failure_leaves_old_revision_intact_and_cleans_staging(self):
        first = self.store.publish(self.identifier, self.metadata, self.files)
        with patch("pimcamp.onboarding_store.os.replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.store.publish(self.identifier, self.metadata, self.files,
                                   reconnect=True, expected_revision=first.name)
        self.assertEqual(self.store._revision(self.identifier), first)
        self.assertEqual(list(self.store.revisions.iterdir()), [first])
        self.assertEqual(len(list(self.store.accounts.iterdir())), 1)

    def test_reconnect_rejects_stale_revision(self):
        first = self.store.publish(self.identifier, self.metadata, self.files)
        second = self.store.publish(self.identifier, self.metadata, self.files,
                                    reconnect=True, expected_revision=first.name)
        with self.assertRaises(AccountStoreError):
            self.store.publish(self.identifier, self.metadata, self.files,
                               reconnect=True, expected_revision=first.name)
        self.assertEqual(self.store._revision(self.identifier), second)
        self.assertTrue(first.is_dir())

    def test_rejects_traversal_secret_metadata_and_public_directory(self):
        with self.assertRaises(AccountStoreError):
            self.store.publish("../escape", self.metadata, self.files)
        with self.assertRaises(AccountStoreError):
            self.store.publish(self.identifier, {**self.metadata, "incoming": {"password": "fixture"}}, self.files)
        self.store.root.chmod(0o755)
        with self.assertRaises(AccountStoreError):
            AccountStore(self.store.root)

    def test_account_link_cannot_escape_revision_store(self):
        (self.store.accounts / self.identifier).symlink_to(Path(self.temp.name))
        with self.assertRaises(AccountStoreError):
            self.store.list_accounts()

    def test_default_tracks_reconnect_and_preserves_existing_selection(self):
        first = self.store.publish(self.identifier, self.metadata, self.files)
        destination = Path(self.temp.name) / "runtime" / "config.json"
        self.assertTrue(self.store.ensure_runtime_default(self.identifier, destination))
        second = self.store.publish(self.identifier, self.metadata,
            {**self.files, "config.json": b'{"revision": 2}\n'},
            reconnect=True, expected_revision=first.name)
        self.assertEqual(destination.resolve(), second / "config.json")
        other = str(uuid.uuid4())
        self.store.publish(other, {**self.metadata, "address": "other@example.test"}, self.files)
        self.assertFalse(self.store.ensure_runtime_default(other, destination))
        self.assertEqual(destination.resolve(), second / "config.json")

    def test_existing_external_default_is_never_replaced(self):
        self.store.publish(self.identifier, self.metadata, self.files)
        destination = Path(self.temp.name) / "config.json"
        destination.write_bytes(b"existing deployment")
        self.assertFalse(self.store.ensure_runtime_default(self.identifier, destination))
        self.assertEqual(destination.read_bytes(), b"existing deployment")


if __name__ == "__main__":
    unittest.main()
