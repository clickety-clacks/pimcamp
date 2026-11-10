"""Account-scoped v2 link, reader, reply and status services."""

from dataclasses import replace
from html.parser import HTMLParser
import hashlib
import json
import time
from typing import Any, Callable

from .config import Config, MailAccountConfig
from .errors import NotSubmittedError, PimcampError, unavailable
from .identity import AmbiguousIdentityError, resolve_identity, validate_identity_evidence
from .jsonio import canonical_json
from .locators import LocatorStore, cannot_link, link_result
from .mail_receipts import MailReplyReceipts
from .schema import validate_public_address, validate_rfc3339_utc
from .service import Service
from .state import State


class AccountMailService:
    def __init__(
        self,
        config: Config,
        state: State | None,
        principal_id: str,
        allowed_account_ids: set[str] | frozenset[str],
        *,
        status_reconciler: Callable[[str, str, str], str | None] | None = None,
    ):
        self.config = config
        self.state = state
        self.principal_id = principal_id
        self.allowed_account_ids = frozenset(allowed_account_ids)
        self.status_reconciler = status_reconciler

    def execute(self, operation: str, value: dict[str, Any]) -> dict[str, Any]:
        if operation == "accounts":
            return self.accounts()
        if operation == "account_info":
            return self.account_info(value["account_id"])
        if operation == "list_account":
            return self.list_account(value)
        if operation == "issue_link":
            return self.issue_link(value["account_id"], value["source_ref"])
        raise PimcampError("unsupported", "The account operation is unsupported.")

    def accounts(self) -> dict[str, Any]:
        return {
            "accounts": [
                {"account_id": account_id, "label": self.config.mail_accounts[account_id].label}
                for account_id in sorted(
                    self.allowed_account_ids.intersection(self.config.mail_accounts)
                )
            ]
        }

    def account_info(self, account_id: str) -> dict[str, Any]:
        account = self._account(account_id)
        selectable = [
            {**item["from"], "provenance": item["provenance"], "validity": item["validity"]}
            for item in account.selectable_from
            if item["validity"] == "valid"
        ]
        return {
            "account_id": account.account_id,
            "label": account.label,
            "primary_from": account.primary_from,
            "selectable_from": selectable,
            "alias_discovery_state": account.alias_discovery_state,
            "identity_capability": account.identity_capability,
        }

    def list_account(self, value: dict[str, Any]) -> dict[str, Any]:
        account = self._account(value["account_id"])
        state = self._state()
        state.ensure_mail_schema()
        service = self._service(account)
        fingerprint = _account_scope(account)
        adapter_input: dict[str, Any] = {"limit": value["limit"]}
        if "cursor" in value:
            adapter_input["cursor"] = state.resolve_cursor(fingerprint, value["cursor"])
        adapter_input["include_identity_evidence"] = True
        result = service.operations.call("list", adapter_input, service._deadline())
        if (
            not isinstance(result, dict)
            or set(result) != {"messages", "next_cursor"}
            or not isinstance(result["messages"], list)
            or (result["next_cursor"] is not None and not isinstance(result["next_cursor"], str))
        ):
            raise unavailable("The operations adapter returned an invalid account list.")
        store = LocatorStore(state, ensure_schema=False)
        messages = []
        for item in result["messages"]:
            summary, adapter_id, evidence = _summary(item)
            evidence = _bind_evidence(account, evidence)
            source_ref = store.store_source(
                account.account_id,
                account.operations_fingerprint,
                adapter_id,
                evidence,
            )
            messages.append({"source_ref": source_ref, **summary})
        next_cursor = result["next_cursor"]
        public_cursor = (
            None
            if next_cursor is None
            else state.store_cursor(fingerprint, next_cursor)
        )
        return {
            "account_id": account.account_id,
            "messages": messages,
            "next_cursor": public_cursor,
        }

    def issue_link(self, account_id: str, source_ref: str) -> dict[str, Any]:
        account = self._account(account_id)
        if self.config.mail_origin is None:
            return cannot_link("mail_origin_unconfigured")
        state = self._state()
        state.ensure_mail_schema()
        source = LocatorStore(state, ensure_schema=False).source(source_ref)
        if source is None:
            return cannot_link("source_not_authoritative")
        if source["account_id"] != account_id:
            return cannot_link("account_mismatch")
        if source["adapter_fingerprint"] != account.operations_fingerprint:
            return cannot_link("source_context_changed")
        evidence = source["identity_evidence"]
        if account.address is None:
            return cannot_link("account_identity_unconfigured")
        if evidence is None:
            return cannot_link("identity_evidence_missing")
        if account.identity_capability not in {"native", "mapped"}:
            return cannot_link("identity_capability_unproven")
        if evidence["continuity"] != account.identity_capability:
            return cannot_link("identity_capability_mismatch")
        try:
            adapter_evidence = _adapter_evidence(account, evidence)
        except PimcampError:
            return cannot_link("account_identity_changed")
        try:
            service = self._service(account)
            adapter_id = resolve_identity(
                service.operations,
                adapter_evidence,
                service._deadline(),
            )
        except AmbiguousIdentityError:
            return cannot_link("ambiguous_identity")
        except PimcampError as exc:
            if exc.code == "conflict":
                return cannot_link("identity_not_resolvable")
            if exc.code == "unsupported":
                return cannot_link("identity_resolution_unsupported")
            if exc.code == "not_found":
                return cannot_link("identity_not_found")
            raise
        if not isinstance(adapter_id, str) or not adapter_id:
            return cannot_link("identity_not_resolvable")
        locator = LocatorStore(state, ensure_schema=False).issue(account_id, evidence)
        url = f"{self.config.mail_origin}/mail/m/{locator}"
        return link_result(account_id, locator, url)

    def locator_account(self, locator: str) -> str:
        record = LocatorStore(self._state(), ensure_schema=False).resolve(locator)
        if record is None:
            raise PimcampError("not_found", "The message locator does not resolve.")
        return str(record["account_id"])

    def get_message(self, locator: str) -> dict[str, Any]:
        record, account = self._authorized_locator(locator)
        adapter_id = self._resolve_locator(account, record["identity_evidence"])
        message = self._service(account).read_adapter_message(adapter_id)
        return {
            "account_id": account.account_id,
            "account_label": account.label,
            "locator": locator,
            "source": {
                "from": message["from"],
                "to": message["to"],
                "cc": message["cc"],
                "reply_to": message["reply_to"],
                "subject": message["subject"],
                "date": message["sent_at"],
            },
            "body_parts": _readable_body_parts(message["body_parts"]),
        }

    def prepare_reply(self, locator: str, body_text: str) -> dict[str, Any]:
        record, account = self._authorized_locator(locator)
        adapter_id = self._resolve_locator(account, record["identity_evidence"])
        message = self._service(account).read_adapter_message(adapter_id)
        recipients = message["reply_to"] or message["from"][:1]
        if not recipients:
            raise PimcampError("invalid_request", "The source has no valid reply recipient.")
        subject = message["subject"]
        if subject[:3].lower() != "re:":
            subject = f"Re: {subject}"
        selectable = [
            {**item["from"], "provenance": item["provenance"], "validity": item["validity"]}
            for item in account.selectable_from
            if item["validity"] == "valid"
        ]
        default_from = selectable[0] if len(selectable) == 1 else None
        operation_id = MailReplyReceipts(self._state()).prepare(
            self.principal_id, account.account_id, locator
        )
        return {
            "operation_id": operation_id,
            "account_id": account.account_id,
            "account_label": account.label,
            "locator": locator,
            "source": {
                "from": message["from"],
                "to": message["to"],
                "cc": message["cc"],
                "reply_to": message["reply_to"],
                "subject": message["subject"],
                "date": message["sent_at"],
            },
            "reply": {
                "from_choices": selectable,
                "selected_from": default_from,
                "to": recipients,
                "cc": [],
                "bcc": [],
                "subject": subject,
                "body_text": body_text,
            },
        }

    def submit_reply(
        self,
        operation_id: str,
        value: dict[str, Any],
        *,
        reauthorize: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        account = self._account(value["account_id"])
        state = self._state()
        state.ensure_mail_schema()
        receipts = MailReplyReceipts(state, ensure_schema=False)
        operation = receipts.operation(operation_id)
        if (
            operation is None
            or operation["principal_id"] != self.principal_id
            or operation["account_id"] != value["account_id"]
            or operation["locator"] != value["locator"]
        ):
            raise PimcampError("not_found", "The reply operation is unavailable.")
        digest = hashlib.sha256(canonical_json(value)).hexdigest()
        claim = receipts.claim(
            operation_id,
            self.principal_id,
            account.account_id,
            value["locator"],
            digest,
            time.time() + self.config.adapter_wait_seconds,
        )
        if claim.state == "sent":
            return {"status": "sent", "operation_id": operation_id}
        if claim.state == "pending":
            return {"status": "pending", "operation_id": operation_id}
        if claim.state == "unknown":
            raise PimcampError("outcome_unknown", "The reply send outcome is unknown.")
        if claim.state != "claimant" or claim.attempt_id is None:
            raise unavailable("Pimcamp reply operation could not be claimed.")

        call_began = False
        try:
            if reauthorize is not None:
                reauthorize(account.account_id)
            account = self._account(account.account_id)
            sender = _authorized_sender(account, value["from"])
            record, current_account = self._authorized_locator(value["locator"])
            if current_account.account_id != account.account_id:
                raise PimcampError("permission_denied", "The source is outside the selected account.")
            adapter_id = self._resolve_locator(account, record["identity_evidence"])
            source = self._service(account).read_adapter_message(adapter_id)
            threading = source["threading"]
            composition = {
                "kind": "reply",
                "to": value["to"],
                "cc": value["cc"],
                "bcc": value["bcc"],
                "subject": value["subject"],
                "body_text": value["body_text"],
                "reply_to_message_ref": None,
            }
            receipts.mark_transport_began(operation_id, claim.attempt_id)
            call_began = True
            result = self._service(account).operations.call(
                "send",
                {
                    "composition": composition,
                    "threading": threading,
                    "sender": sender,
                    "_pimcamp_browser_reply": True,
                },
                self._service(account)._deadline(),
                mutation=True,
            )
            if not isinstance(result, dict) or result != {"status": "sent"}:
                raise PimcampError("outcome_unknown", "The reply send outcome is unknown.")
            receipts.finish(operation_id, claim.attempt_id, "sent")
            return {"status": "sent", "operation_id": operation_id}
        except NotSubmittedError:
            receipts.finish(operation_id, claim.attempt_id, "not_submitted")
            raise
        except PimcampError as exc:
            state = "unknown" if call_began else "not_submitted"
            try:
                receipts.finish(operation_id, claim.attempt_id, state)
            except PimcampError as receipt_error:
                if call_began:
                    raise PimcampError(
                        "outcome_unknown", "The reply send outcome is unknown."
                    ) from receipt_error
                raise
            if call_began:
                raise PimcampError(
                    "outcome_unknown", "The reply send outcome is unknown."
                ) from exc
            raise
        except Exception as exc:
            try:
                receipts.finish(
                    operation_id,
                    claim.attempt_id,
                    "unknown" if call_began else "not_submitted",
                )
            except PimcampError:
                pass
            if call_began:
                raise PimcampError(
                    "outcome_unknown", "The reply send outcome is unknown."
                ) from exc
            raise unavailable("Pimcamp could not prepare the reply submission.") from exc

    def send_status(self, account_id: str, operation_id: str) -> dict[str, Any]:
        account = self._account(account_id)
        receipts = MailReplyReceipts(self._state(), ensure_schema=False)
        status = receipts.status(operation_id, self.principal_id, account.account_id)
        if self.status_reconciler is not None and status in {"pending", "unknown"}:
            try:
                reconciled = self.status_reconciler(
                    self.principal_id, account.account_id, operation_id
                )
            except Exception:
                reconciled = None
            if reconciled in {"sent", "not_submitted", "unknown"}:
                status = reconciled
        return {
            "account_id": account.account_id,
            "operation_id": operation_id,
            "status": status,
            "retry_eligible": status == "not_submitted",
        }

    def _authorized_locator(
        self, locator: str
    ) -> tuple[dict[str, Any], MailAccountConfig]:
        record = LocatorStore(self._state(), ensure_schema=False).resolve(locator)
        if record is None:
            raise PimcampError("not_found", "The message locator does not resolve.")
        account = self._account(record["account_id"])
        return record, account

    def _resolve_locator(self, account: MailAccountConfig, evidence: dict[str, Any]) -> str:
        if account.identity_capability not in {"native", "mapped"}:
            raise PimcampError("unsupported", "Stable message identity is not established.")
        if evidence["continuity"] != account.identity_capability:
            raise PimcampError("conflict", "The message identity capability has changed.")
        service = self._service(account)
        return resolve_identity(service.operations, _adapter_evidence(account, evidence), service._deadline())

    def _account(self, account_id: str) -> MailAccountConfig:
        if account_id not in self.allowed_account_ids:
            raise PimcampError("permission_denied", "The account is not permitted.")
        account = self.config.mail_accounts.get(account_id)
        if account is None:
            raise PimcampError("permission_denied", "The account is not permitted.")
        return account

    def _service(self, account: MailAccountConfig) -> Service:
        account_config = replace(
            self.config,
            operations=account.operations,
            operations_fingerprint=account.operations_fingerprint,
        )
        identity = f"{self.principal_id}\0{account.account_id}"
        return Service(account_config, self.state, identity)

    def _state(self) -> State:
        if self.state is None:
            raise unavailable("Pimcamp private state is unavailable.")
        return self.state


def _account_scope(account: MailAccountConfig) -> str:
    scope = f"{account.account_id}\0{account.operations_fingerprint}".encode("utf-8")
    return hashlib.sha256(scope).hexdigest()


def _binding_prefix(account: MailAccountConfig) -> str:
    if account.address is None:
        raise PimcampError("unsupported", "The account address is not configured.")
    binding = canonical_json([account.account_id, account.address])
    return "pimcamp.account.v1:" + hashlib.sha256(binding).hexdigest() + ":"


def _bind_evidence(account: MailAccountConfig, evidence: dict[str, Any] | None):
    if evidence is None or account.address is None:
        return None
    return {**evidence, "namespace": _binding_prefix(account) + evidence["namespace"]}


def _adapter_evidence(account: MailAccountConfig, evidence: dict[str, Any]):
    prefix = _binding_prefix(account)
    if not evidence["namespace"].startswith(prefix):
        raise PimcampError("conflict", "The account binding changed; the message cannot be resolved.")
    return {**evidence, "namespace": evidence["namespace"][len(prefix):]}


def _summary(value: Any) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    expected = {"adapter_id", "from", "subject", "received_at", "unread"}
    if not isinstance(value, dict) or not expected.issubset(value) or not set(value).issubset(
        expected | {"identity_evidence"}
    ):
        raise unavailable("The operations adapter returned an invalid message summary.")
    adapter_id = value["adapter_id"]
    if (
        not isinstance(adapter_id, str)
        or not adapter_id
        or not isinstance(value["from"], list)
        or not isinstance(value["subject"], str)
        or not isinstance(value["unread"], bool)
    ):
        raise unavailable("The operations adapter returned an invalid message summary.")
    try:
        senders = [validate_public_address(item) for item in value["from"]]
        received_at = validate_rfc3339_utc(value["received_at"])
    except PimcampError as exc:
        raise unavailable("The operations adapter returned an invalid message summary.") from exc
    evidence = validate_identity_evidence(value.get("identity_evidence"))
    return (
        {
            "from": senders,
            "subject": value["subject"],
            "received_at": received_at,
            "unread": value["unread"],
        },
        adapter_id,
        evidence,
    )


def _authorized_sender(account: MailAccountConfig, value: dict[str, Any]) -> dict[str, str | None]:
    for item in account.selectable_from:
        if item["validity"] == "valid" and item["from"] == value:
            return item["from"]
    raise PimcampError("permission_denied", "The selected sender is not authorized.")


class _ReadableHTML(HTMLParser):
    _hidden = {"script", "style", "template", "noscript", "iframe", "object"}
    _breaks = {"address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "li", "p", "pre", "section", "td", "th", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._hidden:
            self.hidden_depth += 1
        elif tag in self._breaks and not self.hidden_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._hidden and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in self._breaks and not self.hidden_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _readable_body_parts(parts: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for part in parts:
        content = part["content_utf8"]
        if part["media_type"] == "text/html":
            parser = _ReadableHTML()
            try:
                parser.feed(content)
                parser.close()
            except Exception as exc:
                raise unavailable("The HTML message body could not be read as text.") from exc
            content = "".join(parser.parts)
        if content:
            result.append({"media_type": "text/plain", "content_utf8": content})
    return result
