"""Closed public error contract."""

from dataclasses import dataclass

from . import CONTRACT_VERSION


ERROR_CODES = frozenset(
    {
        "invalid_request",
        "permission_denied",
        "not_found",
        "unsupported",
        "backend_unavailable",
        "conflict",
        "outcome_unknown",
    }
)


@dataclass(frozen=True)
class PimcampError(Exception):
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown Pimcamp error code: {self.code}")

    @property
    def retryable(self) -> bool:
        return self.code == "backend_unavailable"

    def public_value(self) -> dict[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "code": self.code,
            "retryable": self.retryable,
            "message": self.message,
        }


def invalid(message: str = "The request does not match the selected operation.") -> PimcampError:
    return PimcampError("invalid_request", message)


def unavailable(message: str = "The configured mail adapter is unavailable.") -> PimcampError:
    return PimcampError("backend_unavailable", message)
