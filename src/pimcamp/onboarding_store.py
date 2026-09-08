"""Atomic publication of account configuration revisions, without secret files."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import uuid


class AccountStoreError(Exception):
    pass


def account_id(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise AccountStoreError("Invalid account identifier.") from error
    if value != normalized:
        raise AccountStoreError("Invalid account identifier.")
    return value


class AccountStore:
    """A stable account link selects an immutable, complete configuration revision.

    Credentials are owned by the credential helper, not this store. File payloads
    are produced by trusted setup code, not accepted directly from HTTP requests.
    """

    def __init__(self, root: Path):
        self.root = root.absolute()
        self._directory(self.root)
        # A legitimate parent alias (for example /var -> /private/var) must
        # compare consistently with resolved account links. Validate the root
        # itself before resolving so a symlink cannot bypass its owner check.
        self.root = self.root.resolve(strict=True)
        self.accounts = self.root / "accounts"
        self.revisions = self.root / "revisions"
        self._directory(self.accounts)
        self._directory(self.revisions)

    @staticmethod
    def _directory(path: Path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AccountStoreError("The account configuration directory must be private to its owner.")

    @contextmanager
    def _lock(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.root / ".setup.lock", flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AccountStoreError("The account configuration lock is not private.")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _revision(self, identifier: str) -> Path:
        link = self.accounts / account_id(identifier)
        if not link.is_symlink():
            raise AccountStoreError("The account does not have a valid configuration revision.")
        target = link.resolve(strict=True)
        if target.parent != self.revisions or not target.is_dir():
            raise AccountStoreError("The account configuration points outside its revision store.")
        return target

    def list_accounts(self) -> list[dict]:
        with self._lock():
            return self._list_accounts()

    def ensure_runtime_default(self, identifier: str, destination: Path) -> bool:
        """Select the first account without replacing an existing runtime config.

        Point through the stable account link so reconnect takes effect without
        changing the default. Concurrent first-account saves use create-only
        symlink creation; an existing file or dangling link is preserved.
        """
        with self._lock():
            self._revision(identifier)
            self._directory(destination.parent)
            target = self.accounts / account_id(identifier) / "config.json"
            try:
                destination.symlink_to(target)
            except FileExistsError:
                return destination.is_symlink() and os.readlink(destination) == str(target)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True

    def _list_accounts(self) -> list[dict]:
        records = []
        for entry in sorted(self.accounts.iterdir()):
            if entry.name.startswith("."):
                continue
            revision = self._revision(entry.name)
            record = json.loads((revision / "account.json").read_text())
            if record.get("id") != entry.name:
                raise AccountStoreError("The account metadata is inconsistent.")
            records.append(record)
        return records

    def publish(self, identifier: str, metadata: dict, files: dict[str, bytes],
                *, reconnect: bool = False, expected_revision: str | None = None,
                revision_id: str | None = None) -> Path:
        """Publish one complete revision; reconnect uses optimistic concurrency.

        A fresh account never replaces another. Reconnect requires the revision
        inspected by this setup, preventing one setup from overwriting another.
        Old revisions remain for in-flight readers; retention is a separate task.
        """
        identifier = account_id(identifier)
        revision = self.revisions / account_id(revision_id or str(uuid.uuid4()))
        allowed = {"himalaya.toml", "carillon.toml", "ortie.toml", "config.json", "credential-refs.json"}
        if not files or set(files) - allowed or not all(isinstance(v, bytes) for v in files.values()):
            raise AccountStoreError("Invalid configuration files.")
        if set(metadata) - {"id", "address", "name", "method", "status", "note", "incoming", "outgoing"}:
            raise AccountStoreError("Invalid account metadata.")
        record = {**metadata, "id": identifier}
        if not isinstance(record.get("address"), str) or not record["address"]:
            raise AccountStoreError("An account email address is required.")
        # Never let credential-bearing objects accidentally enter account listings.
        for key in ("incoming", "outgoing"):
            if key in record:
                server = record[key]
                if not isinstance(server, dict) or set(server) - {"host", "port", "security", "username", "sameLogin"}:
                    raise AccountStoreError("Account metadata must not contain credentials.")
        temporary = None
        pointer = self.accounts / (".pending-" + str(uuid.uuid4()))
        with self._lock():
            link = self.accounts / identifier
            exists = os.path.lexists(link)
            if exists != reconnect:
                raise AccountStoreError("The account already exists." if exists else "The account no longer exists.")
            if reconnect and self._revision(identifier).name != expected_revision:
                raise AccountStoreError("This account changed during setup. Reload it before reconnecting.")
            for old in self._list_accounts():
                if old["id"] != identifier and old["address"].casefold() == record["address"].casefold():
                    raise AccountStoreError("This email address is already configured.")
            try:
                revision.mkdir(mode=0o700)
                temporary = revision
                payloads = {**files, "account.json": (json.dumps(record) + "\n").encode()}
                for name, payload in payloads.items():
                    descriptor = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(payload)
                        output.flush()
                        os.fsync(output.fileno())
                directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                pointer.symlink_to(temporary)
                os.replace(pointer, link)
                published = temporary
                temporary = None  # Publication succeeded; never roll back by deleting its target.
                return published
            finally:
                if os.path.lexists(pointer):
                    pointer.unlink()
                if temporary is not None:
                    shutil.rmtree(temporary)
