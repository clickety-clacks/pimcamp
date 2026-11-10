"""Real Gmail adapter logic over synthetic normalized private method results.

No lower CLI JSON/protocol output is fabricated. Parser/provider acceptance is
separate from these tests of account binding, dispatch and native resolution.
"""

import json
import time
import unittest
from unittest.mock import patch

from tests import test_mail_v2 as fixtures
from pimcamp.config import load
from pimcamp.errors import PimcampError
from pimcamp.himalaya import HimalayaOperationsAdapter
from pimcamp.mail_service import AccountMailService
from pimcamp.state import State


class GmailLinkServiceCase(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailV2Case("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.account_ids = ["account-a", "account-b", "account-c", "account-imap"]
        self.fixture.write_config(capability="native")
        self.raw_config = json.loads(self.fixture.config_path.read_text())
        self.messages = {}
        self.lookups = []
        self.reads = []
        for account_id, account in self.raw_config["mail_accounts"].items():
            account["operations_adapter"] = {
                "kind": "himalaya", "executable": "/not-installed/himalaya",
                "account": account_id, "config_paths": ["/fixture/mail.toml"],
                "inbox": "INBOX", "junk_mailbox": None,
                "from": {"name": None, "address": account["address"]},
            }
            if account_id != "account-imap":
                account["operations_adapter"]["backend"] = "gmail"
            else:
                account["identity_capability"] = "unsupported"
            item = fixtures.message("shared-native-key", key=None)
            item["subject"] = account_id
            item["folder"] = "INBOX"
            self.messages[account_id] = [item]
        self.save_config()

        def list_messages(adapter, payload, deadline):
            return {"messages": [{key: item[key] for key in (
                "adapter_id", "from", "subject", "received_at", "unread"
            )} for item in self.messages[adapter.config.account] if item["folder"] == adapter.config.inbox],
                "next_cursor": None}

        def native_id(adapter, key, deadline):
            self.lookups.append((adapter.config.account, key))
            if not any(item["adapter_id"] == key for item in self.messages[adapter.config.account]):
                raise PimcampError("not_found", "Synthetic native identity absent.")
            return key

        def read_message(adapter, payload, deadline):
            self.reads.append((adapter.config.account, payload["adapter_id"]))
            item = next(item for item in self.messages[adapter.config.account]
                        if item["adapter_id"] == payload["adapter_id"])
            return {key: item[key] for key in (
                "from", "to", "cc", "reply_to", "subject", "sent_at", "body_parts", "threading"
            )}

        for name, function in (("_list", list_messages), ("_native_message_id", native_id), ("_read", read_message)):
            patcher = patch.object(HimalayaOperationsAdapter, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = State(self.fixture.state_path)
        self.addCleanup(self.state.close)

    def save_config(self):
        self.fixture.config_path.write_text(json.dumps(self.raw_config))

    def service(self):
        return AccountMailService(load(self.fixture.config_path), self.state, "fixture-agent", self.fixture.account_ids)

    def issue(self, account):
        service = self.service()
        source = service.list_account({"account_id": account, "limit": 10})["messages"][0]["source_ref"]
        return service.issue_link(account, source), source

    def test_native_account_scoping_archive_restart_and_default_change(self):
        links = {account: self.issue(account)[0] for account in self.fixture.account_ids[:3]}
        self.assertEqual(3, len({item["locator"] for item in links.values()}))
        for account in links:
            self.messages[account][0]["folder"] = "Archive"
        self.raw_config["operations_adapter"] = self.raw_config["mail_accounts"]["account-imap"]["operations_adapter"]
        self.save_config()
        self.state.connection.execute("DELETE FROM mail_source_references")
        self.state.close()
        self.state = State(self.fixture.state_path)
        self.addCleanup(self.state.close)
        for account, link in links.items():
            self.assertEqual("linked", link["status"])
            current = self.service().get_message(link["locator"])
            self.assertEqual(account, current["account_id"])
            self.assertEqual(account, current["source"]["subject"])

    def test_address_change_refuses_old_source_and_locator_before_lookup(self):
        old, source = self.issue("account-a")
        count = len(self.lookups)
        self.raw_config["mail_accounts"]["account-a"]["address"] = "changed@example.test"
        self.save_config()
        self.assertEqual({"status": "cannot_link", "reason": "account_identity_changed"},
                         self.service().issue_link("account-a", source))
        with self.assertRaises(PimcampError) as error:
            self.service().get_message(old["locator"])
        self.assertEqual("conflict", error.exception.code)
        self.assertEqual(count, len(self.lookups))
        self.assertNotEqual(old["locator"], self.issue("account-a")[0]["locator"])

    def test_imap_uid_reuse_and_missing_account_address_cannot_issue_links(self):
        for unrelated_subject in ("first UID occupant", "replacement UID occupant"):
            self.messages["account-imap"][0]["subject"] = unrelated_subject
            result, _ = self.issue("account-imap")
            self.assertEqual({"status": "cannot_link", "reason": "identity_evidence_missing"}, result)
        del self.raw_config["mail_accounts"]["account-a"]["address"]
        self.save_config()
        self.assertEqual({"status": "cannot_link", "reason": "account_identity_unconfigured"}, self.issue("account-a")[0])

    def test_native_metadata_mismatch_is_not_linked(self):
        with patch.object(HimalayaOperationsAdapter, "_native_message_id", return_value="different-key"):
            self.assertEqual({"status": "cannot_link", "reason": "identity_not_resolvable"}, self.issue("account-a")[0])

    def test_native_command_is_direct_and_explicit_without_lower_fixture(self):
        # Temporarily restore only this real normalized method; stop before any
        # lower output exists. All network/process calls remain disabled.
        config = load(self.fixture.config_path).mail_accounts["account-a"].operations
        adapter = HimalayaOperationsAdapter(config)
        with patch("pimcamp.himalaya.subprocess.Popen", side_effect=OSError("fixture unavailable")) as spawn:
            with self.assertRaises(PimcampError):
                REAL_NATIVE_LOOKUP(adapter, "native-key", time.monotonic() + 1)
        command = spawn.call_args.args[0]
        self.assertEqual("gmail", command[command.index("--backend") + 1])
        self.assertEqual(["gmail", "messages", "get", "native-key", "--format", "metadata"], command[-6:])


REAL_NATIVE_LOOKUP = HimalayaOperationsAdapter._native_message_id

if __name__ == "__main__":
    unittest.main()
