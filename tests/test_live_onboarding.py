"""Opt-in real IMAP onboarding acceptance. No send, junk, or live config edits."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pimcamp.onboarding_credentials import KeyringCredentials
from pimcamp.onboarding_service import SetupService
from pimcamp.onboarding_store import AccountStore


class LiveOnboardingTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PIMCAMP_ONBOARDING_LIVE_ENABLE") == "1",
                         "Set PIMCAMP_ONBOARDING_LIVE_ENABLE=1 with an authorized IMAP test deployment")
    def test_real_account_setup_and_public_list_preserve_existing_configuration(self):
        required = ("PIMCAMP_ONBOARDING_SOURCE_CONFIG", "PIMCAMP_ONBOARDING_ACCOUNT",
                    "PIMCAMP_ONBOARDING_HIMALAYA", "PIMCAMP_ONBOARDING_CARILLON")
        if any(not os.environ.get(key) for key in required):
            self.skipTest("Missing source config, account, or installed lower-tool paths")
        source = Path(os.environ[required[0]])
        before = hashlib.sha256(source.read_bytes()).digest()
        account = tomllib.loads(source.read_text())["accounts"][os.environ[required[1]]]
        # This explicit live lane reuses protected credential commands in memory.
        # It never copies a provider password to argv, environment, files or logs.
        def read_secret(command):
            self.assertIsInstance(command, list)
            self.assertTrue(Path(command[0]).is_absolute())
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, "Deployment credential helper failed (output suppressed)")
            self.assertTrue(result.stdout, "Deployment credential helper returned no secret")
            return result.stdout.decode().rstrip("\r\n")
        def server(protocol):
            backend = account[protocol]
            uri = urlsplit(backend["server"])
            login = backend["sasl"]["plain"]
            return {"host": uri.hostname, "port": uri.port,
                    "security": "tls" if uri.scheme.endswith("s") else "starttls",
                    "username": login["username"], "password": read_secret(login["password"]["command"])}
        draft = {"method": "imap", "email": account["email"], "name": "Onboarding acceptance @ test.example",
                 "incoming": server("imap"), "outgoing": {**server("smtp"), "sameLogin": False}}
        credentials = KeyringCredentials(str(ROOT / "scripts/pimcamp-keyctl-read"),
                                          str(ROOT / "scripts/pimcamp-keyctl-write"))
        references = []
        with tempfile.TemporaryDirectory(prefix="pimcamp-onboarding-acceptance-") as temporary:
            root = Path(temporary)
            service = SetupService(AccountStore(root / "accounts"), credentials, root / "state",
                                   os.environ[required[2]], os.environ[required[3]],
                                   runtime_config=root / "runtime/config.json")
            setup_id = service.begin()["setupId"]
            try:
                for backend in ("imap", "smtp"):
                    outcome = service.check(setup_id, draft, backend)
                    self.assertTrue(outcome.get("ok"), f"Real {backend} authentication failed (lower output suppressed)")
                saved = service.commit(setup_id, draft)
                self.assertTrue(saved["ok"])
                attempt = service.attempts[setup_id]
                references = list(attempt.references.values())
                client = read_secret(credentials.command(attempt.references["client"]))
                envelope = {"contract_version": "pimcamp.v1", "client_credential": client, "input": {"limit": 1}}
                result = subprocess.run([str(ROOT / "pimcamp"), "list"],
                    input=json.dumps(envelope).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env={**os.environ, "PIMCAMP_CONFIG": str(root / "runtime/config.json")}, timeout=45, check=False)
                self.assertEqual(result.returncode, 0, "Public list failed for newly saved profile (mail output suppressed)")
                response = json.loads(result.stdout)
                self.assertIn("result", response, "Public list did not return a success envelope")
                self.assertEqual(service.list_accounts()[0]["address"], draft["email"])
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before,
                                 "Existing lower configuration changed")
            finally:
                # Include credentials created before any failing step. All are
                # unique entries owned by this isolated acceptance invocation.
                attempt = service.attempts.get(setup_id)
                if attempt:
                    references = list(attempt.references.values())
                failures = [reference.entry for reference in references if not credentials.remove(reference)]
                self.assertFalse(failures, "Could not clear acceptance-owned credential entries")
                draft.clear()


if __name__ == "__main__":
    unittest.main()
