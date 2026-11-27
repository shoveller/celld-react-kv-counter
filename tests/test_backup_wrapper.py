import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class BackupWrapperTests(unittest.TestCase):
    def invoke(self, overrides=None, arguments=()):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "capture-python"
            python.write_text("#!/bin/sh\nprintf '%s\\0' \"$PWD\" \"$@\"\n")
            python.chmod(0o700)
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("CELLD_BACKUP_")
            }
            env["CELLD_BACKUP_PYTHON"] = str(python)
            env.update(overrides or {})
            result = subprocess.run(
                ["bash", str(REPO / "scripts/backup-bind.sh"), *arguments],
                cwd="/", env=env, capture_output=True, check=True,
            )
            return result.stdout.decode().rstrip("\0").split("\0")

    def test_resolves_repo_from_another_directory(self):
        args = self.invoke()
        self.assertEqual(args[:3], [str(REPO), "scripts/celld_backup.py", "backup"])

    def test_defaults_to_local_bind_state(self):
        args = self.invoke()
        self.assertEqual(args[args.index("--source") + 1], "data/dev/objects.sqlite3")
        self.assertEqual(args[args.index("--work-dir") + 1], "data/backups")

    def test_accepts_oci_state_override(self):
        args = self.invoke({"CELLD_BACKUP_SOURCE": "data/counter-bind/dev/objects.sqlite3"})
        self.assertEqual(args[args.index("--source") + 1], "data/counter-bind/dev/objects.sqlite3")

    def test_records_current_checkout_revision(self):
        args = self.invoke()
        expected = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True,
        ).strip()
        self.assertEqual(args[args.index("--revision") + 1], expected)

    def test_preserves_explicit_cli_arguments(self):
        args = self.invoke(arguments=("--prefix", "backups/drill"))
        self.assertEqual(args[-2:], ["--prefix", "backups/drill"])


if __name__ == "__main__":
    unittest.main()
