#!/usr/bin/env python3
"""Deterministic Pimcamp observation-port process for contract tests.

This process implements Pimcamp's private port. It is not a Mirador fixture and
does not imitate or assert any Mirador command or event shape.
"""

import json
from pathlib import Path
import signal
import sys
import time


closing = False


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[3] != "subscribe":
        return 2
    state_path = Path(sys.argv[1])
    calls_path = Path(sys.argv[2])
    if json.loads(sys.stdin.read()) != {}:
        return 2
    state = json.loads(state_path.read_text())
    behavior = state.get("observation_behavior", "event")
    event_file = Path(state["observation_event_file"])

    record(calls_path, "observation_start")
    signal.signal(signal.SIGTERM, request_close)
    if behavior == "start_hang":
        wait_forever()
    output({"ok": {"status": "started"}})

    emitted = False
    while not closing:
        if event_file.exists() and not emitted:
            emitted = True
            record(calls_path, "observation_source_live")
            if behavior == "malformed_event":
                output(
                    {
                        "event": {
                            "kind": "new_mail",
                            "private_subject": state["private_sentinel"],
                        }
                    }
                )
            elif behavior == "failure":
                output({"error": {"code": "backend_unavailable"}})
            else:
                output({"event": "new_mail"})
        time.sleep(0.005)

    record(calls_path, "observation_close")
    if behavior == "close_hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        wait_forever()
    return 0


def request_close(_signum: int, _frame: object) -> None:
    global closing
    closing = True


def record(path: Path, operation: str) -> None:
    with path.open("a", encoding="utf-8") as calls:
        calls.write(json.dumps({"operation": operation}, separators=(",", ":")) + "\n")


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def wait_forever() -> None:
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
