"""Private process boundary to the existing Linux-keyring credential helpers."""

from dataclasses import dataclass
import hashlib
import json
import os
import secrets
from pathlib import Path
import subprocess


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class CredentialRef:
    entry: str
    serial: int | None
    backend: str = "linux-keyring"


class KeyringCredentials:
    lifetime = "session"
    def __init__(self, read_helper: str, write_helper: str, keyctl: str = "/usr/bin/keyctl"):
        if not all(Path(path).is_absolute() for path in (read_helper, write_helper, keyctl)):
            raise ValueError("Credential helper paths must be absolute")
        self.read_helper, self.write_helper, self.keyctl = read_helper, write_helper, keyctl

    def create(self, revision: str, purpose: str, secret: str) -> CredentialRef:
        if purpose not in ("imap", "smtp", "oauth", "client") or not secret:
            raise CredentialError("Invalid credential request.")
        entry = f"account:{hashlib.sha256(revision.encode()).hexdigest()}:{purpose}"
        try:
            result = subprocess.run(
                [self.write_helper, "--create-only", entry], input=secret.encode() + b"\n",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                check=False, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
            receipt = json.loads(result.stdout)
            serial = receipt.get("key_serial")
            if result.returncode or receipt.get("action") != "created" or receipt.get("entry") != entry or type(serial) is not int or serial <= 0:
                raise ValueError("Invalid helper receipt")
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as error:
            raise CredentialError("Could not store the account credential securely.") from error
        return CredentialRef(entry, serial)

    def command(self, reference: CredentialRef) -> list[str]:
        return [self.read_helper, reference.entry]

    def write_command(self, reference: CredentialRef) -> list[str]:
        """Let Ortie persist refreshes to the entry owned by this account."""
        if reference.backend != "linux-keyring":
            raise CredentialError("Credential storage does not match this account.")
        return [self.write_helper, reference.entry]

    def remove(self, reference: CredentialRef) -> bool:
        if reference.backend != "linux-keyring" or type(reference.serial) is not int or reference.serial <= 0:
            return False
        ok = True
        for arguments in (("revoke", str(reference.serial)), ("unlink", str(reference.serial), "@u")):
            try:
                result = subprocess.run([self.keyctl, *arguments], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=5, check=False,
                                        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
                ok = result.returncode == 0 and ok
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        return ok


class SecretServiceCredentials:
    """Use libsecret's CLI and the desktop keyring's default collection."""
    lifetime = "persistent"

    def __init__(self, executable: str = "/usr/bin/secret-tool", bus_address: str | None = None):
        if not Path(executable).is_absolute():
            raise ValueError("Secret Service tool path must be absolute")
        self.executable = executable
        self.bus_address = bus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")

    def _run(self, arguments, secret=None):
        return subprocess.run([self.executable, *arguments], input=secret,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "DBUS_SESSION_BUS_ADDRESS": self.bus_address})

    def create(self, revision: str, purpose: str, secret: str) -> CredentialRef:
        if purpose not in ("imap", "smtp", "oauth", "client") or not secret:
            raise CredentialError("Invalid credential request.")
        # secret-tool store is an upsert. A fresh random item identity avoids
        # targeting any existing item, including another attempt's credential.
        digest = hashlib.sha256((revision + secrets.token_hex(32)).encode()).hexdigest()
        reference = CredentialRef(f"account:{digest}:{purpose}", None, "secret-service")
        try:
            result = self._run(["store", "--label", f"Pimcamp {purpose} credential", "--collection", "default",
                                "application", "pimcamp", "entry", reference.entry], secret.encode())
            if result.returncode != 0:
                raise CredentialError("The mail host's encrypted password vault could not save this credential. Keep this form open and ask the agent or installation owner to check the vault service and unlock the existing vault. Its password is separate from your email password; do not change or re-enter your email password to repair storage.")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CredentialError("The mail host's encrypted password vault did not respond. Keep this form open while the agent or installation owner checks the vault service. Unlock an existing vault with its existing vault password; only choose a new password when creating a new vault. Never send either password through chat.") from error
        return reference

    def command(self, reference: CredentialRef) -> list[str]:
        # Lower tools deliberately sanitize their environment. Pass the owner's
        # nonsecret bus address explicitly; no password or token enters argv.
        return ["/usr/bin/env", f"DBUS_SESSION_BUS_ADDRESS={self.bus_address}", self.executable,
                "lookup", "application", "pimcamp", "entry", reference.entry]

    def remove(self, reference: CredentialRef) -> bool:
        if reference.backend != "secret-service":
            return False
        try:
            return self._run(["clear", "application", "pimcamp", "entry", reference.entry]).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def write_command(self, reference: CredentialRef) -> list[str]:
        if reference.backend != "secret-service":
            raise CredentialError("Credential storage does not match this account.")
        return ["/usr/bin/env", f"DBUS_SESSION_BUS_ADDRESS={self.bus_address}", self.executable,
                "store", "--label", "Pimcamp OAuth credential", "--collection", "default",
                "application", "pimcamp", "entry", reference.entry]
