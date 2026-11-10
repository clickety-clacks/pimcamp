"""JSON/stdio executable boundary."""

from datetime import datetime, timezone
import signal
import sys
import threading
import time

from . import ALL_CAPABILITIES, CONTRACT_VERSION, V2_CAPABILITIES, V2_CONTRACT_VERSION
from .adapters import CommandObservationAdapter, MiradorObservationAdapter
from .config import Config, HimalayaConfig, load
from .diagnostics import emit
from .errors import PimcampError, invalid
from .jsonio import dumps_line, loads_one
from .mail_service import AccountMailService
from .schema import validate_envelope
from .service import Service
from .state import State


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    operation = argv[0] if len(argv) == 1 else None
    if operation not in ALL_CAPABILITIES:
        return _finish_error(invalid("The command names no Pimcamp operation."), None, None, 0)

    started = time.monotonic()
    client_identity: str | None = None
    mutation_id: str | None = None
    adapter_class: str | None = None
    contract_version = CONTRACT_VERSION
    state: State | None = None
    try:
        request = loads_one(sys.stdin.buffer.read())
        if isinstance(request, dict) and request.get("contract_version") == V2_CONTRACT_VERSION:
            contract_version = V2_CONTRACT_VERSION
        credential, value = validate_envelope(operation, request)
        if credential is None:
            raise PimcampError("permission_denied", "The client is not permitted to use this operation.")
        config = load()
        adapter_class = (
            "mirador"
            if operation == "subscribe_new_mail" and config.observation.kind == "mirador"
            else "observation_command"
            if operation == "subscribe_new_mail"
            else "himalaya"
            if config.operations.kind == "himalaya"
            else "operations_command"
        )
        client = config.authenticate(credential)
        if client is None or operation not in client.grants:
            raise PimcampError("permission_denied", "The client is not permitted to use this operation.")
        client_identity = client.identity
        if operation in V2_CAPABILITIES and operation != "accounts":
            account_id = value["account_id"]
            if account_id not in client.account_ids or account_id not in config.mail_accounts:
                raise PimcampError("permission_denied", "The account is not permitted.")
        mutation_id = value.get("mutation_id")
        if operation == "subscribe_new_mail":
            return _subscribe(config, client_identity, started)
        if operation in V2_CAPABILITIES:
            if operation in {"list_account", "issue_link"}:
                state = State(config.state_path)
            result = AccountMailService(
                config, state, client_identity, client.account_ids
            ).execute(operation, value)
            sys.stdout.write(
                dumps_line({"contract_version": V2_CONTRACT_VERSION, "result": result})
            )
            sys.stdout.flush()
            emit(
                client_identity=client_identity,
                operation=operation,
                duration_ms=int((time.monotonic() - started) * 1000),
                result_code="success",
                adapter_class="mail_v2",
            )
            return 0
        if operation in {"list", "read", "reply", "send", "junk"}:
            state = State(config.state_path)
        result = Service(config, state, client_identity).execute(operation, value)
        sys.stdout.write(dumps_line({"contract_version": CONTRACT_VERSION, "result": result}))
        sys.stdout.flush()
        emit(
            client_identity=client_identity,
            operation=operation,
            mutation_id=mutation_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_code="success",
            adapter_class=adapter_class,
        )
        return 0
    except PimcampError as exc:
        return _finish_error(
            exc,
            operation,
            client_identity,
            started,
            mutation_id,
            adapter_class,
            contract_version,
        )
    except Exception:
        return _finish_error(
            PimcampError("backend_unavailable", "Pimcamp could not complete the operation."),
            operation,
            client_identity,
            started,
            mutation_id,
            adapter_class,
            contract_version,
        )
    finally:
        if state is not None:
            state.close()


def _subscribe(config: Config, client_identity: str, started: float) -> int:
    stop = threading.Event()
    inbox = (
        config.operations.inbox
        if isinstance(config.operations, HimalayaConfig)
        else None
    )
    adapter = (
        MiradorObservationAdapter(config.observation, inbox)
        if config.observation.kind == "mirador"
        else CommandObservationAdapter(config.observation)
    )
    previous_handler = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    try:
        opened = adapter.start(
            time.monotonic() + config.adapter_wait_seconds,
            stop,
        )
        if opened:
            sys.stdout.write(
                dumps_line(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "result": {"status": "started"},
                    }
                )
            )
            sys.stdout.flush()
        while opened and adapter.next_event(stop):
            if stop.is_set():
                break
            observed_at = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            sys.stdout.write(
                dumps_line(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "kind": "new_mail",
                        "mailbox": "inbox",
                        "observed_at": observed_at,
                    }
                )
            )
            sys.stdout.flush()
        emit(
            client_identity=client_identity,
            operation="subscribe_new_mail",
            duration_ms=int((time.monotonic() - started) * 1000),
            result_code="success",
            adapter_class=adapter.adapter_class,
        )
        return 0
    finally:
        adapter.close(time.monotonic() + config.adapter_wait_seconds)
        signal.signal(signal.SIGTERM, previous_handler)


def _finish_error(
    error: PimcampError,
    operation: str | None,
    client_identity: str | None,
    started: float,
    mutation_id: str | None = None,
    adapter_class: str | None = None,
    contract_version: str = CONTRACT_VERSION,
) -> int:
    sys.stdout.write(dumps_line(error.public_value(contract_version)))
    sys.stdout.flush()
    duration = int((time.monotonic() - started) * 1000) if started else 0
    emit(
        client_identity=client_identity,
        operation=operation,
        mutation_id=mutation_id,
        duration_ms=duration,
        result_code=error.code,
        adapter_class=adapter_class,
    )
    return 1
