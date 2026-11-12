#!/usr/bin/env python3
"""Run the real CLI with its timeout clock gated on fixture send readiness.

Only the hung-send boundary test uses this entry point. Startup/receipt setup
is outside its measured wait; a real, recorded adapter call must precede the
short response timeout. No production clock or adapter implementation changes.
"""

import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pimcamp import adapters, receipts, service  # noqa: E402
from pimcamp.cli import main  # noqa: E402


class ReadyClock:
    def __init__(self):
        self.monotonic_origin = time.monotonic()
        self.wall_origin = time.time()
        self.started = None

    def elapsed(self):
        return 0.0 if self.started is None else time.monotonic() - self.started

    def monotonic(self):
        return self.monotonic_origin + self.elapsed()

    def time(self):
        return self.wall_origin + self.elapsed()

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)


def run():
    config = json.loads(Path(os.environ["PIMCAMP_CONFIG"]).read_text())
    calls = Path(config["operations_adapter"]["command"][-1])
    clock = ReadyClock()
    remaining = adapters._remaining

    def after_send_ready(deadline):
        # CommandOperationsAdapter has started its stdin writer and stdout
        # reader before asking for the remaining response wait.
        readiness_deadline = time.monotonic() + 3
        while not calls.read_text().strip():
            if time.monotonic() >= readiness_deadline:
                raise adapters.unavailable("fixture did not record its send")
            time.sleep(0.005)
        clock.started = time.monotonic()
        return remaining(deadline)

    with (
        patch.object(service, "time", clock),
        patch.object(receipts, "time", clock),
        patch.object(adapters, "time", clock),
        patch.object(adapters, "_remaining", after_send_ready),
    ):
        return main()


if __name__ == "__main__":
    raise SystemExit(run())
