"""Explicit isolated Secret Service acceptance, using generated test values only."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from pimcamp.onboarding_credentials import SecretServiceCredentials


def probe(root):
    unlock = secrets.token_urlsafe(32).encode()
    original = secrets.token_urlsafe(32)
    refreshed = secrets.token_urlsafe(32)
    daemon = None
    store = SecretServiceCredentials()

    def start():
        process = subprocess.Popen([
            "/usr/bin/gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets",
            "--control-directory", str(root / "control")], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.stdin.write(unlock)
        process.stdin.close()
        time.sleep(0.5)
        if process.poll() is not None:
            raise RuntimeError("Isolated keyring service did not remain running")
        return process

    def stop(process):
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def invoke(command, payload=None):
        result = subprocess.run(command, input=payload, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode:
            raise RuntimeError("Isolated credential operation failed (output suppressed)")
        return result.stdout

    try:
        daemon = start()
        reference = store.create("isolated-persistence-probe", "oauth", original)
        if invoke(store.command(reference)).decode() != original:
            raise RuntimeError("Initial protected lookup did not match")
        invoke(store.write_command(reference), refreshed.encode())
        if invoke(store.command(reference)).decode() != refreshed:
            raise RuntimeError("Refresh write did not replace the owned token")
        stop(daemon)
        daemon = None
        keyrings = list((root / "data/keyrings").glob("*.keyring"))
        if not keyrings:
            raise RuntimeError("No persistent keyring file was created")
        for path in keyrings:
            raw = path.read_bytes()
            if original.encode() in raw or refreshed.encode() in raw or unlock in raw:
                raise RuntimeError("A generated test secret appeared unencrypted on disk")
        daemon = start()
        if invoke(store.command(reference)).decode() != refreshed:
            raise RuntimeError("Credential did not survive service restart and unlock")
        if not store.remove(reference):
            raise RuntimeError("Could not remove the isolated test item")
        result = subprocess.run(store.command(reference), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode == 0 or result.stdout:
            raise RuntimeError("Removed test item remained readable")
        print("PASS: protected create/read, refresh write, encrypted file check, service restart/unlock, removal")
    finally:
        stop(daemon)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--isolated-child":
        try:
            probe(Path(sys.argv[2]))
        except Exception:
            print("FAIL: isolated Secret Service acceptance did not complete; no secrets or lower output disclosed.", file=sys.stderr)
            return 1
        return 0
    with tempfile.TemporaryDirectory(prefix="pimcamp-secret-service-check-") as temporary:
        root = Path(temporary)
        for name in ("data", "runtime", "config", "control"):
            (root / name).mkdir(mode=0o700)
        env = {**os.environ, "XDG_DATA_HOME": str(root / "data"), "XDG_RUNTIME_DIR": str(root / "runtime"),
               "XDG_CONFIG_HOME": str(root / "config")}
        # The isolated bus and daemon inherit temporary XDG paths. HOME and the
        # operator's existing keyring/session are never replaced or unlocked.
        env.pop("GNOME_KEYRING_CONTROL", None)
        return subprocess.run(["/usr/bin/dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()),
                               "--isolated-child", str(root)], env=env, timeout=90, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
