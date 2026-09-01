"""JSON/stdio executable boundary."""

import sys
import time

from . import CAPABILITIES, CONTRACT_VERSION
from .config import load
from .diagnostics import emit
from .errors import PimcampError, invalid
from .jsonio import dumps_line, loads_one
from .schema import validate_envelope
from .service import Service
from .state import State


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    operation = argv[0] if len(argv) == 1 else None
    if operation not in CAPABILITIES:
        return _finish_error(invalid("The command names no Pimcamp operation."), None, None, 0)

    started = time.monotonic()
    client_identity: str | None = None
    mutation_id: str | None = None
    state: State | None = None
    try:
        request = loads_one(sys.stdin.buffer.read())
        credential, value = validate_envelope(operation, request)
        if credential is None:
            raise PimcampError("permission_denied", "The client is not permitted to use this operation.")
        config = load()
        client = config.authenticate(credential)
        if client is None or operation not in client.grants:
            raise PimcampError("permission_denied", "The client is not permitted to use this operation.")
        client_identity = client.identity
        mutation_id = value.get("mutation_id")
        if operation in {"list", "read", "reply", "send", "junk"}:
            state = State(config.state_path)
        result = Service(config, state).execute(operation, value)
        sys.stdout.write(dumps_line({"contract_version": CONTRACT_VERSION, "result": result}))
        sys.stdout.flush()
        emit(
            client_identity=client_identity,
            operation=operation,
            mutation_id=mutation_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_code="success",
        )
        return 0
    except PimcampError as exc:
        return _finish_error(exc, operation, client_identity, started, mutation_id)
    except Exception:
        return _finish_error(
            PimcampError("backend_unavailable", "Pimcamp could not complete the operation."),
            operation,
            client_identity,
            started,
            mutation_id,
        )
    finally:
        if state is not None:
            state.close()


def _finish_error(
    error: PimcampError,
    operation: str | None,
    client_identity: str | None,
    started: float,
    mutation_id: str | None = None,
) -> int:
    sys.stdout.write(dumps_line(error.public_value()))
    sys.stdout.flush()
    duration = int((time.monotonic() - started) * 1000) if started else 0
    emit(
        client_identity=client_identity,
        operation=operation,
        mutation_id=mutation_id,
        duration_ms=duration,
        result_code=error.code,
    )
    return 1
