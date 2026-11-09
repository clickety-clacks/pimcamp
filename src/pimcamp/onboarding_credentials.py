"""Private process boundary to the existing Linux-keyring credential helpers."""

from dataclasses import dataclass
import hashlib
import json
import os
import secrets
from pathlib import Path
import socket
import struct
import subprocess


class CredentialError(Exception):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


# The running keyring daemon's control socket speaks a small framed protocol:
# a one-byte credentials preamble, then big-endian uint32 total length, uint32
# operation and a length-prefixed string. Operation 1 unlocks the login vault
# or creates it when none exists. This is the same request the PAM login
# module sends, so it reaches the daemon already serving the session bus
# instead of starting a competing one.
_CONTROL_OP_UNLOCK = 1
_CONTROL_RESULTS = {0: "ok", 1: "denied", 2: "failed", 3: "no-daemon"}


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

    def __init__(self, executable: str = "/usr/bin/secret-tool", bus_address: str | None = None, *,
                 busctl: str = "/usr/bin/busctl", control_directory: str | None = None):
        if not Path(executable).is_absolute() or not Path(busctl).is_absolute():
            raise ValueError("Secret Service tool path must be absolute")
        self.executable = executable
        self.busctl = busctl
        self.bus_address = bus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        self.control_directory = control_directory or os.environ.get("GNOME_KEYRING_CONTROL") or os.path.join(runtime, "keyring")

    def _run(self, arguments, secret=None):
        return subprocess.run([self.executable, *arguments], input=secret,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "DBUS_SESSION_BUS_ADDRESS": self.bus_address})

    def _bus(self, arguments):
        """Return the decoded busctl JSON payload, or None when the call failed."""
        try:
            result = subprocess.run([self.busctl, f"--address={self.bus_address}", "--json=short", "--timeout=15",
                                     *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=30, check=False,
                                    env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
            if result.returncode != 0:
                return None
            return json.loads(result.stdout)["data"]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            return None

    def _round_trip(self) -> bool:
        """Prove the default vault accepts, returns and removes a generated test item."""
        token = secrets.token_urlsafe(24)
        entry = f"storage-check:{secrets.token_hex(16)}"
        try:
            stored = self._run(["store", "--label", "Pimcamp storage check", "--collection", "default",
                                "application", "pimcamp", "entry", entry], token.encode())
            if stored.returncode != 0:
                return False
            read = self._run(["lookup", "application", "pimcamp", "entry", entry])
            return read.stdout.strip() == token.encode() and self._run(
                ["clear", "application", "pimcamp", "entry", entry]).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def storage_status(self) -> dict:
        """Report the default vault as ready, missing, locked or unavailable.

        The result is observed through the Secret Service itself, never
        inferred from a helper exit code. ``manageable`` says whether this
        process can reach the running daemon's control socket to create or
        unlock the vault on the user's behalf.
        """
        # The first bus call may activate the daemon, which is what creates the
        # control socket, so look for the socket after the bus has answered.
        manageable = lambda: os.path.exists(os.path.join(self.control_directory, "control"))
        default = self._bus(["call", "org.freedesktop.secrets", "/org/freedesktop/secrets",
                             "org.freedesktop.Secret.Service", "ReadAlias", "s", "default"])
        if not isinstance(default, list) or len(default) != 1 or not isinstance(default[0], str):
            return {"state": "unavailable", "manageable": manageable()}
        if default[0] == "/":
            return {"state": "missing", "manageable": manageable()}
        locked = self._bus(["get-property", "org.freedesktop.secrets", default[0],
                            "org.freedesktop.Secret.Collection", "Locked"])
        if locked is True:
            return {"state": "locked", "manageable": manageable()}
        if locked is not False or not self._round_trip():
            return {"state": "unavailable", "manageable": manageable()}
        return {"state": "ready", "manageable": manageable()}

    def _control_unlock(self, password: str) -> str:
        secret = password.encode()
        frame = struct.pack(">II", _CONTROL_OP_UNLOCK, len(secret)) + secret
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(20)
                connection.connect(os.path.join(self.control_directory, "control"))
                connection.sendall(b"\0" + struct.pack(">I", 4 + len(frame)) + frame)
                header = b""
                while len(header) < 8:
                    chunk = connection.recv(8 - len(header))
                    if not chunk:
                        break
                    header += chunk
        except OSError:
            return "unreachable"
        if len(header) != 8:
            return "unreachable"
        return _CONTROL_RESULTS.get(struct.unpack(">II", header)[1], "failed")

    def unlock_storage(self, password: str, *, create: bool) -> dict:
        """Create the vault with a new password, or unlock the existing one.

        Creation is refused when a vault already exists, and unlocking is
        refused when none exists, so a mistaken choice can never reset or
        replace stored credentials. Success means the vault was observed
        unlocked and working afterwards.
        """
        if not isinstance(password, str) or not password:
            raise CredentialError("Enter the vault password first.", code="vault_password_required")
        status = self.storage_status()
        if status["state"] == "ready":
            return status
        if status["state"] == "unavailable":
            raise CredentialError("The mail host's encrypted password vault is not responding, so nothing could be created or unlocked.", code="storage_unavailable")
        if create and status["state"] == "locked":
            raise CredentialError("A vault already exists on the mail host. Unlock it with its existing password instead; it was not changed.", code="vault_exists")
        if not create and status["state"] == "missing":
            raise CredentialError("The mail host has no vault yet. Create one with a new password instead.", code="vault_missing")
        if not status["manageable"]:
            raise CredentialError("This page can't reach the vault service on the mail host to create or unlock it. Ask the installation owner to unlock the vault there.", code="vault_unmanaged")
        outcome = self._control_unlock(password)
        if outcome == "denied":
            if status["state"] == "locked":
                raise CredentialError("That password didn't unlock the existing vault. Check your password manager for the vault password you chose earlier; the vault was not changed.", code="vault_password_rejected")
            raise CredentialError("The vault service refused to create the vault with that password.", code="storage_unavailable")
        if outcome != "ok":
            raise CredentialError("The vault service on the mail host did not complete the request, so nothing was created or unlocked.", code="storage_unavailable")
        after = self.storage_status()
        if after["state"] != "ready":
            raise CredentialError("The vault service accepted the password, but the vault is still not usable. Check again in a moment or ask the installation owner to inspect it.", code="storage_unavailable")
        return after

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
