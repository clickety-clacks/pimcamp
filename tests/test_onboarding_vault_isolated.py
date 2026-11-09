"""Create, lock and unlock a real keyring vault inside a private session bus.

Everything runs under dbus-run-session with temporary XDG directories, so the
operator's own keyring, session bus and stored secrets are never contacted.
Skipped when the keyring daemon or D-Bus tools are not installed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("/usr/bin/dbus-run-session", "/usr/bin/gnome-keyring-daemon", "/usr/bin/busctl", "/usr/bin/secret-tool")

SCENARIO = r'''
import json, os, secrets, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from pimcamp.onboarding_credentials import SecretServiceCredentials, CredentialError

runtime = os.environ["XDG_RUNTIME_DIR"]
store = SecretServiceCredentials(control_directory=os.path.join(runtime, "keyring"))
password = secrets.token_urlsafe(24)
steps = []
def attempt(label, secret, create):
    try:
        steps.append((label, store.unlock_storage(secret, create=create)["state"]))
    except CredentialError as error:
        steps.append((label, error.code))
def bus(*args):
    out = subprocess.run(["/usr/bin/busctl", "--user", "--json=short", *args], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, check=False).stdout
    return json.loads(out)["data"] if out else None
def daemon_pid():
    return bus("call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
               "GetConnectionUnixProcessID", "s", "org.freedesktop.secrets")[0]
def stop_daemon():
    pid = daemon_pid()
    os.kill(pid, 15)
    for _ in range(100):
        try:
            os.kill(pid, 0); time.sleep(0.05)
        except ProcessLookupError:
            return pid
    raise RuntimeError("isolated daemon did not exit")

try:
    steps.append(("fresh", store.storage_status()))
    attempt("unlock-missing", password, False)
    attempt("create", password, True)
    steps.append(("created", store.storage_status()["state"]))
    collection = bus("call", "org.freedesktop.secrets", "/org/freedesktop/secrets",
                     "org.freedesktop.Secret.Service", "ReadAlias", "s", "default")[0]
    bus("call", "org.freedesktop.secrets", "/org/freedesktop/secrets", "org.freedesktop.Secret.Service",
        "Lock", "ao", "1", collection)
    steps.append(("locked", store.storage_status()["state"]))
    attempt("create-over-existing", "another-" + password, True)
    attempt("wrong-password", "wrong-" + password, False)
    steps.append(("still-locked", store.storage_status()["state"]))
    attempt("unlock", password, False)
    steps.append(("unlocked", store.storage_status()["state"]))
    stop_daemon()
    steps.append(("after-restart", store.storage_status()["state"]))
    attempt("unlock-after-restart", password, False)
    steps.append(("final", store.storage_status()["state"]))
finally:
    try:
        stop_daemon()
    except Exception:
        pass
print(json.dumps(steps))
'''


@unittest.skipUnless(all(os.path.exists(tool) for tool in TOOLS), "keyring daemon or D-Bus tools not installed")
class IsolatedVaultTests(unittest.TestCase):
    def test_vault_is_created_once_then_only_unlocked_with_its_own_password(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            runtime.mkdir(mode=0o700)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GNOME_KEYRING", "DBUS_", "XDG_"))}
            env.update({"HOME": str(Path(temp) / "home"), "XDG_RUNTIME_DIR": str(runtime),
                        "XDG_DATA_HOME": str(Path(temp) / "data"), "XDG_CONFIG_HOME": str(Path(temp) / "config"),
                        "XDG_CACHE_HOME": str(Path(temp) / "cache"), "LC_ALL": "C"})
            Path(env["HOME"]).mkdir()
            result = subprocess.run(["/usr/bin/dbus-run-session", "--", sys.executable, "-c", SCENARIO, str(ROOT / "src")],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-2000:])
            steps = [tuple(step) for step in json.loads(result.stdout.decode().strip().splitlines()[-1])]
        self.assertEqual(steps[0][0], "fresh")
        self.assertEqual(steps[0][1], {"state": "missing", "manageable": True})
        self.assertEqual(steps[1:], [
            ("unlock-missing", "vault_missing"),
            ("create", "ready"),
            ("created", "ready"),
            ("locked", "locked"),
            ("create-over-existing", "vault_exists"),
            ("wrong-password", "vault_password_rejected"),
            ("still-locked", "locked"),
            ("unlock", "ready"),
            ("unlocked", "ready"),
            ("after-restart", "locked"),
            ("unlock-after-restart", "ready"),
            ("final", "ready"),
        ])


if __name__ == "__main__":
    unittest.main()
