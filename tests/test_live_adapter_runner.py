"""Non-live checks for safe credential input in the real-adapter runner."""

import contextlib
import getpass
import io
import json
import os
import subprocess
import time
import unittest
from unittest.mock import patch

from tests import test_live_adapters as live_adapters


CLIENT_SECRET = "synthetic-live-client-secret-sentinel"


class LiveAdapterRunnerTests(unittest.TestCase):
    def test_ordinary_reply_uses_token_substrings_in_re_subject_and_body(self) -> None:
        token = "pimcamp-live-synthetic-token"

        self.assertTrue(
            live_adapters._human_return_subject_matches(f"Re: {token} (reply)", token)
        )
        self.assertFalse(live_adapters._human_return_subject_matches(token, token))
        self.assertFalse(
            live_adapters._human_return_subject_matches(f"Fwd: {token}", token)
        )
        self.assertTrue(
            live_adapters._body_contains_token(
                [
                    {
                        "content_utf8": (
                            "On a previous message, someone wrote:\n> " + token
                        )
                    }
                ],
                token,
            )
        )
        self.assertFalse(live_adapters._body_contains_token([], token))

    def test_prompt_scrubs_obsolete_environment_and_does_not_log_input(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with patch.dict(os.environ, {"PIMCAMP_LIVE_CREDENTIAL": CLIENT_SECRET}):
            with patch("sys.stdin.isatty", return_value=True), patch(
                "getpass.getpass", return_value=CLIENT_SECRET
            ) as prompt:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(
                    errors
                ):
                    credential = live_adapters._prompt_for_client_credential()
            self.assertTrue(
                credential == CLIENT_SECRET,
                "hidden prompt did not return its supplied test value",
            )
            self.assertNotIn("PIMCAMP_LIVE_CREDENTIAL", os.environ)

        prompt.assert_called_once_with("Pimcamp live-test client credential: ")
        self.assertFalse(CLIENT_SECRET in output.getvalue(), "input appeared on stdout")
        self.assertFalse(CLIENT_SECRET in errors.getvalue(), "input appeared on stderr")

    def test_noninteractive_runner_skips_before_reading_a_credential(self) -> None:
        with patch.dict(os.environ, {"PIMCAMP_LIVE_CREDENTIAL": CLIENT_SECRET}):
            with patch("sys.stdin.isatty", return_value=False), patch(
                "getpass.getpass"
            ) as prompt:
                with self.assertRaises(unittest.SkipTest):
                    live_adapters._prompt_for_client_credential()
            prompt.assert_not_called()
            self.assertNotIn("PIMCAMP_LIVE_CREDENTIAL", os.environ)

    def test_runner_skips_if_hidden_terminal_input_is_unavailable(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), patch(
            "getpass.getpass",
            side_effect=getpass.GetPassWarning("hidden input is unavailable"),
        ) as prompt:
            with self.assertRaises(unittest.SkipTest):
                live_adapters._prompt_for_client_credential()
        prompt.assert_called_once_with("Pimcamp live-test client credential: ")

    def test_request_keeps_credential_on_stdin_and_out_of_failure_output(self) -> None:
        journey = live_adapters.RealAdapterJourney()
        journey.credential = CLIENT_SECRET
        journey.environment = {
            "PIMCAMP_CONFIG": "/tmp/private-test-config.json",
            "PIMCAMP_LIVE_CREDENTIAL": CLIENT_SECRET,
        }
        response = json.dumps(
            {"contract_version": "pimcamp.v1", "result": {"code": "failed"}}
        )
        completed = subprocess.CompletedProcess(
            args=[str(live_adapters.EXECUTABLE), "list"],
            returncode=1,
            stdout=response,
            stderr=CLIENT_SECRET,
        )
        with patch("subprocess.run", return_value=completed) as run:
            with self.assertRaises(AssertionError) as failure:
                journey._invoke("list", {"limit": 1}, time.monotonic() + 10)

        args, kwargs = run.call_args
        self.assertFalse(CLIENT_SECRET in repr(args), "credential appeared in argv")
        self.assertTrue(
            CLIENT_SECRET in kwargs["input"], "credential was not sent through stdin"
        )
        self.assertFalse(
            CLIENT_SECRET in json.dumps(kwargs["env"]),
            "credential appeared in environment",
        )
        self.assertFalse(
            CLIENT_SECRET in str(failure.exception), "credential appeared in failure output"
        )

    def test_timeout_output_is_suppressed(self) -> None:
        journey = live_adapters.RealAdapterJourney()
        journey.credential = CLIENT_SECRET
        journey.environment = {"PIMCAMP_CONFIG": "/tmp/private-test-config.json"}
        timeout = subprocess.TimeoutExpired(
            cmd=[str(live_adapters.EXECUTABLE), "list"],
            timeout=1,
            output=CLIENT_SECRET,
            stderr=CLIENT_SECRET,
        )
        with patch("subprocess.run", side_effect=timeout):
            with self.assertRaises(AssertionError) as failure:
                journey._invoke("list", {"limit": 1}, time.monotonic() + 10)
        self.assertFalse(
            CLIENT_SECRET in str(failure.exception), "credential appeared in timeout output"
        )

    def test_operation_timeout_is_capped_at_remaining_journey_time(self) -> None:
        journey = live_adapters.RealAdapterJourney()
        journey.credential = CLIENT_SECRET
        journey.environment = {"PIMCAMP_CONFIG": "/tmp/private-test-config.json"}
        completed = subprocess.CompletedProcess(
            args=[str(live_adapters.EXECUTABLE), "list"],
            returncode=0,
            stdout=json.dumps(
                {"contract_version": "pimcamp.v1", "result": {"messages": []}}
            ),
            stderr="",
        )

        with patch(
            "tests.test_live_adapters.time.monotonic", return_value=99.25
        ), patch("subprocess.run", return_value=completed) as run:
            journey._invoke("list", {"limit": 1}, deadline=100.0)

        self.assertEqual(0.75, run.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
