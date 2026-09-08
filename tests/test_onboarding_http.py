"""HTTP handler checks without opening sockets or contacting any mail server."""
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pimcamp.onboarding_http import SetupHandler


class SetupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launch = Path(self.temp.name) / "launch.html"
        self.launch.write_text("fixture launch handoff")
        self.service = Mock()
        self.service.list_accounts.return_value = []
        self.server = SimpleNamespace(authority="127.0.0.1:33281", origin="http://127.0.0.1:33281",
            csrf="fixture-csrf", session="fixture-session", bootstrap="fixture-bootstrap",
            bootstrap_deadline=time.monotonic() + 300, bootstrap_lock=threading.Lock(),
            launch_file=self.launch, service=self.service,
            assets=Path(__file__).resolve().parents[1] / "ui/onboarding")

    def request(self, method="POST", path="/api/action", payload=None, headers=None, raw=None):
        handler = object.__new__(SetupHandler)
        handler.server, handler.path = self.server, path
        handler.headers = Message()
        defaults = {"Host": self.server.authority, "Origin": self.server.origin,
                    "Cookie": "pimcamp_setup=fixture-session", "X-Pimcamp-CSRF": "fixture-csrf",
                    "Content-Type": "application/json"}
        defaults.update(headers or {})
        raw = json.dumps(payload or {"action": "listAccounts"}).encode() if raw is None else raw
        defaults.setdefault("Content-Length", str(len(raw)))
        for key, value in defaults.items():
            if value is not None:
                handler.headers[key] = value
        handler.rfile = BytesIO(raw)
        captured = []
        handler.reply = lambda *args, **kwargs: captured.append((args, kwargs))
        getattr(handler, "do_" + method)()
        return captured[-1]

    def test_account_action_requires_cookie_origin_csrf_and_exact_host(self):
        for header in ("Cookie", "Origin", "X-Pimcamp-CSRF", "Host"):
            with self.subTest(header=header):
                response, _ = self.request(headers={header: "wrong"})
                self.assertEqual(response[0], 403)
        self.service.list_accounts.assert_not_called()

    def test_authorized_account_action_routes_to_service(self):
        response, _ = self.request()
        self.assertEqual(response, (200, []))
        self.service.list_accounts.assert_called_once_with()

    def test_google_app_import_requires_owner_session_and_hides_raw_errors(self):
        payload = {"action": "configureGoogleApplication", "credentialsJson": "fixture-secret-json"}
        for header in ("Cookie", "Origin", "X-Pimcamp-CSRF", "Host"):
            response, _ = self.request(payload=payload, headers={header: "wrong"})
            self.assertEqual(response[0], 403)
        self.service.configure_google_application.assert_not_called()
        self.service.configure_google_application.return_value = {"configured": True, "clientId": "public-id"}
        response, _ = self.request(payload=payload)
        self.assertEqual(response, (200, {"configured": True, "clientId": "public-id"}))
        self.service.configure_google_application.assert_called_once_with("fixture-secret-json")
        self.service.configure_google_application.side_effect = OSError("fixture-secret-json")
        response, _ = self.request(payload=payload)
        self.assertEqual(response[0], 500)
        self.assertNotIn("fixture-secret-json", str(response))

    def test_google_application_status_is_an_authenticated_action(self):
        self.service.google_application_status.return_value = {"configured": False, "helperAvailable": True}
        response, _ = self.request(payload={"action": "googleApplicationStatus"})
        self.assertEqual(response[0], 200)
        self.service.google_application_status.assert_called_once_with()

    def test_oauth_callback_uses_provider_state_without_session_cookie(self):
        path = "/oauth/google/callback?state=fixture-state&code=fixture-code"
        response, extra = self.request(method="GET", path=path, headers={"Cookie": None, "Origin": None})
        self.assertEqual(response[0], 303)
        self.service.complete_oauth.assert_called_once_with(self.server.origin + path)
        self.assertEqual(dict(extra["extra"])["Location"], "/oauth/complete")
        self.assertNotIn("fixture-code", str(response) + str(extra))

    def test_oauth_callback_requires_local_host_and_hides_failures(self):
        path = "/oauth/google/callback?state=fixture-state&code=fixture-code"
        response, _ = self.request(method="GET", path=path, headers={"Host": "evil.example"})
        self.assertEqual(response[0], 403)
        self.service.complete_oauth.assert_not_called()
        self.service.complete_oauth.side_effect = ValueError("private provider diagnostic")
        response, _ = self.request(method="GET", path=path, headers={"Cookie": None})
        self.assertEqual(response[0], 400)
        self.assertNotIn("private provider", str(response))

    def test_oauth_start_requires_the_same_csrf_protection_as_account_writes(self):
        response, _ = self.request(payload={"action": "beginOAuth", "setupId": "fixture-setup"},
                                   headers={"X-Pimcamp-CSRF": None})
        self.assertEqual(response[0], 403)
        self.service.begin_oauth.assert_not_called()

    def test_invalid_body_and_unrecognized_actions_fail_closed(self):
        for raw in (b"null", b"{", b'{"action":"send"}', b'{"action":"listAccounts","command":"bad"}'):
            with self.subTest(raw=raw):
                response, _ = self.request(raw=raw)
                self.assertEqual(response[0], 400)
        self.service.list_accounts.assert_not_called()

    def test_body_size_and_transfer_encoding_are_bounded(self):
        for headers in ({"Content-Length": "65537"}, {"Content-Length": "-1"},
                        {"Transfer-Encoding": "chunked"}):
            response, _ = self.request(headers=headers)
            self.assertEqual(response[0], 400)
        self.service.list_accounts.assert_not_called()

    def test_bootstrap_is_one_time_and_secret_is_not_in_location(self):
        headers = {"Origin": "null", "Cookie": None, "Content-Type": "application/x-www-form-urlencoded"}
        response, extra = self.request(path="/bootstrap", headers=headers, raw=b"token=fixture-bootstrap")
        self.assertEqual(response[0], 303)
        self.assertEqual(dict(extra["extra"])["Location"], "/")
        self.assertIn("HttpOnly", dict(extra["extra"])["Set-Cookie"])
        self.assertFalse(self.launch.exists())
        response, _ = self.request(path="/bootstrap", headers=headers, raw=b"token=fixture-bootstrap")
        self.assertEqual(response[0], 400)

    def test_external_page_cannot_bootstrap_even_with_known_fixture_token(self):
        response, _ = self.request(path="/bootstrap", headers={"Origin": "https://evil.example",
                                   "Content-Type": "application/x-www-form-urlencoded"}, raw=b"token=fixture-bootstrap")
        self.assertEqual(response[0], 400)
        self.assertTrue(self.launch.exists())

    def test_live_html_injects_real_transport_and_unknown_paths_are_not_served(self):
        response, _ = self.request(method="GET", path="/")
        self.assertEqual(response[0], 200)
        self.assertIn(b'data-mode="live"', response[1])
        self.assertIn(b'live-service.js', response[1])
        self.assertNotIn(b' style=', response[1], 'Inline styles violate the live page CSP')
        response, _ = self.request(method="GET", path="/../../config.json")
        self.assertEqual(response[0], 404)

    def test_unauthenticated_browser_cannot_get_csrf_or_ui(self):
        for path in ("/", "/api/session"):
            response, _ = self.request(method="GET", path=path, headers={"Cookie": None})
            self.assertEqual(response[0], 403)

    def test_backend_exception_does_not_expose_raw_details(self):
        self.service.list_accounts.side_effect = OSError("private path and fixture-secret")
        response, _ = self.request()
        self.assertEqual(response[0], 500)
        self.assertNotIn("fixture-secret", str(response))


if __name__ == "__main__":
    unittest.main()
