"""Private adapter evidence for move-stable message identity."""

from typing import Any

from .errors import PimcampError, unavailable


class AmbiguousIdentityError(PimcampError):
    """A v2 identity resolved to more than one current message."""

    def __init__(self) -> None:
        super().__init__("conflict", "The message identity has multiple matches.")


def validate_identity_evidence(value: Any) -> dict[str, str | None] | None:
    """Accept only a closed, adapter-returned identity with continuity context."""
    required = {"namespace", "key", "continuity", "backend", "folder", "generation"}
    if not isinstance(value, dict) or set(value) != required:
        return None
    if any(not isinstance(value[key], str) or not value[key] for key in
           ("namespace", "key", "backend", "folder")):
        return None
    if value["continuity"] not in {"native", "mapped"}:
        return None
    if value["generation"] is not None and (
        not isinstance(value["generation"], str) or not value["generation"]
    ):
        return None
    return {
        "namespace": value["namespace"],
        "key": value["key"],
        "continuity": value["continuity"],
        "backend": value["backend"],
        "folder": value["folder"],
        "generation": value["generation"],
    }


def resolve_identity(adapter: Any, evidence: dict[str, Any], deadline: float) -> str:
    """Resolve one authoritative identity or return a safe normalized error."""
    try:
        result = adapter.call(
            "resolve_identity", {"identity_evidence": evidence}, deadline
        )
    except PimcampError as exc:
        if exc.code == "unsupported":
            raise PimcampError(
                "unsupported", "The configured adapter cannot resolve stable message identity."
            ) from exc
        raise
    if not isinstance(result, dict) or set(result) != {"matches"}:
        raise unavailable("The operations adapter returned invalid identity results.")
    matches = result["matches"]
    if not isinstance(matches, list) or any(
        not isinstance(item, dict)
        or set(item) != {"adapter_id"}
        or not isinstance(item["adapter_id"], str)
        or not item["adapter_id"]
        for item in matches
    ):
        raise unavailable("The operations adapter returned invalid identity results.")
    if not matches:
        raise PimcampError("not_found", "The message identity no longer resolves.")
    if len(matches) != 1:
        raise AmbiguousIdentityError()
    return matches[0]["adapter_id"]
