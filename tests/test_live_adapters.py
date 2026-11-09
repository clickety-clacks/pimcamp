"""Credentialed real-adapter acceptance journey.

This module must never replace absent credentials or lower tools with invented
Himalaya responses or Mirador events.
"""

import getpass
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import subprocess
import sys
import time
import unittest
import uuid
import warnings


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "pimcamp"
EXTERNAL_BOUND_SECONDS = 120


def _prompt_for_client_credential() -> str:
    # Drop the former secret environment variable before any child environment
    # is copied, including when an older runner still supplies it.
    os.environ.pop("PIMCAMP_LIVE_CREDENTIAL", None)
    if not sys.stdin.isatty():
        raise unittest.SkipTest(
            "real journey needs an interactive terminal for hidden credential input"
        )

    try:
        with warnings.catch_warnings():
            # getpass otherwise falls back to echoing input when hidden input is
            # unavailable. Treat that fallback as a skip before reading input.
            warnings.simplefilter("error", getpass.GetPassWarning)
            credential = getpass.getpass("Pimcamp live-test client credential: ")
    except getpass.GetPassWarning:
        raise unittest.SkipTest(
            "terminal cannot accept the client credential without echo"
        ) from None

    if not credential:
        raise unittest.SkipTest("no client credential was entered")
    return credential


