"""Focused checks for failures inside the durable receipt seam."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pimcamp.errors import PimcampError, unavailable
from pimcamp.service import _finish_proved_success


class MutationSafetyCase(unittest.TestCase):
    def test_receipt_failure_after_proved_success_is_never_retryable(self) -> None:
        class BrokenReceipt:
            def finish_success(self, _public: dict[str, object]) -> dict[str, object]:
                raise unavailable("receipt write failed")

            def finish_error(self, _error: object) -> dict[str, object]:
                raise unavailable("receipt write failed")

        with self.assertRaises(PimcampError) as raised:
            _finish_proved_success(
                BrokenReceipt(),
                {"status": "sent", "mutation_id": "mutation"},
                "The mail mutation outcome is unknown.",
            )
        self.assertEqual("outcome_unknown", raised.exception.code)
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
