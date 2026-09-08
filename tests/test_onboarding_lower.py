from pathlib import Path
import subprocess
import tempfile
import sys
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pimcamp.onboarding import validate_imap_setup
from pimcamp.onboarding_lower import (check_himalaya_account, render_himalaya_account,
                                     render_carillon_account, render_pimcamp_profile,
                                     render_google_accounts, google_account_identity)
from pimcamp.config import load


class LowerSetupTests(unittest.TestCase):
    def test_google_configs_share_renewable_token_command_and_identity(self):
        command = ["/opt/ortie", "--config", "/private/ortie.toml", "token", "get"]
        operations, observation = render_google_accounts("alex@example.com", "account-a", command)
        ops = tomllib.loads(operations.decode())["accounts"]["account-a"]
        watch = tomllib.loads(observation.decode())["accounts"]["account-a"]
        self.assertEqual(set(watch), {"default", "imap"})
        for backend in (ops["imap"], ops["smtp"], watch["imap"]):
            self.assertEqual(backend["sasl"]["xoauth2"], {
                "username": "alex@example.com", "token": {"command": command}})
        self.assertEqual(ops["gmail"]["auth"]["token"]["command"], command)
        self.assertEqual(watch["imap"]["mailbox"], "INBOX")

    def test_google_token_command_rejects_relative_executable(self):
        with self.assertRaises(ValueError):
            render_google_accounts("alex@example.com", "account-a", ["ortie", "token", "get"])

    def test_google_identity_failure_does_not_expose_lower_output(self):
        with patch("pimcamp.onboarding_lower.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, b"private diagnostic")) as run:
            with self.assertRaisesRegex(ValueError, "Could not verify the Google account"):
                google_account_identity("/opt/himalaya", Path("/private/account.toml"), "account-a")
        args, kwargs = run.call_args
        self.assertEqual(args[0][-3:], ["gmail", "profile", "get"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def setup_value(self, security="tls"):
        return validate_imap_setup({
            "email": "alex@custom.example", "account_name": "Work mailbox",
            "incoming": {"host": "incoming.custom.example", "security": security,
                         "password": "fixture-secret-not-for-config"},
            "outgoing": {"host": "outgoing.custom.example", "security": security}})

    def test_tls_config_has_references_not_passwords(self):
        raw = render_himalaya_account(self.setup_value(), "account-a",
                                      ["/opt/pimcamp/read", "incoming-a"],
                                      ["/opt/pimcamp/read", "outgoing-a"])
        self.assertNotIn(b"fixture-secret", raw)
        account = tomllib.loads(raw.decode())["accounts"]["account-a"]
        self.assertEqual(account["email"], "alex@custom.example")
        self.assertEqual(account["imap"]["server"], "imaps://incoming.custom.example:993")
        self.assertEqual(account["smtp"]["server"], "smtps://outgoing.custom.example:465")
        self.assertEqual(account["smtp"]["sasl"]["plain"]["password"],
                         {"command": ["/opt/pimcamp/read", "outgoing-a"]})

    def test_starttls_is_explicit_for_both_backends(self):
        raw = render_himalaya_account(self.setup_value("starttls"), "account-a",
                                      ["/opt/pimcamp/read", "incoming-a"],
                                      ["/opt/pimcamp/read", "outgoing-a"])
        account = tomllib.loads(raw.decode())["accounts"]["account-a"]
        for protocol in ("imap", "smtp"):
            self.assertTrue(account[protocol]["starttls"])
            self.assertTrue(account[protocol]["server"].startswith(protocol + "://"))

    def test_check_uses_lower_authentication_command_without_mail_operations(self):
        with patch("pimcamp.onboarding_lower.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)) as run:
            result = check_himalaya_account("/opt/himalaya", Path("/private/account.toml"), "account-a", "smtp")
        self.assertTrue(result["ok"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][-2:], ["account", "check"])
        self.assertEqual(args[0][args[0].index("--backend") + 1], "smtp")
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)

    def test_failures_are_actionable_without_raw_tool_output(self):
        for outcome, code in ((subprocess.TimeoutExpired("fixture", 30), "timeout"),
                              (OSError("private path"), "unavailable")):
            with patch("pimcamp.onboarding_lower.subprocess.run", side_effect=outcome):
                result = check_himalaya_account("/opt/himalaya", Path("/private/account.toml"), "account-a", "imap")
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], code)
            self.assertNotIn("private path", result["message"])

    def test_carillon_has_only_its_own_backend_settings(self):
        raw = render_carillon_account(self.setup_value("starttls"), "account-a", ["/opt/read", "imap-a"])
        account = tomllib.loads(raw.decode())["accounts"]["account-a"]
        self.assertEqual(set(account), {"default", "imap"})
        self.assertEqual(account["imap"]["mailbox"], "INBOX")
        self.assertTrue(account["imap"]["starttls"])
        self.assertNotIn("hook", account["imap"])

    def test_generated_profile_loads_through_real_pimcamp_config_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = render_pimcamp_profile(self.setup_value(), "account-a", root,
                                         root / "state.sqlite3", "/opt/himalaya", "/opt/carillon", "a" * 64)
            path = root / "config.json"
            path.write_bytes(raw)
            path.chmod(0o600)
            config = load(path)
        self.assertEqual(config.operations.account, config.observation.account)
        self.assertEqual(config.operations.sender["address"], "alex@custom.example")
        self.assertIsNone(config.operations.junk_mailbox)
        self.assertNotIn("fixture-secret", raw.decode())


if __name__ == "__main__":
    unittest.main()
