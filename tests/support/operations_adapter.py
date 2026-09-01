#!/usr/bin/env python3
"""Deterministic Pimcamp operations-port implementation for contract tests.

This is not a Himalaya response fixture. It implements Pimcamp's private port
directly, so it contains no invented lower-adapter syntax.
"""

import json
from pathlib import Path
import sys


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    state_path = Path(sys.argv[1])
    calls_path = Path(sys.argv[2])
    operation = sys.argv[3]
    request = json.loads(sys.stdin.read())
    state = json.loads(state_path.read_text())
    with calls_path.open("a", encoding="utf-8") as calls:
        calls.write(json.dumps({"operation": operation}, separators=(",", ":")) + "\n")

    if operation == "list":
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
        return output({"ok": {"messages": summaries, "next_cursor": next_cursor}})
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
    return output({"error": {"code": "unsupported"}})


def output(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
