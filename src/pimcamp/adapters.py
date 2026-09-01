"""Pimcamp-owned private adapter ports."""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any

from .config import AdapterConfig, MiradorConfig
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

        def write_request() -> None:
            assert process.stdin is not None
            try:
                process.stdin.write(dumps_line(payload).encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=write_request, daemon=True).start()

        output: queue.Queue[bytes] = queue.Queue(maxsize=1)

        def read_line() -> None:
            output.put(process.stdout.readline())

        reader = threading.Thread(target=read_line, daemon=True)
        reader.start()
        try:
            remaining = _remaining(deadline)
        except PimcampError as exc:
            _kill(process)
            raise _after_start_error(mutation) from exc
        try:
            line = output.get(timeout=remaining)
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


class CommandObservationAdapter:
    adapter_class = "observation_command"

    def __init__(self, config: AdapterConfig):
        self.command = config.command
        self.process: subprocess.Popen[bytes] | None = None
        self.output: queue.Queue[bytes] = queue.Queue(maxsize=64)

    def start(self, deadline: float, stop: threading.Event) -> bool:
        command = [*self.command, "subscribe"]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise unavailable("The observation adapter could not start.") from exc
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        try:
            self.process.stdin.write(dumps_line({}).encode("utf-8"))
            self.process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            _kill(self.process)
            raise unavailable("The observation adapter could not start.") from exc

        def read_lines() -> None:
            assert self.process is not None
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline()
                self.output.put(line)
                if not line:
                    return

        threading.Thread(target=read_lines, daemon=True).start()
        line = self._next_line(deadline, stop)
        if line is None:
            return False
        result = _decode_port_result(line)
        if result != {"status": "started"}:
            raise unavailable("The observation adapter did not confirm execution start.")
        return not stop.is_set()

    def next_event(self, stop: threading.Event) -> bool:
        line = self._next_line(None, stop)
        if line is None:
            return False
        try:
            value = loads_one(line)
        except PimcampError as exc:
            raise unavailable("The observation adapter violated its contract.") from exc
        if isinstance(value, dict) and set(value) == {"event"}:
            if value["event"] == "new_mail":
                return True
            raise unavailable("The observation adapter reported an unknown event.")
        _port_result(value)
        raise unavailable("The observation adapter violated its contract.")

    def close(self, deadline: float) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill(process)
            emit(adapter_class=self.adapter_class, forced_termination=True)

    def _next_line(
        self, deadline: float | None, stop: threading.Event
    ) -> bytes | None:
        while True:
            if stop.is_set():
                return None
            wait = 0.05
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    assert self.process is not None
                    _kill(self.process)
                    emit(adapter_class=self.adapter_class, forced_termination=True)
                    raise unavailable("The observation adapter exceeded its start bound.")
                wait = min(wait, remaining)
            try:
                line = self.output.get(timeout=wait)
            except queue.Empty:
                continue
            if not line:
                raise unavailable("The observation adapter stopped.")
            return line


class MiradorObservationAdapter(CommandObservationAdapter):
    """Real binding for current Mirador, whose binary is named Carillon.

    Carillon reports mail arrivals only through configured hooks. Pimcamp adds
    one private overlay for this child execution. The hook writes a constant
    signal to the child's stdout and does not copy any lower event field.
    """

    adapter_class = "mirador"

    def __init__(self, config: MiradorConfig):
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self.output: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.hook_table = "hook"

    def start(self, deadline: float, stop: threading.Event) -> bool:
        if stop.is_set():
            return False
        _remaining(deadline)
        self.hook_table = self._inspect_config()
        overlay = self._write_overlay()
        config_paths = ":".join([*self.config.config_paths, str(overlay)])
        command = [
            self.config.executable,
            "--config",
            config_paths,
            "--account",
            self.config.account,
            "--backend",
            self.config.backend,
            "watch",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup_overlay()
            raise unavailable("The Mirador observation execution could not start.") from exc
        assert self.process.stdout is not None

        def read_lines() -> None:
            assert self.process is not None
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline()
                self.output.put(line)
                if not line:
                    return

        threading.Thread(target=read_lines, daemon=True).start()
        try:
            _remaining(deadline)
        except PimcampError:
            _kill(self.process)
            self._cleanup_overlay()
            raise
        return not stop.is_set()

    def close(self, deadline: float) -> None:
        try:
            super().close(deadline)
        finally:
            self._cleanup_overlay()

    def _write_overlay(self) -> Path:
        try:
            self.temporary = tempfile.TemporaryDirectory(
                prefix="pimcamp-observation-"
            )
            path = Path(self.temporary.name) / "pimcamp-hook.toml"
            hook_program = (
                "import sys;"
                "sys.stdout.write('{\"event\":\"new_mail\"}\\n');"
                "sys.stdout.flush()"
            )
            values = [sys.executable, "-c", hook_program]
            command = ", ".join(json.dumps(item) for item in values)
            account = json.dumps(self.config.account)
            path.write_text(
                f"[accounts.{account}.{self.config.backend}.{self.hook_table}.on-message-added]\n"
                f"cmd = [{command}]\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            return path
        except OSError as exc:
            self._cleanup_overlay()
            raise unavailable("The Mirador observation overlay is unavailable.") from exc

    def _cleanup_overlay(self) -> None:
        if self.temporary is None:
            return
        try:
            self.temporary.cleanup()
        except OSError:
            pass
        finally:
            self.temporary = None

    def _inspect_config(self) -> str:
        merged: dict[str, Any] = {}
        try:
            for configured in self.config.config_paths:
                path = Path(os.path.expandvars(configured)).expanduser()
                with path.open("rb") as stream:
                    _deep_merge(merged, tomllib.load(stream))
            account = merged["accounts"][self.config.account]
            backend = account[self.config.backend]
        except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise unavailable("The Mirador observation configuration is invalid.") from exc
        if not isinstance(backend, dict):
            raise unavailable("The Mirador observation configuration is invalid.")
        if "hook" in backend and "hooks" in backend:
            raise unavailable("The Mirador observation hook configuration is ambiguous.")
        hook_table = "hooks" if "hooks" in backend else "hook"
        hooks = backend.get(hook_table, {})
        if not isinstance(hooks, dict) or set(hooks) - {"on-message-added"}:
            raise unavailable("The Mirador observation configuration has extra hooks.")
        arrival = hooks.get("on-message-added", {})
        if not isinstance(arrival, dict) or "notify" in arrival:
            raise unavailable("The Mirador observation configuration has a notification hook.")
        return hook_table


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            target[key] = value


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


def _decode_port_result(line: bytes) -> Any:
    try:
        return _port_result(loads_one(line))
    except PimcampError as exc:
        if exc.code in ERROR_CODES and exc.code != "invalid_request":
            raise
        raise unavailable("The configured mail adapter violated its contract.") from exc


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
