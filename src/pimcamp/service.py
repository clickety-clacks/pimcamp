"""The seven-capability application boundary."""

import hashlib
import time
from typing import Any

from .adapters import CommandOperationsAdapter
from .config import Config
from .errors import PimcampError, unavailable
from .jsonio import canonical_json
from .receipts import Mutation, ReceiptStore, replay
from .schema import validate_public_address, validate_rfc3339_utc
from .state import State


class Service:
    def __init__(self, config: Config, state: State | None, client_identity: str):
        self.config = config
        self.state = state
        if config.operations.kind == "himalaya":
            from .himalaya import HimalayaOperationsAdapter

            self.operations = HimalayaOperationsAdapter(config.operations)
        else:
            self.operations = CommandOperationsAdapter(config.operations)
        self.client_identity = client_identity

    def execute(self, operation: str, value: dict[str, Any]) -> dict[str, Any]:
        if operation == "list":
            return self.list_messages(value)
        if operation == "read":
            return self.read_message(value["message_ref"])
        if operation == "compose":
            return self.compose(value)
        if operation == "reply":
            return self.reply(value)
        if operation == "send":
            return self.send(value)
        if operation == "junk":
            return self.junk(value)
        raise unavailable("The observation service is not initialized.")

    def _deadline(self) -> float:
        return time.monotonic() + self.config.adapter_wait_seconds

    def list_messages(self, value: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        adapter_input = {"limit": value["limit"]}
        if "cursor" in value:
            adapter_input["cursor"] = state.resolve_cursor(
                self.config.operations_fingerprint, value["cursor"]
            )
        result = self.operations.call("list", adapter_input, self._deadline())
        if not isinstance(result, dict) or set(result) != {"messages", "next_cursor"}:
            raise unavailable("The operations adapter returned an invalid list result.")
        if not isinstance(result["messages"], list):
            raise unavailable("The operations adapter returned an invalid list result.")
        next_cursor = result["next_cursor"]
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise unavailable("The operations adapter returned an invalid list result.")
        messages = [self._summary(item, state) for item in result["messages"]]
        public_cursor = (
            None
            if next_cursor is None
            else state.store_cursor(self.config.operations_fingerprint, next_cursor)
        )
        return {"messages": messages, "next_cursor": public_cursor}

    def _summary(self, value: Any, state: State) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "adapter_id",
            "from",
            "subject",
            "received_at",
            "unread",
        }:
            raise unavailable("The operations adapter returned an invalid message summary.")
        if not isinstance(value["adapter_id"], str) or not isinstance(value["from"], list):
            raise unavailable("The operations adapter returned an invalid message summary.")
        if not isinstance(value["subject"], str) or not isinstance(value["unread"], bool):
            raise unavailable("The operations adapter returned an invalid message summary.")
        message_ref = state.store_message_ref(
            self.config.operations_fingerprint, value["adapter_id"]
        )
        return {
            "message_ref": message_ref,
            "from": [_adapter_address(item) for item in value["from"]],
            "subject": value["subject"],
            "received_at": _adapter_time(value["received_at"]),
            "unread": value["unread"],
        }

    def read_message(self, message_ref: str) -> dict[str, Any]:
        value = self._read_internal(message_ref)
        return {
            "message_ref": message_ref,
            "from": value["from"],
            "to": value["to"],
            "cc": value["cc"],
            "subject": value["subject"],
            "sent_at": value["sent_at"],
            "body_parts": value["body_parts"],
        }

    def _read_internal(
        self, message_ref: str, deadline: float | None = None
    ) -> dict[str, Any]:
        adapter_id = self._state().resolve_message_ref(
            self.config.operations_fingerprint, message_ref
        )
        result = self.operations.call(
            "read", {"adapter_id": adapter_id}, deadline or self._deadline()
        )
        if not isinstance(result, dict) or set(result) != {
            "from",
            "to",
            "cc",
            "reply_to",
            "subject",
            "sent_at",
            "body_parts",
            "threading",
        }:
            raise unavailable("The operations adapter returned an invalid message.")
        for field in ("from", "to", "cc", "reply_to"):
            if not isinstance(result[field], list):
                raise unavailable("The operations adapter returned an invalid message.")
            result[field] = [_adapter_address(item) for item in result[field]]
        if not isinstance(result["subject"], str) or not isinstance(result["threading"], dict):
            raise unavailable("The operations adapter returned an invalid message.")
        result["sent_at"] = _adapter_time(result["sent_at"])
        if not isinstance(result["body_parts"], list):
            raise unavailable("The operations adapter returned an invalid message.")
        parts = []
        for part in result["body_parts"]:
            if (
                not isinstance(part, dict)
                or set(part) != {"media_type", "content_utf8"}
                or part.get("media_type") not in {"text/plain", "text/html"}
                or not isinstance(part.get("content_utf8"), str)
            ):
                raise unavailable("The operations adapter returned an invalid message body.")
            parts.append(part)
        result["body_parts"] = parts
        return result

    def compose(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "new",
            "to": value["to"],
            "cc": value["cc"],
            "bcc": value["bcc"],
            "subject": value["subject"],
            "body_text": value["body_text"],
            "reply_to_message_ref": None,
        }

    def reply(self, value: dict[str, Any]) -> dict[str, Any]:
        source = self._read_internal(value["message_ref"])
        recipients = source["reply_to"] or source["from"]
        if not recipients:
            raise PimcampError("invalid_request", "The source has no reply recipient.")
        subject = source["subject"]
        if not subject[:3].lower() == "re:":
            subject = f"Re: {subject}"
        return {
            "kind": "reply",
            "to": [recipients[0]],
            "cc": [],
            "bcc": [],
            "subject": subject,
            "body_text": value["body_text"],
            "reply_to_message_ref": value["message_ref"],
        }

    def send(self, value: dict[str, Any]) -> dict[str, Any]:
        deadline = self._deadline()
        mutation = self._begin_mutation("send", value, deadline)
        try:
            if not mutation.claimant:
                assert mutation.recorded is not None
                return replay(mutation.recorded)
            composition = value["composition"]
            threading: dict[str, Any] | None = None
            if composition["kind"] == "reply":
                source = self._read_internal(
                    composition["reply_to_message_ref"], deadline
                )
                threading = source["threading"]
            recorded = mutation.mark_call_began()
            if recorded is not None:
                return replay(recorded)
            payload = {"composition": composition, "threading": threading}
            result = self.operations.call("send", payload, deadline, mutation=True)
            if not isinstance(result, dict) or result != {"status": "sent"}:
                raise PimcampError(
                    "outcome_unknown", "The mail mutation outcome is unknown."
                )
            public = {"status": "sent", "mutation_id": value["mutation_id"]}
            return _finish_proved_success(
                mutation,
                public,
                "The mail mutation outcome is unknown.",
            )
        except PimcampError as exc:
            if not mutation.claimant:
                raise
            return _finish_with_error(mutation, exc)
        except Exception as exc:
            error = PimcampError(
                "outcome_unknown" if mutation.call_began else "backend_unavailable",
                "The mail mutation outcome is unknown."
                if mutation.call_began
                else "The mail mutation did not begin.",
            )
            return _finish_with_error(mutation, error, exc)
        finally:
            mutation.close()

    def junk(self, value: dict[str, Any]) -> dict[str, Any]:
        deadline = self._deadline()
        mutation = self._begin_mutation("junk", value, deadline)
        try:
            if not mutation.claimant:
                assert mutation.recorded is not None
                return replay(mutation.recorded)
            adapter_id = self._state().resolve_message_ref(
                self.config.operations_fingerprint, value["message_ref"]
            )
            capabilities = self.operations.call("junk_capabilities", {}, deadline)
            if (
                not isinstance(capabilities, dict)
                or set(capabilities) != {"report_spam", "move_to_junk"}
                or not all(isinstance(item, bool) for item in capabilities.values())
            ):
                raise unavailable(
                    "The operations adapter returned invalid junk capabilities."
                )
            if capabilities["report_spam"]:
                mechanism = "report_spam"
            elif capabilities["move_to_junk"]:
                mechanism = "move_to_junk"
            else:
                raise PimcampError(
                    "unsupported", "No supported junk filing mechanism is available."
                )
            recorded = mutation.mark_call_began()
            if recorded is not None:
                return replay(recorded)
            result = self.operations.call(
                mechanism,
                {"adapter_id": adapter_id},
                deadline,
                mutation=True,
            )
            if result != {"filed": True}:
                raise PimcampError(
                    "outcome_unknown", "The junk filing outcome is unknown."
                )
            public = {
                "status": "filed",
                "mutation_id": value["mutation_id"],
                "mechanism": mechanism,
            }
            return _finish_proved_success(
                mutation,
                public,
                "The junk filing outcome is unknown.",
            )
        except PimcampError as exc:
            if not mutation.claimant:
                raise
            return _finish_with_error(mutation, exc)
        except Exception as exc:
            error = PimcampError(
                "outcome_unknown" if mutation.call_began else "backend_unavailable",
                "The junk filing outcome is unknown."
                if mutation.call_began
                else "The junk filing did not begin.",
            )
            return _finish_with_error(mutation, error, exc)
        finally:
            mutation.close()

    def _begin_mutation(
        self, operation: str, value: dict[str, Any], adapter_deadline: float
    ) -> Mutation:
        digest_value = {
            key: item for key, item in value.items() if key != "mutation_id"
        }
        digest = hashlib.sha256(canonical_json(digest_value)).hexdigest()
        remaining = max(0.0, adapter_deadline - time.monotonic())
        claim_deadline = time.time() + remaining
        return ReceiptStore(self._state()).begin(
            self.client_identity,
            operation,
            value["mutation_id"],
            digest,
            claim_deadline,
        )

    def _state(self) -> State:
        if self.state is None:
            raise unavailable("Pimcamp private state is unavailable.")
        return self.state


def _adapter_address(value: Any) -> dict[str, str | None]:
    try:
        return validate_public_address(value)
    except PimcampError as exc:
        raise unavailable("The operations adapter returned an invalid address.") from exc


def _adapter_time(value: Any) -> str | None:
    try:
        return validate_rfc3339_utc(value)
    except PimcampError as exc:
        raise unavailable("The operations adapter returned an invalid UTC time.") from exc


def _finish_proved_success(
    mutation: Mutation,
    public: dict[str, Any],
    unknown_message: str,
) -> dict[str, Any]:
    try:
        recorded = mutation.finish_success(public)
    except PimcampError as store_error:
        unknown = PimcampError("outcome_unknown", unknown_message)
        return _finish_with_error(mutation, unknown, store_error)
    return replay(recorded)


def _finish_with_error(
    mutation: Mutation,
    error: PimcampError,
    cause: Exception | None = None,
) -> dict[str, Any]:
    try:
        recorded = mutation.finish_error(error)
    except PimcampError as store_error:
        if error.code == "outcome_unknown":
            raise error from (cause or store_error)
        raise
    return replay(recorded)
