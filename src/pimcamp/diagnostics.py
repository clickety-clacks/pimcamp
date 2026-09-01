"""Content-free diagnostics."""

import json
import sys
from typing import Any


_FIELDS = {
    "client_identity",
    "operation",
    "mutation_id",
    "duration_ms",
    "result_code",
    "adapter_class",
    "forced_termination",
}


def emit(**values: Any) -> None:
    record = {
        key: value
        for key, value in values.items()
        if key in _FIELDS and value is not None
    }
    sys.stderr.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stderr.flush()
