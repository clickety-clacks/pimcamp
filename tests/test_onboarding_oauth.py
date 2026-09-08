"""Synthetic policy inputs, not recorded Ortie or Google response fixtures."""
import base64
import hashlib
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pimcamp.onboarding_oauth import (GoogleApplication, GoogleGrant, OAuthSetupError,
                                     OrtieAuthorization, render_ortie_account, GOOGLE_AUTH, GMAIL_SCOPE)


class OAuthPolicyTests(unittest.TestCase):
    def setUp(self):
        self.application = GoogleApplication("fixture.apps.googleusercontent.com", "/fixture/ortie")
        self.callback = "http://127.0.0.1:33281/oauth/google/callback"
        self.state = "unit-test-state-0123456789"
        self.verifier = "a" * 43
        self.query = {"client_id": self.application.client_id, "redirect_uri": self.callback,
                      "response_type": "code", "scope": GMAIL_SCOPE, "state": self.state,
                      "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(
                          hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("="),
                      "access_type": "offline", "prompt": "consent select_account"}

    def policy_input(self):
        return {"authorization_uri": GOOGLE_AUTH + "?" + urlencode(self.query),
                "state": self.state, "pkce_code_verifier": self.verifier}

    def grant(self):
        return GoogleGrant.from_ortie(self.policy_input(), self.application, self.callback)

    def test_authorization_request_validates_pkce_and_hides_private_state(self):
        grant = self.grant()
        self.assertNotIn(self.verifier, repr(grant))
        self.assertNotIn(self.state, repr(grant))
        self.assertTrue(grant.authorization_url.startswith(GOOGLE_AUTH))

    def test_mismatched_pkce_wrong_destination_and_token_in_url_are_rejected(self):
        for key, value in (("code_challenge", "wrong"), ("redirect_uri", "https://evil.example"),
                           ("access_token", "fixture-token"), ("scope", "openid")):
            with self.subTest(key=key):
                original = self.query.copy()
                self.query[key] = value
                with self.assertRaises(OAuthSetupError):
                    self.grant()
                self.query = original

    def test_callback_requires_matching_state_and_exact_address(self):
        for uri in (self.callback + "?code=fixture&state=wrong", "https://evil.example?state=" + self.state,
                    self.callback + "?code=fixture&state=" + self.state + "\nquit"):
            with self.subTest(uri=uri), self.assertRaises(OAuthSetupError):
                self.grant().response_status(uri)

    def test_cancellation_policy_denial_and_expiration_are_distinct(self):
        for error, expected in (("access_denied", "cancelled"), ("admin_policy_enforced", "denied-policy")):
            uri = self.callback + "?" + urlencode({"state": self.state, "error": error})
            self.assertEqual(self.grant().response_status(uri), expected)
        grant = self.grant()
        grant.created -= 901
        self.assertEqual(grant.response_status(self.callback), "expired")

    def test_sensitive_resume_values_use_stdin_not_process_arguments(self):
        grant = self.grant()
        uri = self.callback + "?" + urlencode({"state": self.state, "code": "unit-authorization-code"})
        adapter = OrtieAuthorization(self.application)
        with patch("pimcamp.onboarding_oauth.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"")) as run:
            adapter._run(Path("/fixture/ortie.toml"), "account-a", ["repl"], grant.resume_input(uri))
        args, kwargs = run.call_args
        self.assertNotIn("unit-authorization-code", str(args))
        self.assertNotIn(self.verifier, str(args))
        self.assertIn(b"unit-authorization-code", kwargs["input"])
        self.assertIn(b"token inspect\nquit\n", kwargs["input"])

    def test_config_uses_credential_commands_and_fixed_callback(self):
        raw = render_ortie_account(self.application, "account-a", self.callback,
                                   ["/fixture/read", "oauth-a"], ["/fixture/write", "oauth-a"])
        account = tomllib.loads(raw.decode())["accounts"]["account-a"]
        self.assertEqual(account["endpoints"]["redirection"], self.callback)
        self.assertEqual(account["pkce"], "s256")
        self.assertTrue(account["auto-refresh"])
        self.assertEqual(account["storage"]["write"]["command"], ["/fixture/write", "oauth-a"])
        self.assertNotIn("client-secret", account)


if __name__ == "__main__":
    unittest.main()
