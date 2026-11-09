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

from tests.test_live_adapters import (
    EXECUTABLE,
    RealAdapterJourney,
    _prompt_for_client_credential,
)


CLIENT_SECRET = "synthetic-live-client-secret-sentinel"


class LiveAdapterRunnerTests(unittest.TestCase):
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
                    credential = _prompt_for_client_credential()
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
                    _prompt_for_client_credential()
            prompt.assert_not_called()
            self.assertNotIn("PIMCAMP_LIVE_CREDENTIAL", os.environ)

    def test_runner_skips_if_hidden_terminal_input_is_unavailable(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), patch(
            "getpass.getpass",
            side_effect=getpass.GetPassWarning("hidden input is unavailable"),
        ) as prompt:
            with self.assertRaises(unittest.SkipTest):
                _prompt_for_client_credential()
        prompt.assert_called_once_with("Pimcamp live-test client credential: ")

    def test_request_keeps_credential_on_stdin_and_out_of_failure_output(self) -> None:
        journey = RealAdapterJourney()
        journey.credential = CLIENT_SECRET
        journey.environment = {
            "PIMCAMP_CONFIG": "/tmp/private-test-config.json",
            "PIMCAMP_LIVE_CREDENTIAL": CLIENT_SECRET,
        }
        response = json.dumps(
            {"contract_version": "pimcamp.v1", "result": {"code": "failed"}}
        )
        completed = subprocess.CompletedProcess(
            args=[str(EXECUTABLE), "list"],
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
        journey = RealAdapterJourney()
        journey.credential = CLIENT_SECRET
        journey.environment = {"PIMCAMP_CONFIG": "/tmp/private-test-config.json"}
        timeout = subprocess.TimeoutExpired(
            cmd=[str(EXECUTABLE), "list"],
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


if __name__ == "__main__":
    unittest.main()
