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

        def search_many(port, folders, guid, deadline):
            return {folder: search(port, folder, guid, deadline) for folder in folders}

        def fetch_many(port, requests, body, deadline):
            return {folder: {"generation": "123", "messages": {
                uid: item for uid in uids if (item := fetch(port, folder, uid, body, deadline)) is not None
            }} for folder, uids in requests.items()}

        def envelopes(port, folder, uids, deadline):
            result = {}
            for uid in uids:
                item = self.mailboxes[account(port)][folder].get(uid)
                if item is not None:
                    headers = HimalayaOperationsAdapter._parse_message(item["headers"])
                    result[uid] = {"from": headers["from"], "subject": headers["subject"],
                                   "received_at": headers["sent_at"], "unread": item["unread"]}
            return result

        for name, function in (("folders", folders), ("search", search), ("fetch", fetch),
                               ("search_many", search_many), ("fetch_many", fetch_many), ("envelopes", envelopes)):
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
        def failed(port, folders, guid, deadline):
            raise PimcampError("backend_unavailable", "Synthetic inaccessible folder.")
        with patch.object(DovecotIdentity, "search_many", failed):
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

    def test_fetch_extracts_opaque_guid_and_body_from_normalized_records(self):
        guid = b'opaque "quote" \\ slash'
        rows = [[b"*", b"1", b"FETCH", [b"UID", b"42", b"X-GUID", guid,
                 b"BODY[]", b"Subject: fixture\r\n\r\nbody"]]]
        groups = [[[b"*", b"UIDVALIDITY", b"12"]], rows]
        with patch.object(self.port, "_command_groups", return_value=groups) as commands:
            result = self.port.fetch("Folder Name", "42", "body", self.deadline)
        self.assertEqual(guid.decode(), result["guid"])
        self.assertEqual(b"Subject: fixture\r\n\r\nbody", result["body"])
        self.assertEqual(['EXAMINE "Folder Name"', 'UID FETCH 42 (UID X-GUID BODY.PEEK[])'], commands.call_args.args[0])
        self.assertEqual('"opaque \\"quote\\" \\\\ slash"', _quoted(result["guid"]))

    def test_all_selectable_folders_not_only_subscriptions(self):
        rows = [[b"*", b"LIST", [], b"/", b"INBOX"],
                [b"*", b"LIST", [b"\\Noselect"], b"/", b"Container"],
                [b"*", b"LIST", [], b"/", b"Archive/unsubscribed"]]
        with patch.object(self.port, "_commands", return_value=rows) as commands:
            self.assertEqual(["INBOX", "Archive/unsubscribed"], self.port.folders(self.deadline))
        self.assertEqual(['LIST "" "*"'], commands.call_args.args[0])

    def test_search_empty_result_and_missing_result_differ(self):
        with patch.object(self.port, "_command_groups", return_value=[[], [[b"*", b"SEARCH"]]]):
            self.assertEqual([], self.port.search("INBOX", "guid", self.deadline))
        with patch.object(self.port, "_command_groups", return_value=[[], []]):
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
             patch.object(self.port, "search_many", return_value={"INBOX": [], "Archive": ["42"]}), \
             patch.object(self.port, "fetch_many", return_value={"Archive": {"messages": {"42": {"guid": "different"}}}}):
            with self.assertRaises(PimcampError) as caught:
                self.port.resolve(evidence(), self.deadline)
        self.assertEqual("conflict", caught.exception.code)

    def test_missing_guid_is_not_missing_message(self):
        rows = [[b"*", b"1", b"FETCH", [b"UID", b"42"]]]
        with patch.object(self.port, "_command_groups", return_value=[[[b"*", b"UIDVALIDITY", b"12"]], rows]):
            with self.assertRaises(PimcampError) as caught:
                self.port.fetch("INBOX", "42", None, self.deadline)
        self.assertEqual("backend_unavailable", caught.exception.code)

    def test_paging_uses_current_uid_window_and_skips_disappeared_entries(self):
        metadata = {"INBOX": {"generation": "1", "messages": {"2": {"guid": "g2"}}}}
        summary = {"2": {"from": [], "subject": "current", "received_at": None, "unread": True}}
        with patch.object(self.port, "search", return_value=["1", "2", "10"]), \
             patch.object(self.port, "fetch_many", return_value=metadata), \
             patch.object(self.port, "envelopes", return_value=summary):
            rows, more = self.port.list_page("INBOX", 1, 2, self.deadline)
        self.assertTrue(more)
        self.assertEqual("g2", rows[0]["identity_evidence"]["key"])
        self.assertEqual(("INBOX", "2", "g2"), decode_handle(rows[0]["adapter_id"]))

    def test_generation_or_guid_change_during_envelope_fetch_refuses_page(self):
        original = {"INBOX": {"generation": "1", "messages": {"2": {"guid": "g2"}}}}
        for changed in ({"generation": "2", "messages": {"2": {"guid": "g2"}}},
                        {"generation": "1", "messages": {"2": {"guid": "replacement"}}}):
            with self.subTest(changed=changed), \
                 patch.object(self.port, "search", return_value=["2"]), \
                 patch.object(self.port, "fetch_many", side_effect=[original, {"INBOX": changed}]), \
                 patch.object(self.port, "envelopes", return_value={"2": {"subject": "current"}}):
                with self.assertRaises(PimcampError) as caught:
                    self.port.list_page("INBOX", 1, 10, self.deadline)
                self.assertEqual("conflict", caught.exception.code)

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
        with patch("pimcamp.himalaya.subprocess.Popen", side_effect=OSError("fixture unavailable")) as spawn:
            with self.assertRaises(PimcampError):
                DovecotIdentity(adapter._run_json).envelopes("INBOX", ["1", "2"], self.deadline)
        command = spawn.call_args.args[0]
        self.assertEqual(["imap", "fetch", "1,2", "--mailbox", "INBOX", "--envelope", "--flags"], command[-7:])
        for context in ({}, {"mail_account": True}):
            bad = dict(value, account="default") if context else value
            with self.assertRaises(PimcampError):
                _adapter(bad, "operations", **context)