class RealAdapterJourney(unittest.TestCase):
    def test_real_single_account_journey(self) -> None:
        # Do not let a legacy secret variable reach version probes or child tools.
        os.environ.pop("PIMCAMP_LIVE_CREDENTIAL", None)
        required = {
            "PIMCAMP_LIVE_CONFIG": os.environ.get("PIMCAMP_LIVE_CONFIG"),
            "PIMCAMP_LIVE_RECIPIENT": os.environ.get("PIMCAMP_LIVE_RECIPIENT"),
            "PIMCAMP_LIVE_HIMALAYA_VERSION": os.environ.get(
                "PIMCAMP_LIVE_HIMALAYA_VERSION"
            ),
            "PIMCAMP_LIVE_MIRADOR_VERSION": os.environ.get(
                "PIMCAMP_LIVE_MIRADOR_VERSION"
            ),
        }
        missing = [name for name, value in required.items() if not value]
        if os.environ.get("PIMCAMP_LIVE_ENABLE") != "1" or missing:
            detail = ", ".join(missing) if missing else "explicit live enable"
            self.skipTest(
                "real Himalaya/Mirador journey not enabled; missing "
                f"{detail}; no lower-adapter fixture is substituted"
            )

        config_path = Path(str(required["PIMCAMP_LIVE_CONFIG"]))
        if not config_path.is_file():
            self.skipTest(
                "real Pimcamp configuration is absent; no lower-adapter fixture "
                "is substituted"
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        operations = config.get("operations_adapter", {})
        observation = config.get("observation_adapter", {})
        self.assertEqual("himalaya", operations.get("kind"))
        self.assertEqual("mirador", observation.get("kind"))
        self._prove_tool_version(
            str(operations.get("executable", "")),
            str(required["PIMCAMP_LIVE_HIMALAYA_VERSION"]),
            "Himalaya",
        )
        self._prove_tool_version(
            str(observation.get("executable", "")),
            str(required["PIMCAMP_LIVE_MIRADOR_VERSION"]),
            "Mirador/Carillon",
        )
        wait_bound = config.get("adapter_wait_seconds")
        self.assertIsInstance(wait_bound, (int, float))
        self.assertGreater(wait_bound, 0)
        self.teardown_timeout = float(wait_bound) + 5

        self.credential = _prompt_for_client_credential()
        self.recipient = str(required["PIMCAMP_LIVE_RECIPIENT"])
        self.environment = os.environ.copy()
        self.environment["PIMCAMP_CONFIG"] = str(config_path)
        deadline = time.monotonic() + EXTERNAL_BOUND_SECONDS
        subscription = self._start_subscription()

        try:
            started = self._read_line(subscription, deadline)
            self.assertEqual(
                "pimcamp.v1",
                started.get("contract_version"),
                "real subscription returned an invalid contract (content suppressed)",
            )
            self.assertEqual(
                {"status": "started"},
                started.get("result"),
                "real subscription did not start (content suppressed)",
            )

            token = f"pimcamp-live-{uuid.uuid4()}"
            composed = self._success(
                self._invoke(
                    "compose",
                    {
                        "to": [{"name": None, "address": self.recipient}],
                        "cc": [],
                        "bcc": [],
                        "subject": token,
                        "body_text": token,
                    },
                    deadline,
                )
            )
            sent = self._success(
                self._invoke(
                    "send",
                    {"composition": composed, "mutation_id": str(uuid.uuid4())},
                    deadline,
                )
            )
            self.assertEqual("sent", sent.get("status"), "unique-token send failed")

            event = self._read_line(subscription, deadline)
            self.assertEqual(
                {"contract_version", "kind", "mailbox", "observed_at"},
                set(event),
                "real event had an invalid shape (content suppressed)",
            )
            self.assertEqual("new_mail", event["kind"], "unexpected event kind")
            self.assertEqual("inbox", event["mailbox"], "unexpected event mailbox")

            incoming = self._find_message(token, deadline)
            message = self._success(
                self._invoke(
                    "read", {"message_ref": incoming["message_ref"]}, deadline
                )
            )
            self.assertTrue(
                any(
                    token in part["content_utf8"]
                    for part in message["body_parts"]
                )
            )

            reply = self._success(
                self._invoke(
                    "reply",
                    {
                        "message_ref": incoming["message_ref"],
                        "body_text": f"reply-{token}",
                    },
                    deadline,
                )
            )
            reply_sent = self._success(
                self._invoke(
                    "send",
                    {"composition": reply, "mutation_id": str(uuid.uuid4())},
                    deadline,
                )
            )
            self.assertEqual("sent", reply_sent.get("status"), "reply send failed")

            filed = self._success(
                self._invoke(
                    "junk",
                    {
                        "message_ref": incoming["message_ref"],
                        "mutation_id": str(uuid.uuid4()),
                    },
                    deadline,
                )
            )
            self.assertEqual("filed", filed.get("status"), "matching message was not filed")
            self.assertIn(
                filed.get("mechanism"),
                {"report_spam", "move_to_junk"},
                "matching message used an unsupported junk mechanism",
            )

            remaining = self._all_summaries(deadline)
            self.assertFalse(any(token in item["subject"] for item in remaining))
        finally:
            if subscription.poll() is None:
                subscription.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = subscription.communicate(
                    timeout=self.teardown_timeout
                )
            except subprocess.TimeoutExpired:
                subscription.kill()
                try:
                    subscription.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise AssertionError(
                    "real subscription did not honor bounded Pimcamp teardown "
                    "(output suppressed)"
                ) from None
            for line in stdout.splitlines():
                event = json.loads(line)
                self.assertEqual(
                    {"contract_version", "kind", "mailbox", "observed_at"},
                    set(event),
                    "real event had an invalid shape (content suppressed)",
                )
            self.assertEqual(
                0,
                subscription.returncode,
                "real subscription failed (output suppressed)",
            )

    def _prove_tool_version(
        self, executable: str, expected: str, label: str
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            self.skipTest(
                f"real {label} executable is absent; no lower-adapter fixture "
                "is substituted"
            )
        completed = subprocess.run(
            [resolved, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, f"{label} --version failed")
        self.assertIn(expected, completed.stdout + completed.stderr)

    def _start_subscription(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [str(EXECUTABLE), "subscribe_new_mail"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._child_environment(),
        )
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "contract_version": "pimcamp.v1",
                    "client_credential": self.credential,
                    "input": {},
                }
            )
        )
        process.stdin.close()
        process.stdin = None
        return process

    def _invoke(
        self, operation: str, input_value: dict[str, object], deadline: float
    ) -> dict[str, object]:
        remaining = deadline - time.monotonic()
        self.assertGreater(remaining, 0, "real journey exceeded its external bound")
        try:
            completed = subprocess.run(
                [str(EXECUTABLE), operation],
                input=json.dumps(
                    {
                        "contract_version": "pimcamp.v1",
                        "client_credential": self.credential,
                        "input": input_value,
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._child_environment(),
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "Pimcamp request timed out (output suppressed)"
            ) from None
        self.assertEqual(
            1,
            len(completed.stdout.splitlines()),
            "Pimcamp emitted an unexpected response count (output suppressed)",
        )
        value = json.loads(completed.stdout)
        self.assertEqual(
            0, completed.returncode, "Pimcamp request failed (output suppressed)"
        )
        return value

    def _read_line(
        self, process: subprocess.Popen[str], deadline: float
    ) -> dict[str, object]:
        assert process.stdout is not None
        remaining = deadline - time.monotonic()
        self.assertGreater(remaining, 0, "real journey exceeded its external bound")
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(
                ready.select(remaining),
                "real observation emitted no event before the external bound",
            )
        line = process.stdout.readline()
        self.assertTrue(line, "real observation process stopped before emitting a line")
        value = json.loads(line)
        self.assertIsInstance(value, dict)
        return value

    def _find_message(
        self, token: str, deadline: float
    ) -> dict[str, object]:
        while time.monotonic() < deadline:
            summaries = self._all_summaries(deadline)
            found = next(
                (item for item in summaries if token in item["subject"]), None
            )
            if found is not None:
                return found
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        self.fail("the event arrived but authoritative list never returned the live token")

    def _all_summaries(self, deadline: float) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        cursor: object = None
        while True:
            request: dict[str, object] = {"limit": 100}
            if cursor is not None:
                request["cursor"] = cursor
            result = self._success(self._invoke("list", request, deadline))
            summaries.extend(result["messages"])
            cursor = result["next_cursor"]
            if cursor is None:
                return summaries

    def _success(self, envelope: dict[str, object]) -> dict[str, object]:
        self.assertEqual(
            "pimcamp.v1",
            envelope.get("contract_version"),
            "Pimcamp returned an invalid contract (content suppressed)",
        )
        result = envelope.get("result")
        self.assertIsInstance(
            result, dict, "Pimcamp returned an invalid result (content suppressed)"
        )
        return result

    def _child_environment(self) -> dict[str, str]:
        environment = self.environment.copy()
        environment.pop("PIMCAMP_LIVE_CREDENTIAL", None)
        return environment


if __name__ == "__main__":
    unittest.main()
