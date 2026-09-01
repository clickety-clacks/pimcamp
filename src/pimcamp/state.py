"""Stamped private state for opaque message references and mutation receipts."""

from pathlib import Path
import os
import sqlite3
import time
import uuid

from .errors import PimcampError, unavailable


_SCHEMA_VERSION = "pimcamp-state-v1"


class State:
    def __init__(self, path: Path):
        self.path = path
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

    def close(self) -> None:
        self.connection.close()

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
