"""Isolated private-port models, not observed Himalaya/provider captures.

Protocol grammar examples exercise our parser only. No test fabricates a CLI
JSON response or claims real provider move/rebuild continuity.
"""

import json
import time
import unittest
from unittest.mock import patch

from tests import test_mail_v2 as fixtures
from pimcamp.config import _adapter, load
from pimcamp.dovecot import DovecotIdentity, NAMESPACE, _quoted, decode_handle, parse_response
from pimcamp.errors import PimcampError
from pimcamp.himalaya import HimalayaOperationsAdapter
from pimcamp.identity import resolve_identity
from pimcamp.mail_service import AccountMailService
from pimcamp.state import State


def evidence(guid="opaque-guid"):
    return {"namespace": NAMESPACE, "key": guid, "continuity": "native",
            "backend": "dovecot", "folder": "INBOX", "generation": None}


def mail(guid, subject):
    headers = (f"From: Sender <sender@example.test>\r\nTo: owner@example.test\r\n"
               f"Reply-To: reply@example.test\r\nSubject: {subject}\r\n"
               "Message-ID: <equal@example.test>\r\nDate: Tue, 10 Nov 2026 12:00:00 +0000\r\n\r\n").encode()
    return {"guid": guid, "headers": headers, "body": headers + b"Intended body\r\n", "unread": True}


