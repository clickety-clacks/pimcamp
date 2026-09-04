"""Pure checks for the real Mirador/Carillon binding configuration.

These checks do not imitate a lower event. Credentialed behavior belongs only
to test_live_adapters.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pimcamp.adapters import MiradorObservationAdapter
from pimcamp.cli import _subscribe
from pimcamp.config import MiradorConfig, load
from pimcamp.errors import PimcampError


class MiradorOverlayCase(unittest.TestCase):
    def test_private_overlay_satisfies_carillon_0_1_0_mailbox_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "carillon.toml"
            config_path.write_text(
                '[accounts."account.with.dots".imap]\n'
                'server = "imaps://imap.example.test"\n',
                encoding="utf-8",
            )
            adapter = self._adapter(config_path)
            self.addCleanup(adapter._cleanup_overlay)
            adapter.hook_table = adapter._inspect_config()

            path = adapter._write_overlay()
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            command = parsed["accounts"]["account.with.dots"]["imap"]["hook"][
                "on-message-added"
            ]["cmd"]

            # Carillon 0.1.0's strict ImapConfig parser requires `mailbox`.
            backend = parsed["accounts"]["account.with.dots"]["imap"]
            self.assertEqual("Configured Inbox", backend["mailbox"])
            self.assertEqual([sys.executable, "-c"], command[:2])
            self.assertIn('{"event":"new_mail"}', command[2])
            self.assertNotIn("account.with.dots", command[2])
            self.assertEqual(0, path.stat().st_mode & 0o077)
            self.assertEqual(0, os.stat(path.parent).st_mode & 0o077)

    def test_unrelated_real_hooks_are_refused_for_the_private_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "carillon.toml"
            config_path.write_text(
                '[accounts."account.with.dots".imap]\n'
                'mailbox = "INBOX"\n'
                '[accounts."account.with.dots".imap.hook.on-flag-added]\n'
                'cmd = "true"\n',
                encoding="utf-8",
            )
            with self.assertRaises(PimcampError) as raised:
                self._adapter(config_path)._inspect_config()
            self.assertEqual("backend_unavailable", raised.exception.code)

    def test_replaced_operations_adapter_preserves_mirador_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "pimcamp.json"
            config_path.write_text(
                json.dumps(
                    {
                        "credentials": {},
                        "adapter_wait_seconds": 1,
                        "state_path": str(Path(directory) / "state.sqlite3"),
                        "operations_adapter": {
                            "kind": "command",
                            "command": ["adapter"],
                        },
                        "observation_adapter": {
                            "kind": "mirador",
                            "executable": "carillon",
                            "account": "account.with.dots",
                            "backend": "imap",
                            "config_paths": ["/private/carillon/config.toml"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(config_path, 0o600)
            config = load(config_path)
            adapter = Mock()
            adapter.start.return_value = False

            with (
                patch(
                    "pimcamp.cli.MiradorObservationAdapter",
                    return_value=adapter,
                ) as factory,
                patch("pimcamp.cli.emit"),
            ):
                self.assertEqual(0, _subscribe(config, "client", time.monotonic()))

            factory.assert_called_once_with(config.observation, None)
            adapter.close.assert_called_once()

    def _adapter(self, config_path: Path) -> MiradorObservationAdapter:
        return MiradorObservationAdapter(
            MiradorConfig(
                kind="mirador",
                executable="carillon",
                account="account.with.dots",
                backend="imap",
                config_paths=(str(config_path),),
                raw={},
            ),
            "Configured Inbox",
        )


if __name__ == "__main__":
    unittest.main()
