"""Monotonic timing helpers for the opt-in live acceptance runner."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
import math
import os
import time


ENTER_CLOCK_FD_ENV = "PIMCAMP_LIVE_ENTER_CLOCK_FD"


def read_enter_timestamp(
    environment: MutableMapping[str, str] | None = None,
) -> float:
    """Read and consume the wrapper's Enter-time monotonic anchor from a pipe."""
    source = os.environ if environment is None else environment
    descriptor_text = source.pop(ENTER_CLOCK_FD_ENV, None)
    if (
        descriptor_text is None
        or not descriptor_text.isascii()
        or not descriptor_text.isdecimal()
    ):
        raise ValueError("Enter clock pipe is unavailable")

    descriptor = int(descriptor_text)
    if descriptor < 0:
        raise ValueError("Enter clock pipe is invalid")
    try:
        with os.fdopen(descriptor, "rb") as clock_pipe:
            payload = clock_pipe.read(65)
    except OSError as error:
        raise ValueError("Enter clock pipe could not be read") from error

    if len(payload) > 64 or not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise ValueError("Enter clock value is invalid")
    try:
        value = float(payload[:-1].decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("Enter clock value is invalid") from error
    if not math.isfinite(value) or value < 0 or value > time.monotonic():
        raise ValueError("Enter clock value is outside the monotonic timeline")
    return value


@dataclass(frozen=True)
class JourneyWindow:
    """A fixed deadline derived from the event that starts one journey."""

    started_at: float
    duration_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.started_at, bool)
            or not isinstance(self.started_at, (int, float))
            or not math.isfinite(self.started_at)
            or self.started_at < 0
            or isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("journey window values are invalid")

    @property
    def deadline(self) -> float:
        return self.started_at + self.duration_seconds

    def elapsed(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return current - self.started_at

    def remaining(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return self.deadline - current
