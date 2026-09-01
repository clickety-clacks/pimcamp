#!/usr/bin/env python3
"""Deterministic Pimcamp operations-port implementation for contract tests.

This is not a Himalaya response fixture. It implements Pimcamp's private port
directly, so it contains no invented lower-adapter syntax.
"""

import json
import os
from pathlib import Path
import sys
import time


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    state_path = Path(sys.argv[1])
    calls_path = Path(sys.argv[2])
    operation = sys.argv[3]
    state = json.loads(state_path.read_text())
    behavior = state.get("behavior", {})
    if behavior.get(operation) == "nonreading_hang":
        with calls_path.open("a", encoding="utf-8") as calls:
            calls.write(json.dumps({"operation": operation}, separators=(",", ":")) + "\n")
        hang()
    request = json.loads(sys.stdin.read())
    call = {"operation": operation}
    if operation == "send":
        call["threading_present"] = request.get("threading") is not None
    with calls_path.open("a", encoding="utf-8") as calls:
        calls.write(json.dumps(call, separators=(",", ":")) + "\n")

    if operation == "list":
        if behavior.get("list") == "hang":
            hang()
        if behavior.get("list") == "private_violation":
            return output({"private": state["private_operations_sentinel"]})
        cursor = request.get("cursor")
        try:
            offset = 0 if cursor is None else int(cursor.removeprefix("page:"))
        except ValueError:
            return output({"error": {"code": "invalid_request"}})
        limit = request["limit"]
        messages = state["messages"][offset : offset + limit]
        summaries = [
            {
                "adapter_id": message["adapter_id"],
                "from": message["from"],
                "subject": message["subject"],
                "received_at": message["received_at"],
                "unread": message["unread"],
            }
            for message in messages
        ]
        next_offset = offset + len(messages)
        next_cursor = f"page:{next_offset}" if next_offset < len(state["messages"]) else None
        value = {"ok": {"messages": summaries, "next_cursor": next_cursor}}
        if behavior.get("list") == "cleanup_hang":
            output(value)
            hang()
        return output(value)
    if operation == "read":
        for message in state["messages"]:
            if message["adapter_id"] == request["adapter_id"]:
                return output(
                    {
                        "ok": {
                            "from": message["from"],
                            "to": message["to"],
                            "cc": message["cc"],
                            "reply_to": message["reply_to"],
                            "subject": message["subject"],
                            "sent_at": message["sent_at"],
                            "body_parts": message["body_parts"],
                            "threading": message["threading"],
                        }
                    }
                )
        return output({"error": {"code": "not_found"}})
    if operation == "send":
        selected = behavior.get("send", "success")
        if selected == "success":
            return output({"ok": {"status": "sent"}})
        if selected == "lost_response":
            return 0
        if selected == "hang":
            hang()
        if selected in {"pause_success", "crash_pause"}:
            pause_file = Path(state["pause_file"])
            while not pause_file.exists():
                if selected == "crash_pause" and os.getppid() == 1:
                    return 0
                time.sleep(0.005)
            return output({"ok": {"status": "sent"}})
    if operation == "junk_capabilities":
        capabilities = state.get(
            "junk_capabilities", {"report_spam": False, "move_to_junk": False}
        )
        return output({"ok": capabilities})
    if operation in {"report_spam", "move_to_junk"}:
        if behavior.get(operation) == "lost_response":
            return 0
        adapter_id = request["adapter_id"]
        state["messages"] = [
            message for message in state["messages"] if message["adapter_id"] != adapter_id
        ]
        state_path.write_text(json.dumps(state))
        return output({"ok": {"filed": True}})
    return output({"error": {"code": "unsupported"}})


def output(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


def hang() -> None:
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
