"""Real loopback WSGI requests with isolated Pimcamp private-port fixtures."""

from contextlib import redirect_stderr
from http.client import HTTPConnection
from io import StringIO
import json
import threading
import unittest

from tests import test_mail_v2 as fixtures
from pimcamp.mail_server import create_server


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


if __name__ == "__main__":
    unittest.main()
