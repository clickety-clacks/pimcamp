#!/usr/bin/env python3
"""Independent replacement for both Pimcamp test ports.

This speaks only Pimcamp's private port protocols. It contains no claimed
Himalaya response or Mirador event fixture.
"""

import json
import os
from pathlib import Path
import signal
import sys
import time


stop_requested = False


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    state_path, calls_path = Path(sys.argv[1]), Path(sys.argv[2])
    operation = sys.argv[3]
    state = json.loads(state_path.read_text())
    if state.get("behavior", {}).get(operation) == "nonreading_hang":
        record(calls_path, {"operation": operation})
        forever()
    request = json.loads(sys.stdin.read())
    if operation == "subscribe":
        return observe(state_path, calls_path, request)
    return operate(state_path, calls_path, operation, request)


def operate(
    state_path: Path,
    calls_path: Path,
    operation: str,
    request: dict[str, object],
) -> int:
    state = json.loads(state_path.read_text())
    evidence: dict[str, object] = {"operation": operation}
    if operation == "send":
        evidence["threading_present"] = request.get("threading") is not None
    record(calls_path, evidence)
    selected = state.get("behavior", {}).get(operation)

    if operation == "list":
        if selected == "hang":
            forever()
        if selected == "private_violation":
            return emit({"private": state["private_operations_sentinel"]})
        offset = cursor_offset(request.get("cursor"))
        limit = request["limit"]
        page = state["messages"][offset : offset + limit]
        summaries = [
            {
                key: message[key]
                for key in ("adapter_id", "from", "subject", "received_at", "unread")
            }
            for message in page
        ]
        following = offset + len(page)
        cursor = f"page:{following}" if following < len(state["messages"]) else None
        response = {"ok": {"messages": summaries, "next_cursor": cursor}}
        if selected == "cleanup_hang":
            emit(response)
            forever()
        return emit(response)

    if operation == "read":
        found = next(
            (
                message
                for message in state["messages"]
                if message["adapter_id"] == request["adapter_id"]
            ),
            None,
        )
        if found is None:
            return emit({"error": {"code": "not_found"}})
        return emit(
            {
                "ok": {
                    key: found[key]
                    for key in (
                        "from",
                        "to",
                        "cc",
                        "reply_to",
                        "subject",
                        "sent_at",
                        "body_parts",
                        "threading",
                    )
                }
            }
        )

    if operation == "send":
        if selected == "lost_response":
            return 0
        if selected == "hang":
            forever()
        if selected in {"pause_success", "crash_pause"}:
            wait_for_release(Path(state["pause_file"]), selected == "crash_pause")
        return emit({"ok": {"status": "sent"}})

    if operation == "junk_capabilities":
        return emit(
            {
                "ok": state.get(
                    "junk_capabilities",
                    {"report_spam": False, "move_to_junk": False},
                )
            }
        )

    if operation in {"report_spam", "move_to_junk"}:
        if selected == "lost_response":
            return 0
        target = request["adapter_id"]
        state["messages"] = [
            message for message in state["messages"] if message["adapter_id"] != target
        ]
        state_path.write_text(json.dumps(state))
        return emit({"ok": {"filed": True}})

    return emit({"error": {"code": "unsupported"}})


def observe(
    state_path: Path, calls_path: Path, request: dict[str, object]
) -> int:
    if request != {}:
        return 2
    state = json.loads(state_path.read_text())
    behavior = state.get("observation_behavior", "event")
    event_file = Path(state["observation_event_file"])
    record(calls_path, {"operation": "observation_open"})
    signal.signal(signal.SIGTERM, stop)
    if behavior == "open_hang":
        forever()
    emit({"ok": {"status": "subscribed"}})
    sent = False
    while not stop_requested:
        if event_file.exists() and not sent:
            sent = True
            if behavior == "malformed_event":
                emit(
                    {
                        "event": {
                            "kind": "new_mail",
                            "private_subject": state["private_sentinel"],
                        }
                    }
                )
            elif behavior == "failure":
                emit({"error": {"code": "backend_unavailable"}})
            else:
                emit({"event": "new_mail"})
        time.sleep(0.005)
    record(calls_path, {"operation": "observation_close"})
    if behavior == "close_hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        forever()
    return 0


def cursor_offset(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(str(value).removeprefix("page:"))
    except ValueError:
        emit({"error": {"code": "invalid_request"}})
        raise SystemExit(0)


def wait_for_release(path: Path, detect_orphan: bool) -> None:
    while not path.exists():
        if detect_orphan and os.getppid() == 1:
            raise SystemExit(0)
        time.sleep(0.005)


def stop(_signum: int, _frame: object) -> None:
    global stop_requested
    stop_requested = True


def record(path: Path, value: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def emit(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


def forever() -> None:
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
