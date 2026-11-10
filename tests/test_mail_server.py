"""Real loopback WSGI requests with isolated Pimcamp private-port fixtures."""

from contextlib import redirect_stderr
from http.client import HTTPConnection
from io import StringIO
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest

from tests import test_mail_v2 as fixtures
from pimcamp.mail_server import create_application, create_server
from pimcamp.config import load


class MailServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MailV2Case("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def request(self, server, method, path, headers=None, body=None):
        # Exercise the network-to-WSGI boundary, not app.handle directly.
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        connection = HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_link_session_read_and_activity_through_loopback_send_nothing(self):
        locator, _ = self.fixture.linked_locator()
        log = StringIO()
        with create_server(self.fixture.app(), 0) as server, redirect_stderr(log):
            self.assertEqual("127.0.0.1", server.server_address[0])
            status, _, _ = self.request(server, "GET", f"/mail/m/{locator}")
            self.assertEqual(403, status)
            status, _, _ = self.request(server, "GET", f"/mail/api/v2/messages/{locator}",
                                        {"X-Test-Principal": "human-operator"})
            self.assertEqual(401, status)
            headers = {"Origin": fixtures.ORIGIN, "X-Test-Principal": "human-operator"}
            status, response_headers, body = self.request(
                server, "POST", "/mail/api/session", headers, "{}"
            )
            self.assertEqual(200, status)
            session = json.loads(body)
            headers["Cookie"] = response_headers["Set-Cookie"].split(";", 1)[0]
            headers["X-Pimcamp-Csrf"] = session["csrf_token"]
            status, _, _ = self.request(server, "GET", f"/mail/m/{locator}", headers)
            self.assertEqual(200, status)
            status, _, _ = self.request(server, "POST", "/mail/api/session/activity", headers, "{}")
            self.assertEqual(200, status)
            status, _, body = self.request(server, "GET", f"/mail/api/v2/messages/{locator}", headers)
            self.assertEqual(200, status)
            self.assertEqual("account-a", json.loads(body)["account_id"])
        self.assertEqual([], self.fixture.calls_for("send"))
        self.assertEqual("", log.getvalue())

    def test_listener_does_not_supply_identity_or_account_grants(self):
        with create_server(self.fixture.app(authorized_accounts=[]), 0) as server:
            for headers in ({"Origin": fixtures.ORIGIN}, {
                "Origin": fixtures.ORIGIN, "X-Test-Principal": "human-operator"
            }):
                status, _, _ = self.request(server, "POST", "/mail/api/session", headers, "{}")
                self.assertEqual(403, status)
        self.assertEqual([], self.fixture.calls_for("send"))

    def production_app(self):
        config = json.loads(self.fixture.config_path.read_text())
        config["mail_principals"] = {"operator@example.test": self.fixture.account_ids}
        self.fixture.config_path.write_text(json.dumps(config))
        return create_application(load(self.fixture.config_path))

    def test_real_composition_uses_only_serve_assertion_and_configured_accounts(self):
        self.fixture.account_ids = ["account-a", "account-b", "account-c", "account-imap"]
        self.fixture.write_config()
        locator, _ = self.fixture.linked_locator()
        app = self.production_app()
        with create_server(app, 0) as server:
            for assertion in ({}, {"X-Test-Principal": "human-operator"},
                              {"Tailscale-User-Login": "unmapped@example.test"}):
                status, _, _ = self.request(server, "POST", "/mail/api/session",
                                            {"Origin": fixtures.ORIGIN, **assertion}, "{}")
                self.assertEqual(403, status)
            headers = {"Origin": fixtures.ORIGIN, "Tailscale-User-Login": "operator@example.test"}
            status, response_headers, body = self.request(server, "POST", "/mail/api/session", headers, "{}")
            self.assertEqual(200, status)
            session = json.loads(body)
            self.assertEqual(set(self.fixture.account_ids), {a["account_id"] for a in session["accounts"]})
            headers["Cookie"] = response_headers["Set-Cookie"].split(";", 1)[0]
            headers["X-Pimcamp-Csrf"] = session["csrf_token"]
            for path in (f"/mail/m/{locator}", "/mail/assets/mail-app.js", f"/mail/api/v2/messages/{locator}"):
                self.assertEqual(200, self.request(server, "GET", path, headers)[0])
            self.assertEqual(204, self.request(server, "POST", "/mail/api/session/logout", headers, "{}")[0])
            self.assertEqual(401, self.request(server, "GET", f"/mail/api/v2/messages/{locator}", headers)[0])
        config = json.loads(self.fixture.config_path.read_text())
        del config["mail_principals"]
        self.fixture.config_path.write_text(json.dumps(config))
        empty = create_application(load(self.fixture.config_path))
        self.assertEqual(403, empty.handle("POST", "/mail/api/session", headers, "{}").status)
        self.assertEqual([], self.fixture.calls_for("send"))

    def test_real_composition_keeps_accepted_timeout_unknown_without_resubmission(self):
        locator, _ = self.fixture.linked_locator()
        config = json.loads(self.fixture.config_path.read_text())
        config["adapter_wait_seconds"] = 0.5
        self.fixture.config_path.write_text(json.dumps(config))
        state = json.loads(self.fixture.adapter_state.read_text())
        state["behavior"] = {"send": "accepted_then_timeout"}
        self.fixture.adapter_state.write_text(json.dumps(state))
        app = self.production_app()
        headers = {"origin": fixtures.ORIGIN, "tailscale-user-login": "operator@example.test"}
        session = app.handle("POST", "/mail/api/session", headers, "{}")
        headers["cookie"] = dict(session.headers)["Set-Cookie"].split(";", 1)[0]
        headers["x-pimcamp-csrf"] = json.loads(session.body)["csrf_token"]
        prepared = app.handle("POST", "/mail/api/v2/replies/prepare", headers,
                              json.dumps({"locator": locator, "body_text": "Reviewed reply"}))
        draft = json.loads(prepared.body)
        reply = draft["reply"]
        submit = {"account_id": "account-a", "locator": locator,
                  "from": {k: reply["from_choices"][0][k] for k in ("name", "address")},
                  **{k: reply[k] for k in ("to", "cc", "bcc", "subject", "body_text")}}
        route = f'/mail/api/v2/replies/{draft["operation_id"]}/submit'
        self.assertEqual(409, app.handle("POST", route, headers, json.dumps(submit)).status)
        status_route = f'/mail/api/v2/accounts/account-a/send-status/{draft["operation_id"]}'
        status = json.loads(app.handle("GET", status_route, headers).body)
        self.assertEqual("unknown", status["status"])
        self.assertFalse(status["retry_eligible"])
        self.assertEqual(409, app.handle("POST", route, headers, json.dumps(submit)).status)
        self.assertEqual(1, json.loads(self.fixture.adapter_state.read_text())["accepted_submissions"])
        self.assertEqual(1, len(self.fixture.calls_for("send")))

    def test_executable_starts_only_scratch_loopback_composition(self):
        self.production_app()  # Writes only this fixture's approved mapping.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/pimcamp-mail"),
            "--config", str(self.fixture.config_path), "--port", str(port),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while True:
                connection = HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    connection.request("POST", "/mail/api/session", body="{}", headers={
                        "Origin": fixtures.ORIGIN, "Tailscale-User-Login": "operator@example.test",
                    })
                    response = connection.getresponse()
                    self.assertEqual(200, response.status)
                    self.assertEqual("operator@example.test", json.loads(response.read())["principal_id"])
                    break
                except ConnectionRefusedError:
                    self.assertIsNone(process.poll())
                    if time.monotonic() >= deadline:
                        self.fail("Isolated mail runner did not open its loopback listener.")
                    time.sleep(0.01)
                finally:
                    connection.close()
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode)
        self.assertEqual(b"", stdout)
        self.assertEqual(b"", stderr)
        self.assertEqual([], self.fixture.calls_for("send"))


if __name__ == "__main__":
    unittest.main()
