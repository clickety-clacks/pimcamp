"""Opaque, account-bound source references and stable message locators."""

import json
import secrets
import sqlite3
import time
from typing import Any

from .errors import PimcampError, unavailable
from .identity import validate_identity_evidence
from .jsonio import canonical_json
from .state import State


class LocatorStore:
    def __init__(self, state: State, *, ensure_schema: bool = True):
        self.state = state
        if ensure_schema:
            self.state.ensure_mail_schema()

    def store_source(
        self,
        account_id: str,
        adapter_fingerprint: str,
        adapter_id: str,
        identity_evidence: dict[str, Any] | None,
    ) -> str:
        source_ref = secrets.token_urlsafe(24)
        encoded = (
            None
            if identity_evidence is None
            else canonical_json(identity_evidence).decode("utf-8")
        )
        try:
            self.state.connection.execute(
                """INSERT INTO mail_source_references(
                     source_ref, account_id, adapter_fingerprint, adapter_id,
                     identity_evidence_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (source_ref, account_id, adapter_fingerprint, adapter_id, encoded, time.time()),
            )
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp source-reference state is unavailable.") from exc
        return source_ref

    def source(self, source_ref: str) -> dict[str, Any] | None:
        try:
            if self.state.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mail_source_references'"
            ).fetchone() is None:
                return None
            row = self.state.connection.execute(
                """SELECT account_id, adapter_fingerprint, adapter_id,
                          identity_evidence_json
                   FROM mail_source_references WHERE source_ref = ?""",
                (source_ref,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp source-reference state is unavailable.") from exc
        if row is None:
            return None
        evidence = None
        if row["identity_evidence_json"] is not None:
            try:
                evidence = json.loads(row["identity_evidence_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise unavailable("Pimcamp source identity state is invalid.") from exc
            evidence = validate_identity_evidence(evidence)
            if evidence is None:
                raise unavailable("Pimcamp source identity state is invalid.")
        return {
            "account_id": row["account_id"],
            "adapter_fingerprint": row["adapter_fingerprint"],
            "adapter_id": row["adapter_id"],
            "identity_evidence": evidence,
        }

    def issue(self, account_id: str, evidence: dict[str, Any]) -> str:
        try:
            self.state.connection.execute(
                """INSERT OR IGNORE INTO mail_locators(
                     locator, account_id, identity_namespace, identity_key,
                     identity_evidence_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    secrets.token_urlsafe(32),
                    account_id,
                    evidence["namespace"],
                    evidence["key"],
                    canonical_json(evidence).decode("utf-8"),
                    time.time(),
                ),
            )
            row = self.state.connection.execute(
                """SELECT locator FROM mail_locators
                   WHERE account_id = ? AND identity_namespace = ? AND identity_key = ?""",
                (account_id, evidence["namespace"], evidence["key"]),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp locator state is unavailable.") from exc
        if row is None:
            raise unavailable("Pimcamp locator state is unavailable.")
        return str(row["locator"])

    def resolve(self, locator: str) -> dict[str, Any] | None:
        try:
            if self.state.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mail_locators'"
            ).fetchone() is None:
                return None
            row = self.state.connection.execute(
                """SELECT account_id, identity_evidence_json FROM mail_locators
                   WHERE locator = ?""",
                (locator,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise unavailable("Pimcamp locator state is unavailable.") from exc
        if row is None:
            return None
        try:
            evidence = json.loads(row["identity_evidence_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise unavailable("Pimcamp locator identity state is invalid.") from exc
        evidence = validate_identity_evidence(evidence)
        if evidence is None:
            raise unavailable("Pimcamp locator identity state is invalid.")
        return {"account_id": row["account_id"], "identity_evidence": evidence}


def cannot_link(reason: str) -> dict[str, Any]:
    return {"status": "cannot_link", "reason": reason}


def link_result(account_id: str, locator: str, url: str) -> dict[str, Any]:
    return {
        "status": "linked",
        "account_id": account_id,
        "locator": locator,
        "url": url,
    }
