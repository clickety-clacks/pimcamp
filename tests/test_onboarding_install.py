"""Install into isolated prefixes; never touch the operator's installation."""
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "prefix"
        self.install = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/install"))["install"]

    def test_installed_setup_resolves_included_backend_and_assets(self):
        release = self.install(self.prefix, "test-1")
        for name in ("index.html", "styles.css", "app.js", "live-service.js"):
            self.assertTrue((release / "ui/onboarding" / name).is_file())
        result = subprocess.run([str(self.prefix / "bin/pimcamp-setup"), "--help"],
                                capture_output=True, text=True, check=True)
        self.assertIn("--google-client-id", result.stdout)
        self.assertIn("--runtime-config", result.stdout)
        self.assertFalse((self.prefix / "config").exists())

    def test_upgrade_keeps_old_release_and_refuses_reusing_version(self):
        first = self.install(self.prefix, "test-1")
        second = self.install(self.prefix, "test-2")
        self.assertTrue(first.is_dir())
        self.assertEqual((self.prefix / "bin/pimcamp").resolve(), second / "pimcamp")
        with self.assertRaises(FileExistsError):
            self.install(self.prefix, "test-2")

    def test_foreign_executable_and_path_traversal_are_rejected(self):
        (self.prefix / "bin").mkdir(parents=True)
        foreign = self.prefix / "bin/pimcamp"
        foreign.write_text("unrelated user file")
        with self.assertRaises(ValueError):
            self.install(self.prefix, "test-1")
        self.assertEqual(foreign.read_text(), "unrelated user file")
        with self.assertRaises(ValueError):
            self.install(self.prefix, "../escape")


if __name__ == "__main__":
    unittest.main()