class BatchedDovecotCase(unittest.TestCase):
    """Normalized command-port model; no CLI JSON or server capture invented."""

    def setUp(self):
        self.port = DovecotIdentity(lambda *a, **kw: self.fail("No real lower process"))
        self.deadline = time.monotonic() + 10
        self.calls = []
        self.boxes = {f"Folder-{i}": {"1": "same-guid"} for i in range(40)}
        self.boxes["INBOX"] = {str(i): f"guid-{i}" for i in range(1, 102)}
        self.raw_body = b"Subject: caf\xe9\r\n\r\n8bit body\xe9"

        def groups(commands, deadline):
            self.assertEqual(self.deadline, deadline)
            self.calls.append(("raw", commands))
            folder = None
            result = []
            for command in commands:
                if command.startswith("LIST "):
                    result.append([[b"*", b"LIST", [], b"/", name.encode()] for name in self.boxes])
                elif command.startswith("EXAMINE "):
                    folder = json.loads(command.removeprefix("EXAMINE "))
                    result.append([[b"*", b"UIDVALIDITY", b"55"]])
                elif command.startswith("UID SEARCH "):
                    query = command.removeprefix("UID SEARCH ")
                    guid = None if query == "ALL" else json.loads(query.removeprefix("X-GUID "))
                    uids = [uid.encode() for uid, key in self.boxes[folder].items() if guid is None or key == guid]
                    result.append([[b"*", b"SEARCH", *uids]])
                elif command.startswith("UID FETCH "):
                    if "BODY.PEEK[]" in command:
                        # Model the inspected lower's lossy conversion. This is
                        # a grammar example only, never a fake lower response.
                        raw = (b'* 1 FETCH (UID 1 X-GUID "guid-1" BODY[] {' +
                               str(len(self.raw_body)).encode() + b'}\r\n' + self.raw_body +
                               b')\r\npc0 OK done\r\n')
                        return parse_response(raw.decode("utf-8", errors="replace").encode(), ["pc0"], grouped=True)
                    self.assertNotIn("BODY", command)  # Listing never handles raw header literals.
                    uids = command.split()[2].split(",")
                    result.append([[b"*", b"1", b"FETCH", [b"UID", uid.encode(), b"X-GUID", self.boxes[folder][uid].encode()]]
                                   for uid in uids if uid in self.boxes[folder]])
                else:
                    self.fail("Unexpected command")
            return result

        def envelopes(folder, uids, deadline):
            self.assertEqual(self.deadline, deadline)
            self.calls.append(("envelopes", uids))
            return {uid: {"subject": "caf\ufffd" if uid == "100" else f"message-{uid}",
                          "from": [], "received_at": None, "unread": True} for uid in uids}

        self.groups = groups
        for name, function in (("_command_groups", groups), ("envelopes", envelopes)):
            patcher = patch.object(self.port, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_large_page_is_four_lower_calls_and_next_page_remains_available(self):
        rows, more = self.port.list_page("INBOX", 1, 100, self.deadline)
        self.assertEqual(100, len(rows))
        self.assertTrue(more)
        self.assertEqual("caf\ufffd", rows[1]["subject"])
        self.assertEqual(["raw", "raw", "envelopes", "raw"], [kind for kind, _ in self.calls])
        self.calls.clear()
        rows, more = self.port.list_page("INBOX", 2, 100, self.deadline)
        self.assertEqual(["message-1"], [row["subject"] for row in rows])
        self.assertFalse(more)
        self.assertEqual(4, len(self.calls))

    def test_many_folders_and_duplicate_matches_use_three_lower_calls(self):
        result = self.port.resolve(evidence("same-guid"), self.deadline)
        self.assertEqual(40, len(result["matches"]))
        self.assertEqual(3, len(self.calls))
        folders = {decode_handle(row["adapter_id"])[0] for row in result["matches"]}
        self.assertEqual({f"Folder-{i}" for i in range(40)}, folders)
        self.assertEqual(82, len(self.calls[1][1]))
        self.assertEqual(80, len(self.calls[2][1]))

    def test_failed_folder_in_batch_cannot_establish_uniqueness(self):
        def incomplete(commands, deadline):
            result = self.groups(commands, deadline)
            if commands[0].startswith("EXAMINE"):
                result[1] = []
            return result
        with patch.object(self.port, "_command_groups", incomplete):
            with self.assertRaises(PimcampError) as caught:
                self.port.resolve(evidence("same-guid"), self.deadline)
        self.assertEqual("backend_unavailable", caught.exception.code)

    def test_lossy_body_is_message_local_and_does_not_break_listing(self):
        with self.assertRaises(PimcampError) as caught:
            self.port.read("INBOX", "1", "guid-1", self.deadline)
        self.assertEqual("backend_unavailable", caught.exception.code)
        self.calls.clear()
        rows, more = self.port.list_page("INBOX", 1, 100, self.deadline)
        self.assertEqual(100, len(rows))
        self.assertTrue(more)
        self.assertEqual(4, len(self.calls))


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

    def test_batch_groups_preserve_folder_attribution_and_reject_bad_or_reordered_tags(self):
        raw = (b'* OK [UIDVALIDITY 12] first\r\npc0 OK examined\r\n'
               b'* SEARCH 1\r\npc1 OK searched\r\n'
               b'* OK [UIDVALIDITY 90] second\r\npc2 OK examined\r\n'
               b'* SEARCH 7 9\r\npc3 OK searched\r\n')
        groups = parse_response(raw, ["pc0", "pc1", "pc2", "pc3"], grouped=True)
        self.assertEqual([[b"*", b"UIDVALIDITY", b"12"]], groups[0])
        self.assertEqual([[b"*", b"SEARCH", b"1"]], groups[1])
        self.assertEqual([[b"*", b"SEARCH", b"7", b"9"]], groups[3])
        for bad in (raw.replace(b"pc2 OK", b"pc2 NO"), raw.replace(b"pc0 OK", b"pc2 OK", 1)):
            with self.assertRaises(PimcampError):
                parse_response(bad, ["pc0", "pc1", "pc2", "pc3"], grouped=True)

    def test_literal_mailbox_and_quoted_escapes(self):
        rows = parse_response(b'* LIST () "/" {13}\r\nFolder (name)\r\npc0 OK done\r\n', ["pc0"])
        self.assertEqual(b"Folder (name)", rows[0][4])


if __name__ == "__main__":
    unittest.main()
