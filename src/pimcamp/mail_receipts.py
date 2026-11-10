"""Durable browser-reply operation claims and read-only status projection."""

from dataclasses import dataclass
import json
import sqlite3
import time
import uuid
from typing import Any

from .errors import PimcampError, unavailable
from .state import State


@dataclass(frozen=True)
class ReplyClaim:
    state: str
    attempt_id: str | None = None


class MailReplyReceipts:
    def __init__(self, state: State, *, ensure_schema: bool = True):
        self.state = state
        if ensure_schema:
            self.state.ensure_mail_schema()

    def prepare(self, principal_id: str, account_id: str, locator: str) -> str:
        operation_id = str(uuid.uuid4())
        now = time.time()
        try:
            self.state.connection.execute(
                """INSERT INTO mail_reply_operations(
                     operation_id, principal_id, account_id, locator,
                     request_digest, state, attempt_id, claim_deadline,
                     transport_call_began, result_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, NULL, 'prepared', NULL, NULL, 0, NULL, ?, ?)""",
                (operation_id, principal_id, account_id, locator, now, now),
            )
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp reply-operation state is unavailable.") from exc
        return operation_id

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        try:
            row = self.state.connection.execute(
                """SELECT operation_id, principal_id, account_id, locator
                   FROM mail_reply_operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp reply-operation state is unavailable.") from exc
        return None if row is None else dict(row)

    def claim(
        self,
        operation_id: str,
        principal_id: str,
        account_id: str,
        locator: str,
        request_digest: str,
        claim_deadline: float,
    ) -> ReplyClaim:
        connection = self.state.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT principal_id, account_id, locator, request_digest, state,
                          attempt_id, claim_deadline, transport_call_began
                   FROM mail_reply_operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
            if row is None or (
                row["principal_id"], row["account_id"], row["locator"]
            ) != (principal_id, account_id, locator):
                connection.execute("ROLLBACK")
                raise PimcampError("not_found", "The reply operation is unavailable.")
            if row["request_digest"] is not None and row["request_digest"] != request_digest:
                connection.execute("ROLLBACK")
                raise PimcampError("conflict", "The operation ID belongs to another reply.")

            state = row["state"]
            if state == "sent" or state == "unknown":
                connection.execute("COMMIT")
                return ReplyClaim(state)
            if state == "pending":
                deadline = row["claim_deadline"]
                if deadline is not None and float(deadline) > time.time():
                    connection.execute("COMMIT")
                    return ReplyClaim("pending")
                if row["transport_call_began"]:
                    connection.execute(
                        """UPDATE mail_reply_operations SET state = 'unknown', updated_at = ?
                           WHERE operation_id = ? AND state = 'pending'""",
                        (time.time(), operation_id),
                    )
                    connection.execute("COMMIT")
                    return ReplyClaim("unknown")
                state = "not_submitted"

            if state not in {"prepared", "not_submitted"}:
                connection.execute("ROLLBACK")
                raise unavailable("Pimcamp reply-operation state is invalid.")
            attempt_id = str(uuid.uuid4())
            connection.execute(
                """UPDATE mail_reply_operations
                   SET request_digest = ?, state = 'pending', attempt_id = ?,
                       claim_deadline = ?, transport_call_began = 0,
                       result_json = NULL, updated_at = ?
                   WHERE operation_id = ?""",
                (request_digest, attempt_id, claim_deadline, time.time(), operation_id),
            )
            connection.execute("COMMIT")
            return ReplyClaim("claimant", attempt_id)
        except PimcampError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise unavailable("Pimcamp could not claim the reply operation.") from exc

    def mark_transport_began(self, operation_id: str, attempt_id: str) -> None:
        try:
            cursor = self.state.connection.execute(
                """UPDATE mail_reply_operations SET transport_call_began = 1, updated_at = ?
                   WHERE operation_id = ? AND attempt_id = ? AND state = 'pending'""",
                (time.time(), operation_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise PimcampError("conflict", "The reply operation is no longer active.")
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp could not mark the reply submission boundary.") from exc

    def finish(self, operation_id: str, attempt_id: str, state: str) -> None:
        if state not in {"sent", "not_submitted", "unknown"}:
            raise ValueError("invalid browser reply terminal state")
        encoded = json.dumps({"status": state}, separators=(",", ":"), sort_keys=True)
        try:
            cursor = self.state.connection.execute(
                """UPDATE mail_reply_operations
                   SET state = ?, result_json = ?, updated_at = ?
                   WHERE operation_id = ? AND attempt_id = ? AND state = 'pending'""",
                (state, encoded, time.time(), operation_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise unavailable("Pimcamp could not record the reply outcome.")
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp could not record the reply outcome.") from exc

    def status(
        self,
        operation_id: str,
        principal_id: str,
        account_id: str,
    ) -> str:
        try:
            table = self.state.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mail_reply_operations'"
            ).fetchone()
            if table is None:
                return "absent"
            row = self.state.connection.execute(
                """SELECT principal_id, account_id, state, claim_deadline,
                          transport_call_began
                   FROM mail_reply_operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp reply status is unavailable.") from exc
        if row is None or (row["principal_id"], row["account_id"]) != (
            principal_id,
            account_id,
        ):
            return "absent"
        state = str(row["state"])
        if state == "prepared":
            return "not_submitted"
        if state == "pending":
            deadline = row["claim_deadline"]
            if deadline is not None and float(deadline) <= time.time():
                return "unknown" if row["transport_call_began"] else "not_submitted"
            return "pending"
        if state in {"sent", "not_submitted", "unknown"}:
            return state
        raise unavailable("Pimcamp reply status has an invalid state.")
