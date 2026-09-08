from pathlib import Path
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pimcamp.onboarding_credentials import CredentialRef
from pimcamp.onboarding_service import SetupError, SetupService
from pimcamp.onboarding_store import AccountStore


class FakeCredentials:
    def __init__(self):
        self.keys = {}
        self.next_serial = 1

    def create(self, revision, purpose, secret):
        reference = CredentialRef(f"account:{hashlib.sha256(revision.encode()).hexdigest()}:{purpose}", self.next_serial)
        self.next_serial += 1
        self.keys[reference.serial] = (reference, secret)
        return reference

    def command(self, reference):
        return ["/fixture/read", reference.entry]

    def write_command(self, reference):
        return ["/fixture/write", reference.entry]

    def remove(self, reference):
        self.keys.pop(reference.serial, None)
        return True


class SetupServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.credentials = FakeCredentials()
        self.outcomes = {"imap": True, "smtp": True}
        self.calls = []
        def checker(executable, config, account, backend):
            self.assertTrue(config.is_file())
            self.assertNotIn(b"fixture-password", config.read_bytes())
            self.calls.append(backend)
            return {"ok": self.outcomes[backend], "message": "fixture authentication result"}
        self.service = SetupService(AccountStore(root / "config"), self.credentials, root / "state",
                                    "/fixture/himalaya", "/fixture/carillon", checker=checker)
        self.draft = {"method": "imap", "email": "alex@custom.example", "name": "Work · alex@custom.example",
                      "incoming": {"host": "incoming.custom.example", "port": "993", "security": "tls",
                                   "username": "alex@custom.example", "password": "fixture-password"},
                      "outgoing": {"host": "outgoing.custom.example", "port": "587", "security": "starttls",
                                   "sameLogin": True, "username": "", "password": ""}}
        self.setup_id = self.service.begin()["setupId"]

    def checked(self, setup_id=None, draft=None):
        setup_id, draft = setup_id or self.setup_id, draft or self.draft
        for backend in ("imap", "smtp"):
            self.service.check(setup_id, draft, backend)

    def test_complete_imap_setup_publishes_matching_real_config_files(self):
        self.checked()
        result = self.service.commit(self.setup_id, self.draft)
        self.assertTrue(result["ok"])
        account = self.service.list_accounts()[0]
        self.assertEqual(account["address"], self.draft["email"])
        self.assertEqual(account["incoming"]["host"], "incoming.custom.example")
        revision = self.service.store._revision(result["accountId"])
        config = json.loads((revision / "config.json").read_text())
        self.assertEqual(config["operations_adapter"]["config_paths"], [str(revision / "himalaya.toml")])
        for path in revision.iterdir():
            self.assertNotIn(b"fixture-password", path.read_bytes())
        self.assertIsNone(self.service.attempts[self.setup_id].settings)
        self.assertEqual(len(self.credentials.keys), 3)

    def test_remote_host_label_is_explicit_installation_metadata(self):
        self.assertFalse(self.service.begin()["installation"]["remote"])
        self.service.remote_host_label = "My mail host"
        installation = self.service.begin()["installation"]
        self.assertTrue(installation["remote"])
        self.assertEqual(installation["host"], "My mail host")

    def google_authorized(self):
        from pimcamp.onboarding_oauth import GoogleApplication, GoogleGrant
        from unittest.mock import Mock
        self.service.google_application = GoogleApplication("fixture.apps.googleusercontent.com", "/fixture/ortie")
        self.service.oauth = Mock()
        grant = GoogleGrant("https://accounts.google.com/fixture", "state-fixture-0123456789", "v" * 43,
                            self.service.oauth_callback)
        self.service.oauth.begin.return_value = grant
        self.service.oauth.resume.return_value = "authorized"
        self.service.identity_reader = lambda *_: "selected@custom.example"
        self.service.begin_oauth(self.setup_id)
        self.service.complete_oauth(self.service.oauth_callback + "?state=" + grant.state + "&code=fixture")
        return {"method": "google", "email": "selected@custom.example", "name": "Google work"}

    def test_google_setup_persists_refresh_wiring_after_temporary_cleanup(self):
        draft = self.google_authorized()
        self.assertEqual(self.service.oauth_result(self.setup_id)["identity"], draft["email"])
        self.checked(draft=draft)
        result = self.service.commit(self.setup_id, draft)
        revision = self.service.store._revision(result["accountId"])
        self.assertTrue((revision / "ortie.toml").is_file())
        for filename in ("himalaya.toml", "carillon.toml"):
            raw = (revision / filename).read_text()
            self.assertIn(str(revision / "ortie.toml"), raw)
            self.assertNotIn(".check-", raw)
        self.assertEqual(self.service.list_accounts()[0]["method"], "google")
        self.assertEqual(len(self.credentials.keys), 2)
        self.assertTrue(self.service.commit(self.setup_id, draft)["ok"])

    def test_google_identity_must_be_confirmed_before_checking(self):
        draft = self.google_authorized()
        draft["email"] = "different@custom.example"
        with self.assertRaisesRegex(SetupError, "Confirm the email address"):
            self.service.check(self.setup_id, draft, "imap")
        self.assertEqual(self.calls, [])

    def test_google_cancellation_cleans_only_pending_credentials(self):
        self.google_authorized()
        self.service.cancel_oauth(self.setup_id)
        self.assertEqual(self.credentials.keys, {})
        self.assertEqual(self.service.oauth_result(self.setup_id)["status"], "cancelled")

    def test_unknown_google_callback_cannot_exchange_authorization(self):
        from pimcamp.onboarding_oauth import OAuthSetupError
        self.google_authorized()
        self.service.oauth.resume.reset_mock()
        with self.assertRaises(OAuthSetupError):
            self.service.complete_oauth(self.service.oauth_callback + "?state=unknown&code=fixture")
        self.service.oauth.resume.assert_not_called()

    def test_rejected_credentials_cannot_be_committed(self):
        self.outcomes["imap"] = False
        self.checked()
        with self.assertRaises(SetupError):
            self.service.commit(self.setup_id, self.draft)
        self.assertEqual(self.service.list_accounts(), [])

    def test_settings_change_requires_both_checks_again(self):
        self.checked()
        changed = copy.deepcopy(self.draft)
        changed["incoming"]["host"] = "replacement.custom.example"
        with self.assertRaises(SetupError):
            self.service.commit(self.setup_id, changed)
        self.service.check(self.setup_id, changed, "imap")
        with self.assertRaises(SetupError):
            self.service.commit(self.setup_id, changed)
        self.service.check(self.setup_id, changed, "smtp")
        self.assertTrue(self.service.commit(self.setup_id, changed)["ok"])

    def test_commit_retry_has_no_duplicate_account_or_secret(self):
        self.checked()
        first = self.service.commit(self.setup_id, self.draft)
        second = self.service.commit(self.setup_id, self.draft)
        self.assertEqual(first["accountId"], second["accountId"])
        self.assertEqual(len(self.credentials.keys), 3)
        self.assertEqual(len(self.service.list_accounts()), 1)

    def test_cancel_cleans_only_unpublished_setup_owned_keys(self):
        self.checked()
        self.service.cancel(self.setup_id)
        self.assertEqual(self.credentials.keys, {})
        self.assertEqual(list(self.service.store.root.glob(".check-*")), [])

    def test_cancel_after_commit_keeps_saved_credentials(self):
        self.checked()
        self.service.commit(self.setup_id, self.draft)
        self.service.cancel(self.setup_id)
        self.assertEqual(len(self.credentials.keys), 3)

    def test_reconnect_preserves_client_access_and_existing_account(self):
        self.checked()
        identifier = self.service.commit(self.setup_id, self.draft)["accountId"]
        original = self.service.store._revision(identifier)
        new_setup = self.service.begin(identifier)["setupId"]
        self.checked(new_setup)
        self.service.commit(new_setup, self.draft)
        current = self.service.store._revision(identifier)
        self.assertNotEqual(original, current)
        self.assertEqual(json.loads((original / "config.json").read_text())["credentials"],
                         json.loads((current / "config.json").read_text())["credentials"])
        self.assertEqual(json.loads((original / "credential-refs.json").read_text())["client"],
                         json.loads((current / "credential-refs.json").read_text())["client"])
        self.assertEqual(len(self.service.list_accounts()), 1)

    def test_publish_failure_can_be_retried_or_cancelled(self):
        self.checked()
        with patch.object(self.service.store, "publish", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.service.commit(self.setup_id, self.draft)
        self.assertEqual(self.service.list_accounts(), [])
        self.assertTrue(self.service.commit(self.setup_id, self.draft)["ok"])
        self.assertEqual(len(self.credentials.keys), 3)
