"""Pimcamp-owned private adapter ports."""

import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any

from .config import AdapterConfig
from .diagnostics import emit
from .errors import ERROR_CODES, PimcampError, unavailable
from .jsonio import dumps_line, loads_one


class CommandOperationsAdapter:
    adapter_class = "operations_command"

    def __init__(self, config: AdapterConfig):
        self.command = config.command

    def call(
        self,
        operation: str,
        payload: dict[str, Any],
        deadline: float,
        *,
        mutation: bool = False,
    ) -> Any:
        command = [*self.command, operation]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise unavailable() from exc
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            process.stdin.write(dumps_line(payload).encode("utf-8"))
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            _kill(process)
            raise _after_start_error(mutation) from exc

        output: queue.Queue[bytes] = queue.Queue(maxsize=1)

        def read_line() -> None:
            output.put(process.stdout.readline())

        reader = threading.Thread(target=read_line, daemon=True)
        reader.start()
        try:
            line = output.get(timeout=_remaining(deadline))
        except queue.Empty as exc:
            _kill(process)
            raise _after_start_error(mutation) from exc
        if not line:
            _kill(process)
            raise _after_start_error(mutation)
        try:
            value = loads_one(line)
            result = _port_result(value)
        except PimcampError:
            _kill(process)
            raise
        except Exception as exc:
            _kill(process)
            raise _after_start_error(mutation) from exc

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill(process)
            emit(adapter_class=self.adapter_class, forced_termination=True)
        remainder = process.stdout.read()
        if remainder.strip():
            raise _after_start_error(mutation)
        return result


def _port_result(value: Any) -> Any:
    if not isinstance(value, dict) or len(value) != 1:
        raise unavailable("The configured mail adapter violated its contract.")
    if "ok" in value:
        return value["ok"]
    if "error" not in value or not isinstance(value["error"], dict):
        raise unavailable("The configured mail adapter violated its contract.")
    error = value["error"]
    if set(error) != {"code"} or error["code"] not in ERROR_CODES:
        raise unavailable("The configured mail adapter violated its contract.")
    raise PimcampError(error["code"], "The configured mail adapter returned a normalized failure.")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise unavailable("The configured mail adapter exceeded its wait bound.")
    return remaining


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _after_start_error(mutation: bool) -> PimcampError:
    if mutation:
        return PimcampError("outcome_unknown", "The mail mutation outcome is unknown.")
    return unavailable("The configured mail adapter exceeded or violated its wait bound.")
