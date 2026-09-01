"""Cross-process durable mutation claims and immutable replay."""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .errors import PimcampError, unavailable
from .state import State


@dataclass
class Mutation:
    store: "ReceiptStore"
    key: tuple[str, str, str]
    claim_deadline: float
    lock_fd: int | None
    claimant: bool
    recorded: dict[str, Any] | None
    call_began: bool = False

    def mark_call_began(self) -> dict[str, Any] | None:
        recorded = self.store.mark_call_began(self.key, self.claim_deadline)
        if recorded is None:
            self.call_began = True
        return recorded

    def finish_success(self, result: dict[str, Any]) -> dict[str, Any]:
        return self.store.finish(
            self.key, self.claim_deadline, "succeeded", {"ok": result}
        )

    def finish_error(self, error: PimcampError) -> dict[str, Any]:
        state = "unknown" if error.code == "outcome_unknown" else "failed"
        return self.store.finish(
            self.key,
            self.claim_deadline,
            state,
            {"error": {"code": error.code, "message": error.message}},
        )

    def close(self) -> None:
        if self.lock_fd is None:
            return
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.lock_fd)
            self.lock_fd = None


class ReceiptStore:
    def __init__(self, state: State):
        self.state = state
        self.connection = state.connection
        self.lock_directory = state.path.parent / "mutation-locks"
        try:
            # SQLite's connection default may wait beyond this invocation's
            # absolute claim deadline. Receipt transactions retry explicitly
            # against that deadline instead.
            self.connection.execute("PRAGMA busy_timeout = 0")
            self.lock_directory.mkdir(mode=0o700, exist_ok=True)
            if self.lock_directory.stat().st_mode & 0o077:
                raise unavailable("Pimcamp mutation-lock state is not owner-only.")
        except (OSError, sqlite3.Error) as exc:
            raise unavailable("Pimcamp mutation-lock state is unavailable.") from exc

    def begin(
        self,
        client_identity: str,
        operation: str,
        mutation_id: str,
        request_digest: str,
        claim_deadline: float,
    ) -> Mutation:
        key = (client_identity, operation, mutation_id)
        lock_fd = self._open_lock(key)
        claimed = _try_lock(lock_fd)
        try:
            row = self._row_before_deadline(key, claim_deadline)
        except PimcampError:
            _unlock_close(lock_fd)
            raise

        while row is None and not claimed:
            if time.time() >= claim_deadline:
                _unlock_close(lock_fd)
                raise unavailable("Pimcamp could not reserve the mutation before its deadline.")
            time.sleep(0.005)
            claimed = _try_lock(lock_fd)
            try:
                row = self._row_before_deadline(key, claim_deadline)
            except PimcampError:
                _unlock_close(lock_fd)
                raise

        if row is None:
            try:
                self._begin_immediate(
                    claim_deadline,
                    "Pimcamp could not reserve the mutation before its deadline.",
                )
                if time.time() >= claim_deadline:
                    raise unavailable(
                        "Pimcamp could not reserve the mutation before its deadline."
                    )
                self.connection.execute(
                    """
                    INSERT INTO mutation_receipts(
                      client_identity, operation, mutation_id, request_digest,
                      mutation_call_began, state, claim_deadline, result_json
                    ) VALUES (?, ?, ?, ?, 0, 'reserved', ?, NULL)
                    """,
                    (*key, request_digest, claim_deadline),
                )
                self.connection.execute("COMMIT")
            except PimcampError:
                self._rollback()
                _unlock_close(lock_fd)
                raise
            except sqlite3.Error as exc:
                self._rollback()
                _unlock_close(lock_fd)
                raise unavailable("Pimcamp could not reserve the mutation receipt.") from exc
            return Mutation(self, key, claim_deadline, lock_fd, True, None)

        stored_deadline = float(row["claim_deadline"])
        if row["request_digest"] != request_digest:
            _unlock_close(lock_fd)
            raise PimcampError("conflict", "The mutation ID belongs to another request.")
        if row["state"] != "reserved":
            recorded = _recorded(row)
            _unlock_close(lock_fd)
            return Mutation(self, key, stored_deadline, None, False, recorded)
        if claimed:
            recorded = self._transition(key, stored_deadline, "abandoned")
            _unlock_close(lock_fd)
            return Mutation(self, key, stored_deadline, None, False, recorded)

        recorded = self._await_terminal(key, stored_deadline, lock_fd)
        return Mutation(self, key, stored_deadline, None, False, recorded)

    def mark_call_began(
        self, key: tuple[str, str, str], claim_deadline: float
    ) -> dict[str, Any] | None:
        try:
            self._begin_immediate(
                claim_deadline,
                "Pimcamp could not mark the mutation before its deadline.",
            )
            row = self._row(key)
            if row is None:
                raise unavailable("Pimcamp mutation receipt disappeared.")
            if row["state"] != "reserved":
                self.connection.execute("COMMIT")
                return _recorded(row)
            if time.time() >= row["claim_deadline"]:
                recorded = self._transition_locked(row)
                self.connection.execute("COMMIT")
                return recorded
            self.connection.execute(
                """
                UPDATE mutation_receipts
                SET mutation_call_began = 1
                WHERE client_identity = ? AND operation = ? AND mutation_id = ?
                  AND state = 'reserved'
                """,
                key,
            )
            self.connection.execute("COMMIT")
            return None
        except PimcampError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise unavailable("Pimcamp could not mark the mutation call boundary.") from exc

    def finish(
        self,
        key: tuple[str, str, str],
        claim_deadline: float,
        state: str,
        recorded: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            self._begin_immediate(
                claim_deadline,
                "Pimcamp could not record the mutation result before its deadline.",
            )
            row = self._row(key)
            if row is None:
                raise unavailable("Pimcamp mutation receipt disappeared.")
            if row["state"] != "reserved":
                self.connection.execute("COMMIT")
                return _recorded(row)
            if time.time() >= row["claim_deadline"]:
                result = self._transition_locked(row)
                self.connection.execute("COMMIT")
                return result
            self.connection.execute(
                """
                UPDATE mutation_receipts
                SET state = ?, result_json = ?
                WHERE client_identity = ? AND operation = ? AND mutation_id = ?
                  AND state = 'reserved'
                """,
                (state, _encode(recorded), *key),
            )
            self.connection.execute("COMMIT")
            return recorded
        except PimcampError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise unavailable("Pimcamp could not record the mutation result.") from exc

    def _await_terminal(
        self, key: tuple[str, str, str], claim_deadline: float, lock_fd: int
    ) -> dict[str, Any]:
        try:
            while True:
                row = self._row_before_deadline(key, claim_deadline)
                if row is None:
                    raise unavailable("Pimcamp mutation receipt disappeared.")
                if row["state"] != "reserved":
                    return _recorded(row)
                if time.time() >= claim_deadline:
                    return self._transition(key, claim_deadline, "expired")
                if _try_lock(lock_fd):
                    return self._transition(key, claim_deadline, "abandoned")
                time.sleep(
                    min(0.01, max(0.001, claim_deadline - time.time()))
                )
        finally:
            _unlock_close(lock_fd)

    def _transition(
        self, key: tuple[str, str, str], claim_deadline: float, reason: str
    ) -> dict[str, Any]:
        try:
            self._begin_immediate(
                claim_deadline,
                f"Pimcamp could not close the {reason} mutation claim before its deadline.",
            )
            row = self._row(key)
            if row is None:
                raise unavailable("Pimcamp mutation receipt disappeared.")
            result = (
                _recorded(row)
                if row["state"] != "reserved"
                else self._transition_locked(row)
            )
            self.connection.execute("COMMIT")
            return result
        except PimcampError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise unavailable(f"Pimcamp could not close the {reason} mutation claim.") from exc

    def _transition_locked(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["mutation_call_began"]:
            state = "unknown"
            recorded = {
                "error": {
                    "code": "outcome_unknown",
                    "message": "The mail mutation outcome is unknown.",
                }
            }
        else:
            state = "failed"
            recorded = {
                "error": {
                    "code": "backend_unavailable",
                    "message": "The mail mutation did not begin before its deadline.",
                }
            }
        self.connection.execute(
            """
            UPDATE mutation_receipts
            SET state = ?, result_json = ?
            WHERE client_identity = ? AND operation = ? AND mutation_id = ?
              AND state = 'reserved'
            """,
            (
                state,
                _encode(recorded),
                row["client_identity"],
                row["operation"],
                row["mutation_id"],
            ),
        )
        return recorded

    def _row(self, key: tuple[str, str, str]) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT client_identity, operation, mutation_id, request_digest,
                   mutation_call_began, state, claim_deadline, result_json
            FROM mutation_receipts
            WHERE client_identity = ? AND operation = ? AND mutation_id = ?
            """,
            key,
        ).fetchone()

    def _row_before_deadline(
        self, key: tuple[str, str, str], claim_deadline: float
    ) -> sqlite3.Row | None:
        while True:
            try:
                return self._row(key)
            except sqlite3.OperationalError as exc:
                if not _database_busy(exc) or time.time() >= claim_deadline:
                    raise unavailable(
                        "Pimcamp could not read the mutation receipt before its deadline."
                    ) from exc
                time.sleep(min(0.005, claim_deadline - time.time()))

    def _begin_immediate(self, claim_deadline: float, message: str) -> None:
        while True:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not _database_busy(exc) or time.time() >= claim_deadline:
                    raise unavailable(message) from exc
                time.sleep(min(0.005, claim_deadline - time.time()))

    def _open_lock(self, key: tuple[str, str, str]) -> int:
        digest = hashlib.sha256("\0".join(key).encode("utf-8")).hexdigest()
        path = self.lock_directory / f"{digest}.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            if Path(path).stat().st_mode & 0o077:
                os.close(fd)
                raise unavailable("Pimcamp mutation lock is not owner-only.")
            return fd
        except OSError as exc:
            raise unavailable("Pimcamp mutation lock is unavailable.") from exc

    def _rollback(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")


def replay(recorded: dict[str, Any]) -> dict[str, Any]:
    if set(recorded) == {"ok"} and isinstance(recorded["ok"], dict):
        return recorded["ok"]
    if set(recorded) != {"error"} or not isinstance(recorded["error"], dict):
        raise unavailable("Pimcamp mutation receipt is invalid.")
    error = recorded["error"]
    if set(error) != {"code", "message"} or not isinstance(error["message"], str):
        raise unavailable("Pimcamp mutation receipt is invalid.")
    raise PimcampError(error["code"], error["message"])


def _recorded(row: sqlite3.Row) -> dict[str, Any]:
    encoded = row["result_json"]
    if not isinstance(encoded, str):
        raise unavailable("Pimcamp terminal mutation receipt has no result.")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise unavailable("Pimcamp mutation receipt result is invalid.") from exc
    if not isinstance(value, dict):
        raise unavailable("Pimcamp mutation receipt result is invalid.")
    return value


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _database_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _unlock_close(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
