"""Isolated tests for the additive v2 agent and browser backend surface."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
EXECUTABLE = ROOT / "pimcamp"
PORT_ADAPTER = ROOT / "tests" / "support" / "operations_adapter.py"
ORIGIN = "https://mail.example.test"


def identity(key: str) -> dict[str, str | None]:
    return {
        "namespace": "fixture/account-message",
        "key": key,
        "continuity": "mapped",
        "backend": "fixture",
        "folder": "INBOX",
        "generation": "fixture-generation-1",
    }


def message(adapter_id: str, key: str | None = "stable-message-1") -> dict[str, object]:
    result: dict[str, object] = {
        "adapter_id": adapter_id,
        "from": [{"name": "Source Sender", "address": "source@example.test"}],
        "to": [{"name": None, "address": "owner@example.test"}],
        "cc": [],
        "reply_to": [
            {"name": None, "address": "reply-one@example.test"},
            {"name": None, "address": "reply-two@example.test"},
        ],
        "subject": "Re: Quarterly review",
        "received_at": "2026-10-20T12:00:00Z",
        "sent_at": "2026-10-20T12:00:00Z",
        "unread": True,
        "body_parts": [
            {"media_type": "text/html", "content_utf8": "<p>Hello</p><script>send()</script><img src='https://remote.test/x'>"}
        ],
        "threading": {"message_id": "<source@example.test>", "references": []},
    }
    if key is not None:
        result["identity_evidence"] = identity(key)
    return result


class FixtureIdentityVerifier:
    def authenticate(self, headers: dict[str, str]) -> str | None:
        return headers.get("x-test-principal")


class MailV2Case(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.adapter_state = self.root / "adapter.json"
        self.calls = self.root / "calls.jsonl"
        self.calls.write_text("")
        self.state_path = self.root / "private" / "pimcamp.sqlite3"
        self.credential = "mail-v2-fixture-credential"
        self.account_ids = ["account-a"]
        self.write_adapter_state([message("lower-id-1")])
        self.write_config()

    def write_config(
        self,
        *,
        capability: str = "mapped",
        mail_origin: str | None = ORIGIN,
        selectable_from: list[dict[str, object]] | None = None,
        alias_discovery_state: str = "supported-complete",
    ) -> None:
        digest = hashlib.sha256(self.credential.encode()).hexdigest()
        command = [sys.executable, str(PORT_ADAPTER), str(self.adapter_state), str(self.calls)]
        if selectable_from is None:
            selectable_from = [
                {
                    "from": {"name": "Owner", "address": "owner@example.test"},
                    "provenance": "synthetic-fixture-authority",
                    "validity": "valid",
                },
                {
                    "from": {"name": "Alias", "address": "alias@example.test"},
                    "provenance": "synthetic-fixture-authority",
                    "validity": "valid",
                },
            ]
        accounts = {
            account_id: {
                "label": f"Fixture {account_id}",
                "operations_adapter": {"kind": "command", "command": command},
                "primary_from": {"name": "Owner", "address": "owner@example.test"},
                "selectable_from": selectable_from,
                "alias_discovery_state": alias_discovery_state,
                "identity_capability": capability,
            }
            for account_id in self.account_ids
        }
        config = {
            "credentials": {
                digest: {
                    "client_identity": "fixture-agent",
                    "grants": ["accounts", "account_info", "list_account", "issue_link"],
                    "account_ids": self.account_ids,
                }
            },
            "adapter_wait_seconds": 3,
            "state_path": str(self.state_path),
            "operations_adapter": {"kind": "command", "command": command},
            "observation_adapter": {"kind": "command", "command": command},
            "mail_accounts": accounts,
        }
        if mail_origin is not None:
            config["mail_origin"] = mail_origin
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(config))
        os.chmod(self.config_path, 0o600)

    def write_adapter_state(self, messages: list[dict[str, object]]) -> None:
        self.adapter_state.write_text(json.dumps({"messages": messages}))

    def calls_for(self, operation: str) -> list[dict[str, object]]:
        return [
            value
            for line in self.calls.read_text().splitlines()
            if (value := json.loads(line))["operation"] == operation
        ]

    def invoke(self, operation: str, input_value: dict[str, object]) -> dict[str, object]:
        envelope = {
            "contract_version": "pimcamp.v2",
            "client_credential": self.credential,
            "input": input_value,
        }
        environment = os.environ.copy()
        environment["PIMCAMP_CONFIG"] = str(self.config_path)
        result = subprocess.run(
            [str(EXECUTABLE), operation],
            input=json.dumps(envelope),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=5,
        )
        self.assertEqual(1, len(result.stdout.splitlines()), result.stderr)
        return json.loads(result.stdout)

    def app(self, *, clock=None, status_reconciler=None, authorized_accounts=None):
        from pimcamp.config import load
        from pimcamp.mail_http import MailHTTPApplication, SessionPolicy

        page_path = self.root / "index.html"
        page_path.write_text("<!doctype html><title>Private mail</title>")
        return MailHTTPApplication(
            load(self.config_path),
            FixtureIdentityVerifier(),
            lambda principal: (
                authorized_accounts
                if principal == "human-operator" and authorized_accounts is not None
                else self.account_ids if principal == "human-operator" else ()
            ),
            SessionPolicy(),
            page_path=page_path,
            status_reconciler=status_reconciler,
            clock=clock or __import__("time").time,
        )

    def linked_locator(self) -> tuple[str, str]:
        listed = self.invoke("list_account", {"account_id": "account-a", "limit": 20})
        self.assertIn("result", listed, listed)
        self.assertEqual("pimcamp.v2", listed["contract_version"])
        source_ref = listed["result"]["messages"][0]["source_ref"]
        linked = self.invoke("issue_link", {"account_id": "account-a", "source_ref": source_ref})
        return linked["result"]["locator"], linked["result"]["url"]

    def authenticated_headers(self, cookie: str, csrf: str | None = None) -> dict[str, str]:
        headers = {
            "x-test-principal": "human-operator",
            "cookie": cookie,
        }
        if csrf is not None:
            headers["x-pimcamp-csrf"] = csrf
        return headers

    def establish_session(self, app) -> tuple[str, str]:
        cookie, csrf, _ = self.establish_session_details(app)
        return cookie, csrf

    def establish_session_details(self, app) -> tuple[str, str, dict[str, object]]:
        from pimcamp.jsonio import loads_one

        response = app.handle(
            "POST",
            "/mail/api/session",
            {"origin": ORIGIN, "x-test-principal": "human-operator"},
            b"{}",
        )
        self.assertEqual(200, response.status)
        cookie_header = dict(response.headers)["Set-Cookie"]
        cookie = cookie_header.split(";", 1)[0]
        session = loads_one(response.body)
        self.assertEqual("human-operator", session["principal_id"])
        csrf = session["csrf_token"]
        return cookie, csrf, session

    @staticmethod
    def rfc3339_utc(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    def test_v2_accounts_and_account_info_are_explicitly_account_scoped(self) -> None:
        listed = self.invoke("accounts", {})
        self.assertEqual([{"account_id": "account-a", "label": "Fixture account-a"}], listed["result"]["accounts"])
        info = self.invoke("account_info", {"account_id": "account-a"})
        result = info["result"]
        self.assertEqual("mapped", result["identity_capability"])
        self.assertEqual("owner@example.test", result["primary_from"]["address"])
        self.assertEqual(2, len(result["selectable_from"]))
        denied = self.invoke("account_info", {"account_id": "account-other"})
        self.assertEqual("permission_denied", denied["code"])
        self.assertEqual(0, len(self.calls_for("list")))

    def test_issue_link_requires_authoritative_account_source_and_returns_exact_locator(self) -> None:
        self.account_ids = ["account-a", "account-b"]
        self.write_config()
        locator, url = self.linked_locator()
        self.assertTrue(locator)
        self.assertEqual(f"{ORIGIN}/mail/m/{locator}", url)
        with sqlite3.connect(self.state_path) as database:
            v1_stamp = database.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            mail_stamp = database.execute(
                "SELECT value FROM mail_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual("pimcamp-state-v1", v1_stamp)
        self.assertEqual("pimcamp-mail-v2", mail_stamp)
        listed = self.invoke("list_account", {"account_id": "account-a", "limit": 20})
        source_ref = listed["result"]["messages"][0]["source_ref"]
        mismatch = self.invoke("issue_link", {"account_id": "account-b", "source_ref": source_ref})
        self.assertEqual({"status": "cannot_link", "reason": "account_mismatch"}, mismatch["result"])
        self.assertNotIn("url", mismatch["result"])
        listed_b = self.invoke("list_account", {"account_id": "account-b", "limit": 20})
        linked_b = self.invoke(
            "issue_link",
            {"account_id": "account-b", "source_ref": listed_b["result"]["messages"][0]["source_ref"]},
        )["result"]
        self.assertNotEqual(locator, linked_b["locator"])

    def test_cannot_link_is_url_free_without_stable_identity_or_origin(self) -> None:
        self.write_adapter_state([message("lower-id-1", key=None)])
        listed = self.invoke("list_account", {"account_id": "account-a", "limit": 20})
        source_ref = listed["result"]["messages"][0]["source_ref"]
        result = self.invoke("issue_link", {"account_id": "account-a", "source_ref": source_ref})["result"]
        self.assertEqual({"status": "cannot_link", "reason": "identity_evidence_missing"}, result)
        self.assertNotIn("url", result)

        self.write_adapter_state([message("lower-id-1")])
        self.write_config(mail_origin=None)
        listed = self.invoke("list_account", {"account_id": "account-a", "limit": 20})
        result = self.invoke(
            "issue_link",
            {"account_id": "account-a", "source_ref": listed["result"]["messages"][0]["source_ref"]},
        )["result"]
        self.assertEqual({"status": "cannot_link", "reason": "mail_origin_unconfigured"}, result)
        self.assertNotIn("url", result)

    def test_ambiguous_authoritative_identity_returns_cannot_link_without_url(self) -> None:
        self.write_adapter_state([message("lower-id-1"), message("lower-id-duplicate")])
        listed = self.invoke("list_account", {"account_id": "account-a", "limit": 20})
        source_ref = listed["result"]["messages"][0]["source_ref"]
        result = self.invoke(
            "issue_link", {"account_id": "account-a", "source_ref": source_ref}
        )["result"]
        self.assertEqual({"status": "cannot_link", "reason": "ambiguous_identity"}, result)
        self.assertNotIn("url", result)

    def test_locator_survives_fixture_move_and_http_page_and_read_are_private(self) -> None:
        locator, url = self.linked_locator()
        moved = message("lower-id-moved")
        state = json.loads(self.adapter_state.read_text())
        state["messages"] = [moved]
        self.adapter_state.write_text(json.dumps(state))

        app = self.app()
        page = app.handle("GET", f"/mail/m/{locator}", {})
        self.assertEqual(403, page.status)
        page = app.handle(
            "GET", f"/mail/m/{locator}", {"x-test-principal": "human-operator"}
        )
        self.assertEqual(200, page.status)
        self.assertIn("no-store", dict(page.headers)["Cache-Control"])
        self.assertIn("Content-Security-Policy", dict(page.headers))

        cookie, _ = self.establish_session(app)
        headers = self.authenticated_headers(cookie)
        response = app.handle("GET", f"/mail/api/v2/messages/{locator}", headers)
        self.assertEqual(200, response.status)
        body = json.loads(response.body)
        self.assertEqual("account-a", body["account_id"])
        self.assertEqual("Fixture account-a", body["account_label"])
        self.assertEqual("lower-id-moved", self.calls_for("read")[-1]["adapter_id"])
        rendered = body["body_parts"][0]["content_utf8"]
        self.assertIn("Hello", rendered)
        self.assertNotIn("send()", rendered)
        self.assertNotIn("remote.test", rendered)
        self.assertEqual("Re: Quarterly review", body["source"]["subject"])
        self.assertEqual(url, f"{ORIGIN}/mail/m/{locator}")

    def test_private_mail_assets_are_allowlisted_and_served_from_the_page_directory(self) -> None:
        assets = {
            "mail.css": ("text/css; charset=utf-8", b"body { color: black; }"),
            "mail-client.js": ("text/javascript; charset=utf-8", b"export const client = true;"),
            "mail-drafts.js": ("text/javascript; charset=utf-8", b"export const drafts = true;"),
            "mail-text.js": ("text/javascript; charset=utf-8", b"export const text = true;"),
            "mail-app.js": ("text/javascript; charset=utf-8", b"export const app = true;"),
        }
        for name, (_, body) in assets.items():
            (self.root / name).write_bytes(body)
        app = self.app()

        denied = app.handle("GET", "/mail/assets/mail.css", {})
        self.assertEqual(403, denied.status)
        for name, (content_type, body) in assets.items():
            with self.subTest(asset=name):
                response = app.handle(
                    "GET",
                    f"/mail/assets/{name}",
                    {"x-test-principal": "human-operator"},
                )
                self.assertEqual(200, response.status)
                self.assertEqual(body, response.body)
                self.assertEqual(content_type, dict(response.headers)["Content-Type"])
                self.assertEqual("no-store", dict(response.headers)["Cache-Control"])

        unknown = app.handle(
            "GET", "/mail/assets/config.json", {"x-test-principal": "human-operator"}
        )
        self.assertEqual(404, unknown.status)
        traversal = app.handle(
            "GET",
            "/mail/assets/%2e%2e%2fconfig.json",
            {"x-test-principal": "human-operator"},
        )
        self.assertEqual(400, traversal.status)

    def test_locator_identity_capability_change_fails_as_conflict(self) -> None:
        locator, _ = self.linked_locator()
        self.write_config(capability="native")
        app = self.app()
        cookie, _ = self.establish_session(app)
        response = app.handle(
            "GET",
            f"/mail/api/v2/messages/{locator}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual(409, response.status)
        self.assertEqual(0, len(self.calls_for("read")))

    def test_browser_reader_and_status_enforce_the_principals_account_allowlist(self) -> None:
        self.account_ids = ["account-a", "account-b"]
        self.write_config()
        listed = self.invoke("list_account", {"account_id": "account-b", "limit": 20})
        source_ref = listed["result"]["messages"][0]["source_ref"]
        linked = self.invoke(
            "issue_link", {"account_id": "account-b", "source_ref": source_ref}
        )["result"]
        app = self.app(authorized_accounts=["account-a"])
        page = app.handle(
            "GET",
            f"/mail/m/{linked['locator']}",
            {"x-test-principal": "human-operator"},
        )
        self.assertEqual(403, page.status)

        cookie, _ = self.establish_session(app)
        headers = self.authenticated_headers(cookie)
        response = app.handle(
            "GET", f"/mail/api/v2/messages/{linked['locator']}", headers
        )
        self.assertEqual(403, response.status)
        status = app.handle(
            "GET",
            "/mail/api/v2/accounts/account-b/send-status/00000000-0000-4000-8000-000000000000",
            headers,
        )
        self.assertEqual(403, status.status)
        self.assertEqual(0, len(self.calls_for("read")))
        self.assertEqual(0, len(self.calls_for("send")))

    def test_browser_prepare_explicit_submit_replay_and_read_only_status(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "Reviewed text"}),
        )
        self.assertEqual(200, prepared.status)
        draft = loads_one(prepared.body)
        self.assertEqual(
            ["reply-one@example.test", "reply-two@example.test"],
            [item["address"] for item in draft["source"]["reply_to"]],
        )
        reply = draft["reply"]
        self.assertEqual(
            ["reply-one@example.test", "reply-two@example.test"],
            [item["address"] for item in reply["to"]],
        )
        self.assertIsNone(reply["selected_from"])
        self.assertEqual(2, len(reply["from_choices"]))
        self.assertEqual(0, len(self.calls_for("send")))

        status_path = f"/mail/api/v2/accounts/account-a/send-status/{draft['operation_id']}"
        before_status = self.state_path.read_bytes()
        status = app.handle("GET", status_path, self.authenticated_headers(cookie))
        self.assertEqual("not_submitted", loads_one(status.body)["status"])
        self.assertTrue(loads_one(status.body)["retry_eligible"])
        self.assertEqual(before_status, self.state_path.read_bytes())

        submit = {
            "account_id": "account-a",
            "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": reply["to"],
            "cc": [],
            "bcc": [],
            "subject": reply["subject"],
            "body_text": reply["body_text"],
        }
        sent = app.handle(
            "POST",
            f"/mail/api/v2/replies/{draft['operation_id']}/submit",
            headers,
            json.dumps(submit),
        )
        self.assertEqual(200, sent.status, sent.body)
        self.assertEqual("sent", loads_one(sent.body)["status"])
        self.assertFalse(loads_one(sent.body).get("retry_eligible", False))
        self.assertEqual(1, len(self.calls_for("send")))
        self.assertEqual("owner@example.test", self.calls_for("send")[0]["sender"]["address"])

        repeated = app.handle(
            "POST",
            f"/mail/api/v2/replies/{draft['operation_id']}/submit",
            headers,
            json.dumps(submit),
        )
        self.assertEqual(200, repeated.status)
        self.assertEqual(1, len(self.calls_for("send")))
        before_status = self.state_path.read_bytes()
        status = app.handle("GET", status_path, self.authenticated_headers(cookie))
        self.assertEqual("sent", loads_one(status.body)["status"])
        self.assertFalse(loads_one(status.body)["retry_eligible"])
        self.assertEqual(before_status, self.state_path.read_bytes())

    def test_primary_only_account_preselects_its_single_valid_from(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        sender = {
            "name": "Owner",
            "address": "owner@example.test",
            "provenance": "synthetic-fixture-authority",
            "validity": "valid",
        }
        self.write_config(
            selectable_from=[
                {
                    "from": {"name": "Owner", "address": "owner@example.test"},
                    "provenance": sender["provenance"],
                    "validity": sender["validity"],
                }
            ]
        )
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN

        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "Primary only"}),
        )
        self.assertEqual(200, prepared.status)
        reply = loads_one(prepared.body)["reply"]
        self.assertEqual([sender], reply["from_choices"])
        self.assertEqual(sender, reply["selected_from"])
        self.assertEqual(0, len(self.calls_for("send")))

    def test_unknown_alias_discovery_excludes_unverified_from_choices(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        self.write_config(
            selectable_from=[
                {
                    "from": {"name": "Owner", "address": "owner@example.test"},
                    "provenance": "synthetic-fixture-authority",
                    "validity": "valid",
                },
                {
                    "from": {"name": "Possible", "address": "possible@example.test"},
                    "provenance": "synthetic-fixture-authority",
                    "validity": "unknown",
                },
                {
                    "from": {"name": "Withdrawn", "address": "withdrawn@example.test"},
                    "provenance": "synthetic-fixture-authority",
                    "validity": "invalid",
                },
            ],
            alias_discovery_state="unknown",
        )
        info = self.invoke("account_info", {"account_id": "account-a"})["result"]
        self.assertEqual("unknown", info["alias_discovery_state"])
        self.assertEqual(
            ["owner@example.test"],
            [item["address"] for item in info["selectable_from"]],
        )

        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "Known authority only"}),
        )
        self.assertEqual(200, prepared.status)
        reply = loads_one(prepared.body)["reply"]
        self.assertEqual(
            ["owner@example.test"],
            [item["address"] for item in reply["from_choices"]],
        )
        self.assertEqual("owner@example.test", reply["selected_from"]["address"])

    def test_withdrawn_from_after_prepare_is_denied_without_substitution(self) -> None:
        from pimcamp.config import load
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "Review this sender"}),
        )
        self.assertEqual(200, prepared.status)
        draft = loads_one(prepared.body)
        alias = {"name": "Alias", "address": "alias@example.test"}
        self.assertIn(
            alias["address"],
            [item["address"] for item in draft["reply"]["from_choices"]],
        )

        self.write_config(
            selectable_from=[
                {
                    "from": {"name": "Owner", "address": "owner@example.test"},
                    "provenance": "synthetic-fixture-authority",
                    "validity": "valid",
                }
            ]
        )
        app.config = load(self.config_path)
        submit = {
            "account_id": "account-a",
            "locator": locator,
            "from": alias,
            "to": draft["reply"]["to"],
            "cc": [],
            "bcc": [],
            "subject": draft["reply"]["subject"],
            "body_text": draft["reply"]["body_text"],
        }
        rejected = app.handle(
            "POST",
            f"/mail/api/v2/replies/{draft['operation_id']}/submit",
            headers,
            json.dumps(submit),
        )
        self.assertEqual(403, rejected.status)
        self.assertEqual("permission_denied", loads_one(rejected.body)["code"])
        self.assertEqual(0, len(self.calls_for("send")))

        status = app.handle(
            "GET",
            f"/mail/api/v2/accounts/account-a/send-status/{draft['operation_id']}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual("not_submitted", loads_one(status.body)["status"])
        self.assertTrue(loads_one(status.body)["retry_eligible"])
        self.assertEqual(0, len(self.calls_for("send")))

    def test_status_keeps_expired_unbegun_claim_pending_and_nonretryable(self) -> None:
        from pimcamp.jsonio import loads_one
        from pimcamp.mail_receipts import MailReplyReceipts
        from pimcamp.state import State

        locator, _ = self.linked_locator()
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "waiting preflight"}),
        )
        operation_id = loads_one(prepared.body)["operation_id"]
        state = State(self.state_path)
        try:
            claim = MailReplyReceipts(state, ensure_schema=False).claim(
                operation_id,
                "human-operator",
                "account-a",
                locator,
                "fixture-request-digest",
                time.time() - 1,
            )
        finally:
            state.close()
        self.assertEqual("claimant", claim.state)

        status_path = f"/mail/api/v2/accounts/account-a/send-status/{operation_id}"
        before_status = self.state_path.read_bytes()
        response = app.handle("GET", status_path, self.authenticated_headers(cookie))
        result = loads_one(response.body)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["retry_eligible"])
        self.assertEqual(before_status, self.state_path.read_bytes())
        self.assertEqual(0, len(self.calls_for("send")))

    def test_browser_submit_requires_same_origin_csrf_and_prepared_account_binding(self) -> None:
        locator, _ = self.linked_locator()
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared_response = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "draft"}),
        )
        prepared = json.loads(prepared_response.body)
        reply = prepared["reply"]
        submit = {
            "account_id": "account-a",
            "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": [{"name": None, "address": "reply@example.test"}],
            "cc": [],
            "bcc": [],
            "subject": reply["subject"],
            "body_text": "draft",
        }
        forged = dict(headers)
        forged.pop("x-pimcamp-csrf")
        response = app.handle(
            "POST",
            f"/mail/api/v2/replies/{prepared['operation_id']}/submit",
            forged,
            json.dumps(submit),
        )
        self.assertEqual(403, response.status)
        wrong_origin = dict(headers)
        wrong_origin["origin"] = "https://other.example.test"
        response = app.handle(
            "POST",
            f"/mail/api/v2/replies/{prepared['operation_id']}/submit",
            wrong_origin,
            json.dumps(submit),
        )
        self.assertEqual(403, response.status)
        self.assertEqual(0, len(self.calls_for("send")))

    def test_expired_mail_session_hides_reader_content(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        now = [1_000.0]
        app = self.app(clock=lambda: now[0])
        cookie, csrf, created = self.establish_session_details(app)
        self.assertEqual(
            self.rfc3339_utc(now[0] + 30 * 60), created["idle_deadline"]
        )
        self.assertEqual(
            self.rfc3339_utc(now[0] + 12 * 60 * 60),
            created["absolute_deadline"],
        )
        mutation_headers = self.authenticated_headers(cookie, csrf)
        mutation_headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            mutation_headers,
            json.dumps({"locator": locator, "body_text": "Deadline fixture"}),
        )
        self.assertEqual(200, prepared.status)
        operation_id = loads_one(prepared.body)["operation_id"]

        now[0] += 29 * 60
        readable = app.handle(
            "GET",
            f"/mail/api/v2/messages/{locator}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual(200, readable.status)
        reads_at_29_minutes = len(self.calls_for("read"))

        now[0] += 60
        response = app.handle(
            "GET",
            f"/mail/api/v2/messages/{locator}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual(401, response.status)
        error = loads_one(response.body)
        self.assertEqual("session_expired", error["code"])
        self.assertFalse(error["retryable"])
        self.assertEqual(
            {"contract_version", "code", "message", "retryable"}, set(error)
        )
        self.assertEqual(reads_at_29_minutes, len(self.calls_for("read")))

        submit = {
            "account_id": "account-a",
            "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": [{"name": None, "address": "reply-one@example.test"}],
            "cc": [],
            "bcc": [],
            "subject": "Re: Quarterly review",
            "body_text": "Deadline fixture",
        }
        expired_submit = app.handle(
            "POST",
            f"/mail/api/v2/replies/{operation_id}/submit",
            mutation_headers,
            json.dumps(submit),
        )
        self.assertEqual(401, expired_submit.status)
        self.assertEqual("session_expired", loads_one(expired_submit.body)["code"])
        self.assertEqual(0, len(self.calls_for("send")))

    def test_missing_mail_session_has_a_distinct_flat_renewal_error(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        app = self.app()
        response = app.handle(
            "GET",
            f"/mail/api/v2/messages/{locator}",
            {"x-test-principal": "human-operator"},
        )
        self.assertEqual(401, response.status)
        self.assertEqual(
            {
                "contract_version": "pimcamp.v2",
                "code": "session_expired",
                "retryable": False,
                "message": "The mail session has expired or is unavailable.",
            },
            loads_one(response.body),
        )

    def test_activity_renews_idle_deadline_but_status_poll_does_not(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        now = [1_000.0]
        app = self.app(clock=lambda: now[0])
        cookie, csrf, created = self.establish_session_details(app)
        created_at = now[0]
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "Idle session fixture"}),
        )
        self.assertEqual(200, prepared.status)
        operation_id = loads_one(prepared.body)["operation_id"]

        now[0] += 29 * 60
        wrong_origin = dict(headers, origin="https://other.example.test")
        rejected_origin = app.handle(
            "POST",
            "/mail/api/session/activity",
            wrong_origin,
            b"{}",
        )
        self.assertEqual(403, rejected_origin.status)
        self.assertEqual("permission_denied", loads_one(rejected_origin.body)["code"])
        wrong_csrf = dict(headers)
        wrong_csrf["x-pimcamp-csrf"] = "wrong"
        rejected_csrf = app.handle(
            "POST",
            "/mail/api/session/activity",
            wrong_csrf,
            b"{}",
        )
        self.assertEqual(403, rejected_csrf.status)
        self.assertEqual("permission_denied", loads_one(rejected_csrf.body)["code"])

        activity = app.handle(
            "POST",
            "/mail/api/session/activity",
            headers,
            b"{}",
        )
        self.assertEqual(200, activity.status)
        activity_result = loads_one(activity.body)
        self.assertEqual(
            self.rfc3339_utc(created_at + 59 * 60),
            activity_result["idle_deadline"],
        )
        self.assertEqual(
            created["absolute_deadline"], activity_result["absolute_deadline"]
        )

        now[0] += 28 * 60
        status = app.handle(
            "GET",
            f"/mail/api/v2/accounts/account-a/send-status/{operation_id}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual(200, status.status)
        self.assertEqual("not_submitted", loads_one(status.body)["status"])

        now[0] += 2 * 60
        expired = app.handle(
            "GET",
            f"/mail/api/v2/messages/{locator}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual(401, expired.status)
        self.assertEqual("session_expired", loads_one(expired.body)["code"])

    def test_absolute_mail_session_deadline_wins_despite_foreground_activity(self) -> None:
        from pimcamp.jsonio import loads_one

        now = [5_000.0]
        app = self.app(clock=lambda: now[0])
        cookie, csrf, created = self.establish_session_details(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        absolute_deadline = created["absolute_deadline"]

        for _ in range(24):
            now[0] += 29 * 60
            activity = app.handle(
                "POST",
                "/mail/api/session/activity",
                headers,
                b"{}",
            )
            self.assertEqual(200, activity.status)
            result = loads_one(activity.body)
            self.assertEqual(absolute_deadline, result["absolute_deadline"])
            self.assertEqual(
                self.rfc3339_utc(now[0] + 30 * 60), result["idle_deadline"]
            )

        now[0] = 5_000.0 + 12 * 60 * 60
        expired = app.handle(
            "POST",
            "/mail/api/session/activity",
            headers,
            b"{}",
        )
        self.assertEqual(401, expired.status)
        error = loads_one(expired.body)
        self.assertEqual("session_expired", error["code"])
        self.assertFalse(error["retryable"])

    def test_logout_invalidates_only_current_session_and_requires_origin_csrf(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        app = self.app()
        cookie_one, csrf_one = self.establish_session(app)
        cookie_two, _ = self.establish_session(app)
        headers_one = self.authenticated_headers(cookie_one, csrf_one)
        headers_one["origin"] = ORIGIN

        wrong_origin = dict(headers_one, origin="https://other.example.test")
        denied_origin = app.handle(
            "POST", "/mail/api/session/logout", wrong_origin, b"{}"
        )
        self.assertEqual(403, denied_origin.status)
        self.assertEqual("permission_denied", loads_one(denied_origin.body)["code"])

        wrong_csrf = dict(headers_one)
        wrong_csrf["x-pimcamp-csrf"] = "wrong"
        denied_csrf = app.handle(
            "POST", "/mail/api/session/logout", wrong_csrf, b"{}"
        )
        self.assertEqual(403, denied_csrf.status)
        self.assertEqual("permission_denied", loads_one(denied_csrf.body)["code"])

        logout = app.handle(
            "POST", "/mail/api/session/logout", headers_one, b"{}"
        )
        self.assertEqual(204, logout.status)
        self.assertEqual(b"", logout.body)
        self.assertIn("Max-Age=0", dict(logout.headers)["Set-Cookie"])
        self.assertEqual(
            401,
            app.handle(
                "GET",
                f"/mail/api/v2/messages/{locator}",
                self.authenticated_headers(cookie_one),
            ).status,
        )
        self.assertEqual(
            200,
            app.handle(
                "GET",
                f"/mail/api/v2/messages/{locator}",
                self.authenticated_headers(cookie_two),
            ).status,
        )
        self.assertEqual(0, len(self.calls_for("send")))

    def test_status_lookup_failure_uses_truthful_status_unavailable_error(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST",
            "/mail/api/v2/replies/prepare",
            headers,
            json.dumps({"locator": locator, "body_text": "Status fixture"}),
        )
        self.assertEqual(200, prepared.status)
        operation_id = loads_one(prepared.body)["operation_id"]
        self.state_path.rename(self.root / "unavailable.sqlite3")

        status = app.handle(
            "GET",
            f"/mail/api/v2/accounts/account-a/send-status/{operation_id}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual(503, status.status)
        error = loads_one(status.body)
        self.assertEqual("status_unavailable", error["code"])
        self.assertTrue(error["retryable"])

    def test_concurrent_explicit_submits_share_one_transport_claim(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        pause_file = self.root / "release-send"
        adapter_state = json.loads(self.adapter_state.read_text())
        adapter_state["behavior"] = {"send": "pause_success"}
        adapter_state["pause_file"] = str(pause_file)
        self.adapter_state.write_text(json.dumps(adapter_state))
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST", "/mail/api/v2/replies/prepare", headers,
            json.dumps({"locator": locator, "body_text": "one explicit send"}),
        )
        draft = loads_one(prepared.body)
        reply = draft["reply"]
        route = f"/mail/api/v2/replies/{draft['operation_id']}/submit"
        submit = {
            "account_id": "account-a", "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": reply["to"], "cc": [], "bcc": [],
            "subject": reply["subject"], "body_text": reply["body_text"],
        }
        responses = []
        first = threading.Thread(
            target=lambda: responses.append(
                app.handle("POST", route, headers, json.dumps(submit))
            )
        )
        first.start()
        deadline = time.monotonic() + 3
        while not self.calls_for("send") and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(1, len(self.calls_for("send")))
        second = app.handle("POST", route, headers, json.dumps(submit))
        self.assertEqual("pending", loads_one(second.body)["status"])
        self.assertEqual(1, len(self.calls_for("send")))
        pause_file.touch()
        first.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertEqual(1, len(responses))
        self.assertEqual("sent", loads_one(responses[0].body)["status"])
        self.assertEqual(1, len(self.calls_for("send")))

    def test_proved_pre_submission_failure_can_retry_same_operation_and_snapshot(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST", "/mail/api/v2/replies/prepare", headers,
            json.dumps({"locator": locator, "body_text": "retry this exact draft"}),
        )
        draft = loads_one(prepared.body)
        reply = draft["reply"]
        submit = {
            "account_id": "account-a", "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": reply["to"], "cc": [], "bcc": [],
            "subject": reply["subject"], "body_text": reply["body_text"],
        }
        self.write_adapter_state([])
        route = f"/mail/api/v2/replies/{draft['operation_id']}/submit"
        failed = app.handle("POST", route, headers, json.dumps(submit))
        self.assertEqual(404, failed.status)
        status_path = f"/mail/api/v2/accounts/account-a/send-status/{draft['operation_id']}"
        status = app.handle("GET", status_path, self.authenticated_headers(cookie))
        self.assertEqual("not_submitted", loads_one(status.body)["status"])
        self.assertTrue(loads_one(status.body)["retry_eligible"])
        self.assertEqual(0, len(self.calls_for("send")))

        self.write_adapter_state([message("lower-id-restored")])
        retry = app.handle("POST", route, headers, json.dumps(submit))
        self.assertEqual(200, retry.status, retry.body)
        self.assertEqual("sent", loads_one(retry.body)["status"])
        self.assertEqual(1, len(self.calls_for("send")))

    def test_unknown_send_outcome_is_not_retried_by_status_or_repeated_submit(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        state = json.loads(self.adapter_state.read_text())
        state["behavior"] = {"send": "lost_response"}
        self.adapter_state.write_text(json.dumps(state))
        app = self.app()
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST", "/mail/api/v2/replies/prepare", headers,
            json.dumps({"locator": locator, "body_text": "uncertain"}),
        )
        draft = loads_one(prepared.body)
        reply = draft["reply"]
        submit = {
            "account_id": "account-a", "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": reply["to"], "cc": [], "bcc": [], "subject": reply["subject"],
            "body_text": reply["body_text"],
        }
        path = f"/mail/api/v2/replies/{draft['operation_id']}/submit"
        failed = app.handle("POST", path, headers, json.dumps(submit))
        self.assertEqual(409, failed.status)
        status_path = f"/mail/api/v2/accounts/account-a/send-status/{draft['operation_id']}"
        status = app.handle("GET", status_path, self.authenticated_headers(cookie))
        self.assertEqual("unknown", loads_one(status.body)["status"])
        self.assertFalse(loads_one(status.body)["retry_eligible"])
        again = app.handle("POST", path, headers, json.dumps(submit))
        self.assertEqual(409, again.status)
        self.assertEqual(1, len(self.calls_for("send")))

    def test_status_can_use_only_injected_read_only_reconciliation(self) -> None:
        from pimcamp.jsonio import loads_one

        locator, _ = self.linked_locator()
        reconciled = []
        app = self.app(status_reconciler=lambda principal, account, operation: (reconciled.append((principal, account, operation)) or "sent"))
        cookie, csrf = self.establish_session(app)
        headers = self.authenticated_headers(cookie, csrf)
        headers["origin"] = ORIGIN
        prepared = app.handle(
            "POST", "/mail/api/v2/replies/prepare", headers,
            json.dumps({"locator": locator, "body_text": "draft"}),
        )
        operation_id = loads_one(prepared.body)["operation_id"]
        state = json.loads(self.adapter_state.read_text())
        state["behavior"] = {"send": "lost_response"}
        self.adapter_state.write_text(json.dumps(state))
        submit = {
            "account_id": "account-a", "locator": locator,
            "from": {"name": "Owner", "address": "owner@example.test"},
            "to": [{"name": None, "address": "reply@example.test"}],
            "cc": [], "bcc": [], "subject": "Re: Quarterly review", "body_text": "draft",
        }
        app.handle("POST", f"/mail/api/v2/replies/{operation_id}/submit", headers, json.dumps(submit))
        status = app.handle(
            "GET", f"/mail/api/v2/accounts/account-a/send-status/{operation_id}",
            self.authenticated_headers(cookie),
        )
        self.assertEqual("sent", loads_one(status.body)["status"])
        self.assertFalse(loads_one(status.body)["retry_eligible"])
        self.assertEqual([("human-operator", "account-a", operation_id)], reconciled)
        self.assertEqual(1, len(self.calls_for("send")))


if __name__ == "__main__":
    unittest.main()
