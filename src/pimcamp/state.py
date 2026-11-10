"""Stamped private state for opaque message references and mutation receipts."""

from pathlib import Path
import os
import sqlite3
import time
import uuid
from urllib.parse import quote

from .errors import PimcampError, unavailable


_SCHEMA_VERSION = "pimcamp-state-v1"


class State:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            self._open_read_only()
            return
        self._prepare_parent()
        existed = path.exists()
        try:
            self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp state is unavailable.") from exc
        if existed:
            self._verify_stamp()
        else:
            self._create()

    @classmethod
    def open_read_only(cls, path: Path) -> "State":
        return cls(path, read_only=True)

    def _open_read_only(self) -> None:
        try:
            mode = self.path.stat().st_mode
            if mode & 0o077:
                raise unavailable("Pimcamp state is not owner-only.")
            uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self._verify_stamp()
        except PimcampError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise unavailable("Pimcamp state is unavailable.") from exc

    def close(self) -> None:
        self.connection.close()

    def ensure_mail_schema(self) -> None:
        """Create only the additive v2 tables; leave the v1 stamp and rows intact."""
        if self.read_only:
            raise unavailable("Pimcamp mail state is read-only.")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS mail_metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )"""
            )
            row = self.connection.execute(
                "SELECT value FROM mail_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and row["value"] != "pimcamp-mail-v2":
                self.connection.execute("ROLLBACK")
                raise unavailable("Pimcamp mail state has an unknown schema stamp.")
            if row is None:
                self.connection.execute(
                    "INSERT INTO mail_metadata(key, value) VALUES ('schema_version', 'pimcamp-mail-v2')"
                )
                row = self.connection.execute(
                    "SELECT value FROM mail_metadata WHERE key = 'schema_version'"
                ).fetchone()
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS mail_source_references (
                  source_ref TEXT PRIMARY KEY,
                  account_id TEXT NOT NULL,
                  adapter_fingerprint TEXT NOT NULL,
                  adapter_id TEXT NOT NULL,
                  identity_evidence_json TEXT,
                  created_at REAL NOT NULL
                )"""
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS mail_source_refs_by_account ON mail_source_references(account_id, created_at)"
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS mail_locators (
                  locator TEXT PRIMARY KEY,
                  account_id TEXT NOT NULL,
                  identity_namespace TEXT NOT NULL,
                  identity_key TEXT NOT NULL,
                  identity_evidence_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  UNIQUE(account_id, identity_namespace, identity_key)
                )"""
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS mail_reply_operations (
                  operation_id TEXT PRIMARY KEY,
                  principal_id TEXT NOT NULL,
                  account_id TEXT NOT NULL,
                  locator TEXT NOT NULL,
                  request_digest TEXT,
                  state TEXT NOT NULL CHECK (state IN ('prepared', 'pending', 'sent', 'not_submitted', 'unknown')),
                  attempt_id TEXT,
                  claim_deadline REAL,
                  transport_call_began INTEGER NOT NULL DEFAULT 0 CHECK (transport_call_began IN (0, 1)),
                  result_json TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                )"""
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS mail_reply_ops_scope ON mail_reply_operations(principal_id, account_id, operation_id)"
            )
            self.connection.execute("COMMIT")
        except PimcampError:
            raise
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise unavailable("Pimcamp v2 mail state could not be initialized.") from exc
        if row is None or row["value"] != "pimcamp-mail-v2":
            raise unavailable("Pimcamp mail state has an unknown schema stamp.")

    def _prepare_parent(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = self.path.parent.stat().st_mode
            if mode & 0o077:
                raise unavailable("Pimcamp state directory is not owner-only.")
        except OSError as exc:
            raise unavailable("Pimcamp state directory is unavailable.") from exc

    def _create(self) -> None:
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                INSERT INTO metadata(key, value) VALUES ('schema_version', 'pimcamp-state-v1');
                CREATE TABLE message_references (
                  message_ref TEXT PRIMARY KEY,
                  adapter_fingerprint TEXT NOT NULL,
                  adapter_id TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE cursor_references (
                  cursor TEXT PRIMARY KEY,
                  adapter_fingerprint TEXT NOT NULL,
                  adapter_cursor TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE mutation_receipts (
                  client_identity TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  mutation_id TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  mutation_call_began INTEGER NOT NULL CHECK (mutation_call_began IN (0, 1)),
                  state TEXT NOT NULL CHECK (state IN ('reserved', 'succeeded', 'failed', 'unknown')),
                  claim_deadline REAL NOT NULL,
                  result_json TEXT,
                  PRIMARY KEY(client_identity, operation, mutation_id)
                );
                COMMIT;
                """
            )
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise unavailable("Pimcamp state could not be initialized.") from exc

    def _verify_stamp(self) -> None:
        try:
            mode = self.path.stat().st_mode
            if mode & 0o077:
                raise unavailable("Pimcamp state is not owner-only.")
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp state has an unknown shape.") from exc
        if row is None or row["value"] != _SCHEMA_VERSION:
            raise unavailable("Pimcamp state has an unknown schema stamp.")

    def store_message_ref(self, fingerprint: str, adapter_id: str) -> str:
        message_ref = str(uuid.uuid4())
        try:
            self.connection.execute(
                "INSERT INTO message_references(message_ref, adapter_fingerprint, adapter_id, created_at) VALUES (?, ?, ?, ?)",
                (message_ref, fingerprint, adapter_id, time.time()),
            )
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp message-reference state is unavailable.") from exc
        return message_ref

    def resolve_message_ref(self, fingerprint: str, message_ref: str) -> str:
        try:
            row = self.connection.execute(
                "SELECT adapter_id, adapter_fingerprint FROM message_references WHERE message_ref = ?",
                (message_ref,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp message-reference state is unavailable.") from exc
        if row is None or row["adapter_fingerprint"] != fingerprint:
            raise PimcampError("not_found", "The message reference no longer resolves.")
        return str(row["adapter_id"])

    def store_cursor(self, fingerprint: str, adapter_cursor: str) -> str:
        cursor = str(uuid.uuid4())
        try:
            self.connection.execute(
                "INSERT INTO cursor_references(cursor, adapter_fingerprint, adapter_cursor, created_at) VALUES (?, ?, ?, ?)",
                (cursor, fingerprint, adapter_cursor, time.time()),
            )
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp cursor state is unavailable.") from exc
        return cursor

    def resolve_cursor(self, fingerprint: str, cursor: str) -> str:
        try:
            row = self.connection.execute(
                "SELECT adapter_cursor, adapter_fingerprint FROM cursor_references WHERE cursor = ?",
                (cursor,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp cursor state is unavailable.") from exc
        if row is None or row["adapter_fingerprint"] != fingerprint:
            raise PimcampError("invalid_request", "The paging cursor is invalid.")
        return str(row["adapter_cursor"])