class DovecotServiceCase(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailV2Case("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.account_ids = ["native-a", "native-b"]
        self.fixture.write_config(capability="native")
        self.config = json.loads(self.fixture.config_path.read_text())
        self.mailboxes = {}
        self.calls = []
        for account_id, account in self.config["mail_accounts"].items():
            account["operations_adapter"] = {
                "kind": "himalaya", "executable": "/not-installed/himalaya", "backend": "dovecot",
                "account": account_id, "config_paths": ["/fixture/config.toml"], "inbox": "INBOX",
                "junk_mailbox": None, "from": {"name": None, "address": account["address"]},
            }
            self.mailboxes[account_id] = {"INBOX": {"1": mail("same-guid", account_id)}, "Archive": {}}
        self.save()

        def account(port):
            return port.run_json.__self__.config.account

        def folders(port, deadline):
            self.calls.append((account(port), "folders"))
            return list(self.mailboxes[account(port)])

        def search(port, folder, guid, deadline):
            self.calls.append((account(port), "search", folder, guid))
            return [uid for uid, item in self.mailboxes[account(port)][folder].items()
                    if guid is None or item["guid"] == guid]

        def fetch(port, folder, uid, body, deadline):
            self.calls.append((account(port), "fetch", folder, uid, body))
            item = self.mailboxes[account(port)][folder].get(uid)
            if item is None:
                return None
            return {"guid": item["guid"], "unread": item["unread"],
                    "body": item["headers"] if body == "header" else item["body"] if body else None}

        for name, function in (("folders", folders), ("search", search), ("fetch", fetch)):
            patcher = patch.object(DovecotIdentity, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)
        guard = patch("pimcamp.himalaya.subprocess.Popen", side_effect=AssertionError("No lower process permitted"))
        self.spawn = guard.start()
        self.addCleanup(guard.stop)
        self.state = State(self.fixture.state_path)
        self.addCleanup(self.state.close)

    def save(self):
        self.fixture.config_path.write_text(json.dumps(self.config))

    def service(self):
        return AccountMailService(load(self.fixture.config_path), self.state, "fixture-agent", self.fixture.account_ids)

    def source(self, account="native-a"):
        return self.service().list_account({"account_id": account, "limit": 10})["messages"][0]["source_ref"]

    def issue(self, account="native-a"):
        return self.service().issue_link(account, self.source(account))

    def test_cross_folder_move_uid_reuse_restart_reindex_and_default_change(self):
        link = self.issue()
        other = self.issue("native-b")
        self.assertNotEqual(link["locator"], other["locator"])
        boxes = self.mailboxes["native-a"]
        boxes["Archive"]["92"] = boxes["INBOX"].pop("1")
        boxes["INBOX"]["1"] = mail("unrelated-guid", "replacement UID occupant")
        self.config["operations_adapter"] = {key: value for key, value in
            self.config["mail_accounts"]["native-b"]["operations_adapter"].items() if key != "backend"}
        self.save()
        self.state.connection.execute("DELETE FROM mail_source_references")
        self.state.close()
        self.state = State(self.fixture.state_path)
        self.addCleanup(self.state.close)
        result = self.service().get_message(link["locator"])
        self.assertEqual("native-a", result["account_id"])
        self.assertEqual("native-a", result["source"]["subject"])
        self.assertIn(("native-a", "fetch", "Archive", "92", "body"), self.calls)
        reply = self.service().prepare_reply(link["locator"], "draft")
        self.assertEqual("native-a", reply["account_id"])
        self.assertEqual("reply@example.test", reply["reply"]["to"][0]["address"])
        self.spawn.assert_not_called()

    def test_duplicate_guid_in_other_folder_refuses_issue_and_read(self):
        source = self.source()
        link = self.service().issue_link("native-a", source)
        self.mailboxes["native-a"]["Archive"]["10"] = mail("same-guid", "copy")
        self.assertEqual({"status": "cannot_link", "reason": "ambiguous_identity"},
                         self.service().issue_link("native-a", source))
        with self.assertRaises(PimcampError) as caught:
            self.service().get_message(link["locator"])
        self.assertEqual("conflict", caught.exception.code)
        self.assertFalse(any(call[-1] == "body" for call in self.calls))

    def test_missing_or_changed_guid_does_not_match_uid_or_message_id(self):
        source = self.source()
        link = self.service().issue_link("native-a", source)
        self.mailboxes["native-a"]["INBOX"]["1"] = mail("new-guid-after-rebuild", "unrelated")
        self.assertEqual({"status": "cannot_link", "reason": "identity_not_found"},
                         self.service().issue_link("native-a", source))
        with self.assertRaises(PimcampError) as caught:
            self.service().get_message(link["locator"])
        self.assertEqual("not_found", caught.exception.code)

    def test_account_address_change_rejected_before_any_provider_call(self):
        source = self.source()
        link = self.service().issue_link("native-a", source)
        count = len(self.calls)
        self.config["mail_accounts"]["native-a"]["address"] = "replacement@example.test"
        self.save()
        self.assertEqual("account_identity_changed", self.service().issue_link("native-a", source)["reason"])
        with self.assertRaises(PimcampError) as caught:
            self.service().get_message(link["locator"])
        self.assertEqual("conflict", caught.exception.code)
        self.assertEqual(count, len(self.calls))

    def test_source_cross_account_and_missing_grants_refused(self):
        source = self.source()
        self.assertEqual("account_mismatch", self.service().issue_link("native-b", source)["reason"])
        restricted = AccountMailService(load(self.fixture.config_path), self.state, "fixture-agent", ["native-b"])
        with self.assertRaises(PimcampError) as caught:
            restricted.issue_link("native-a", source)
        self.assertEqual("permission_denied", caught.exception.code)

    def test_failed_folder_search_is_not_missing_or_unique(self):
        source = self.source()
        original = DovecotIdentity.search
        def failed(port, folder, guid, deadline):
            if folder == "Archive":
                raise PimcampError("backend_unavailable", "Synthetic inaccessible folder.")
            return original(port, folder, guid, deadline)
        with patch.object(DovecotIdentity, "search", failed):
            with self.assertRaises(PimcampError) as caught:
                self.service().issue_link("native-a", source)
        self.assertEqual("backend_unavailable", caught.exception.code)

    def test_listing_pairs_current_headers_and_guid_after_uid_reuse(self):
        self.mailboxes["native-a"]["INBOX"]["1"] = mail("new-guid", "replacement")
        result = self.service().list_account({"account_id": "native-a", "limit": 10})
        self.assertEqual("replacement", result["messages"][0]["subject"])
        link = self.service().issue_link("native-a", result["messages"][0]["source_ref"])
        self.assertEqual("replacement", self.service().get_message(link["locator"])["source"]["subject"])


class DovecotPortCase(unittest.TestCase):
    def setUp(self):
        self.port = DovecotIdentity(lambda *a, **kw: self.fail("No lower process permitted"))
        self.deadline = time.monotonic() + 10

    def test_fetch_extracts_opaque_guid_headers_and_flags_from_normalized_records(self):
        guid = b'opaque "quote" \\ slash'
        rows = [[b"*", b"1", b"FETCH", [b"UID", b"42", b"X-GUID", guid,
                 b"FLAGS", [b"\\Seen"], b"BODY[HEADER]", b"Subject: fixture\r\n\r\n"]]]
        with patch.object(self.port, "_commands", return_value=rows) as commands:
            result = self.port.fetch("Folder Name", "42", "header", self.deadline)
        self.assertEqual(guid.decode(), result["guid"])
        self.assertFalse(result["unread"])
        self.assertEqual(['EXAMINE "Folder Name"', 'UID FETCH 42 (UID X-GUID FLAGS BODY.PEEK[HEADER])'], commands.call_args.args[0])
        self.assertEqual('"opaque \\"quote\\" \\\\ slash"', _quoted(result["guid"]))

    def test_all_selectable_folders_not_only_subscriptions(self):
        rows = [[b"*", b"LIST", [], b"/", b"INBOX"],
                [b"*", b"LIST", [b"\\Noselect"], b"/", b"Container"],
                [b"*", b"LIST", [], b"/", b"Archive/unsubscribed"]]
        with patch.object(self.port, "_commands", return_value=rows) as commands:
            self.assertEqual(["INBOX", "Archive/unsubscribed"], self.port.folders(self.deadline))
        self.assertEqual(['LIST "" "*"'], commands.call_args.args[0])

    def test_search_empty_result_and_missing_result_differ(self):
        with patch.object(self.port, "_commands", return_value=[[b"*", b"SEARCH"]]):
            self.assertEqual([], self.port.search("INBOX", "guid", self.deadline))
        with patch.object(self.port, "_commands", return_value=[]):
            with self.assertRaises(PimcampError):
                self.port.search("INBOX", "guid", self.deadline)

    def test_uid_reuse_between_resolution_and_read_rejected(self):
        for changed in (None, {"guid": "other-guid", "body": b"wrong", "unread": True}):
            with self.subTest(changed=changed), patch.object(self.port, "fetch", return_value=changed):
                with self.assertRaises(PimcampError) as caught:
                    self.port.read("Archive", "12", "expected-guid", self.deadline)
                self.assertEqual("not_found" if changed is None else "conflict", caught.exception.code)

    def test_lookup_checks_every_search_match_and_does_not_accept_changed_guid(self):
        with patch.object(self.port, "folders", return_value=["INBOX", "Archive"]), \
             patch.object(self.port, "search", side_effect=[[], ["42"]]), \
             patch.object(self.port, "fetch", return_value={"guid": "different"}):
            with self.assertRaises(PimcampError) as caught:
                self.port.resolve(evidence(), self.deadline)
        self.assertEqual("conflict", caught.exception.code)

    def test_missing_guid_is_not_missing_message(self):
        rows = [[b"*", b"1", b"FETCH", [b"UID", b"42"]]]
        with patch.object(self.port, "_commands", return_value=rows):
            with self.assertRaises(PimcampError) as caught:
                self.port.fetch("INBOX", "42", None, self.deadline)
        self.assertEqual("backend_unavailable", caught.exception.code)

    def test_paging_uses_current_uid_window_and_skips_disappeared_entries(self):
        with patch.object(self.port, "search", return_value=["1", "2", "10"]), \
             patch.object(self.port, "fetch", side_effect=[None, {"guid": "g2", "body": b"headers", "unread": True}]):
            rows, more = self.port.list_page("INBOX", 1, 2, self.deadline)
        self.assertTrue(more)
        self.assertEqual("g2", rows[0]["identity_evidence"]["key"])
        self.assertEqual(("INBOX", "2", "g2"), decode_handle(rows[0]["adapter_id"]))

    def test_explicit_selector_and_raw_stdin_command_without_fake_lower_output(self):
        value = {"kind": "himalaya", "executable": "/not-installed/himalaya", "backend": "dovecot",
                 "account": "fixture-account", "config_paths": [], "inbox": "INBOX", "junk_mailbox": None,
                 "from": {"name": None, "address": "owner@example.test"}}
        adapter = HimalayaOperationsAdapter(_adapter(value, "operations", mail_account=True))
        with patch("pimcamp.himalaya.subprocess.Popen", side_effect=OSError("fixture unavailable")) as spawn:
            with self.assertRaises(PimcampError):
                DovecotIdentity(adapter._run_json).search("INBOX", 'opaque\\value', self.deadline)
        command = spawn.call_args.args[0]
        self.assertEqual(["imap", "raw"], command[-2:])
        self.assertEqual("imap", command[command.index("--backend") + 1])
        self.assertEqual("fixture-account", command[command.index("--account") + 1])
        self.assertNotIn('opaque\\value', command)
        for context in ({}, {"mail_account": True}):
            bad = dict(value, account="default") if context else value
            with self.assertRaises(PimcampError):
                _adapter(bad, "operations", **context)


class ImapGrammarCase(unittest.TestCase):
    """Protocol grammar examples only, never replayed as lower-adapter captures."""

    def test_literal_framing_quoted_guid_and_tag_looking_message_text(self):
        body = b'pc1 OK this is content\r\n* SEARCH 999\r\n'
        raw = (b'* 1 FETCH (UID 42 X-GUID "a\\"b\\\\c" BODY[] {' + str(len(body)).encode() +
               b'}\r\n' + body + b')\r\npc0 OK examined\r\npc1 OK fetched\r\n')
        rows = parse_response(raw, ["pc0", "pc1"])
        self.assertEqual(b'a"b\\c', rows[0][3][3])
        self.assertEqual(body, rows[0][3][5])

    def test_bad_missing_completion_and_truncated_literal_are_errors(self):
        for raw in (b'pc0 OK done\r\npc1 NO denied\r\n', b'pc0 OK done\r\n',
                    b'* BYE lost connection\r\n', b'* 1 FETCH (BODY[] {500}\r\nshort',
                    b'* SEARCH\r\npc0 OK done\r\npc1 BAD unsupported\r\n'):
            with self.subTest(raw=raw), self.assertRaises(PimcampError):
                parse_response(raw, ["pc0", "pc1"])

    def test_literal_mailbox_and_quoted_escapes(self):
        rows = parse_response(b'* LIST () "/" {13}\r\nFolder (name)\r\npc0 OK done\r\n', ["pc0"])
        self.assertEqual(b"Folder (name)", rows[0][4])


if __name__ == "__main__":
    unittest.main()
