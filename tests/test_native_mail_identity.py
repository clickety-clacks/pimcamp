"""Native identity contract evidence, not Gmail provider/response captures."""

import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from tests import test_mail_v2 as fixtures
from pimcamp.config import _adapter
from pimcamp.errors import PimcampError
from pimcamp.himalaya import HimalayaOperationsAdapter


class NativeMailIdentityCase(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailV2Case("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.native_accounts = ["native-a", "native-b", "native-c"]
        self.fixture.account_ids = [*self.native_accounts, "unestablished"]
        self.fixture.write_config(capability="native")
        config = json.loads(self.fixture.config_path.read_text())
        self.paths = {}
        for account_id, account in config["mail_accounts"].items():
            path = self.fixture.root / f"{account_id}.json"
            self.paths[account_id] = path
            item = fixtures.message("same-native-id", key=None)
            item["subject"] = f"Source for {account_id}"
            if account_id in self.native_accounts:
                # Authority belongs exclusively to this synthetic private port.
                # It is not an assertion of any real Gmail account identity.
                item["identity_evidence"] = {
                    "namespace": f"fixture-native-message-v1/{account_id}/incarnation-one",
                    "key": "same-native-id",
                    "continuity": "native",
                    "backend": "fixture-native",
                    "folder": "INBOX",
                    "generation": None,
                }
            else:
                account["identity_capability"] = "unknown"
            path.write_text(json.dumps({"messages": [item]}))
            account["operations_adapter"]["command"][2] = str(path)
        self.fixture.config_path.write_text(json.dumps(config))

    def issue(self, account_id):
        listed = self.fixture.invoke("list_account", {"account_id": account_id, "limit": 10})
        source_ref = listed["result"]["messages"][0]["source_ref"]
        return self.fixture.invoke("issue_link", {
            "account_id": account_id, "source_ref": source_ref,
        })["result"], source_ref

    def test_three_native_accounts_survive_archive_restart_and_default_change(self):
        issued = {account: self.issue(account)[0] for account in self.native_accounts}
        self.assertEqual(3, len({item["locator"] for item in issued.values()}))
        for account, path in self.paths.items():
            if account not in self.native_accounts:
                continue
            contents = json.loads(path.read_text())
            contents["messages"][0]["identity_evidence"]["folder"] = "Archive"
            path.write_text(json.dumps(contents))

        # Old source observations can disappear; issued native locators remain.
        with sqlite3.connect(self.fixture.state_path) as database:
            database.execute("DELETE FROM mail_source_references")
        config = json.loads(self.fixture.config_path.read_text())
        config["operations_adapter"] = config["mail_accounts"]["native-b"]["operations_adapter"]
        self.fixture.config_path.write_text(json.dumps(config))
        app = self.fixture.app()
        cookie, _ = self.fixture.establish_session(app)
        for account, link in issued.items():
            with self.subTest(account=account):
                self.assertEqual("linked", link["status"])
                response = app.handle("GET", f'/mail/api/v2/messages/{link["locator"]}',
                                      self.fixture.authenticated_headers(cookie))
                self.assertEqual(200, response.status)
                value = json.loads(response.body)
                self.assertEqual(account, value["account_id"])
                self.assertEqual(f"Source for {account}", value["source"]["subject"])
                self.assertEqual(link["url"], self.issue(account)[0]["url"])
        self.assertEqual([], self.fixture.calls_for("send"))

    def test_cross_account_source_and_unestablished_account_return_no_url(self):
        _, source_ref = self.issue("native-a")
        result = self.fixture.invoke("issue_link", {
            "account_id": "native-b", "source_ref": source_ref,
        })["result"]
        self.assertEqual({"status": "cannot_link", "reason": "account_mismatch"}, result)
        result, _ = self.issue("unestablished")
        self.assertEqual({"status": "cannot_link", "reason": "identity_evidence_missing"}, result)
        # Merely declaring native capability cannot manufacture missing authority.
        config = json.loads(self.fixture.config_path.read_text())
        config["mail_accounts"]["unestablished"]["identity_capability"] = "native"
        self.fixture.config_path.write_text(json.dumps(config))
        self.assertEqual(result, self.issue("unestablished")[0])
        self.assertEqual([], self.fixture.calls_for("send"))

    def test_changed_account_incarnation_cannot_resolve_old_native_key(self):
        link, source_ref = self.issue("native-a")
        path = self.paths["native-a"]
        contents = json.loads(path.read_text())
        contents["messages"][0]["identity_evidence"]["namespace"] = "fixture-native-message-v1/native-a/incarnation-two"
        path.write_text(json.dumps(contents))
        result = self.fixture.invoke("issue_link", {
            "account_id": "native-a", "source_ref": source_ref,
        })["result"]
        self.assertEqual({"status": "cannot_link", "reason": "identity_not_found"}, result)
        app = self.fixture.app()
        cookie, _ = self.fixture.establish_session(app)
        response = app.handle("GET", f'/mail/api/v2/messages/{link["locator"]}',
                              self.fixture.authenticated_headers(cookie))
        self.assertEqual(404, response.status)
        self.assertEqual([], self.fixture.calls_for("read"))
        self.assertEqual([], self.fixture.calls_for("send"))
        self.assertNotEqual(link["locator"], self.issue("native-a")[0]["locator"])


class GmailBackendSelectionCase(unittest.TestCase):
    def config(self, **extra):
        return {
            "kind": "himalaya", "executable": "/not-installed/himalaya",
            "account": "configured-account", "config_paths": ["/fixture/mail.toml"],
            "inbox": "INBOX", "junk_mailbox": None,
            "from": {"name": None, "address": "sender@example.test"}, **extra,
        }

    def test_v2_can_pin_native_backend_while_v1_schema_stays_closed(self):
        value = self.config(backend="gmail")
        adapter = HimalayaOperationsAdapter(_adapter(value, "operations", mail_account=True))
        # Capture only commands on spawn failure; no invented lower responses.
        for operation, payload in (("list", {"limit": 1}), ("read", {"adapter_id": "native-id"})):
            with self.subTest(operation=operation), patch(
                "pimcamp.himalaya.subprocess.Popen", side_effect=OSError("fixture unavailable")
            ) as spawn:
                with self.assertRaises(PimcampError):
                    adapter.call(operation, payload, time.monotonic() + 1)
                command = spawn.call_args.args[0]
                self.assertEqual("gmail", command[command.index("--backend") + 1])
                self.assertEqual("configured-account", command[command.index("--account") + 1])
        with self.assertRaises(PimcampError):
            _adapter(value, "operations")
        legacy = HimalayaOperationsAdapter(_adapter(self.config(), "operations"))
        self.assertNotIn("--backend", legacy._base_command())
        for unsupported in (None, "auto", "imap"):
            with self.subTest(backend=unsupported), self.assertRaises(PimcampError):
                _adapter(self.config(backend=unsupported), "operations", mail_account=True)
        with self.assertRaises(PimcampError):
            _adapter(self.config(backend="gmail", account="default"), "operations", mail_account=True)

    def test_backend_pin_does_not_claim_an_account_identity(self):
        adapter = HimalayaOperationsAdapter(_adapter(
            self.config(backend="gmail"), "operations", mail_account=True
        ))
        with patch("pimcamp.himalaya.subprocess.Popen") as spawn:
            with self.assertRaises(PimcampError) as caught:
                adapter.call("resolve_identity", {"identity_evidence": {}}, time.monotonic() + 1)
        self.assertEqual("unsupported", caught.exception.code)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
