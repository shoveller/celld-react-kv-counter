"""Public CLI tests; no credentials, network, or third-party test runner needed."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "celld_backup.py"


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_suffix = None
        self.bad_head = False

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.calls.append(Key)
        if self.fail_suffix and Key.endswith(self.fail_suffix):
            raise RuntimeError("SECRET credentials must not appear in errors")
        data = Body.read() if hasattr(Body, "read") else Body
        self.objects[Key] = (bytes(data), kwargs.get("Metadata", {}))

    def head_object(self, Bucket, Key):
        data, metadata = self.objects[Key]
        return {"ContentLength": len(data) + int(self.bad_head), "Metadata": metadata}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][0])}


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("celld_backup", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "objects.sqlite3"
        self.work = self.root / "private"
        self.destination = self.root / "restored"
        self.s3 = FakeS3()
        self.db = sqlite3.connect(self.source)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE objects (
              key TEXT PRIMARY KEY, body BLOB NOT NULL, etag INTEGER NOT NULL,
              modified_ms INTEGER NOT NULL, attributes TEXT NOT NULL);
            CREATE TABLE store_sequence (
              singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
              next_etag INTEGER NOT NULL);
            INSERT INTO store_sequence VALUES(1, 2);
            INSERT INTO objects VALUES('kv/log', X'3132', 1, 0, '[]');
        """)

    def run_cli(self, command="backup", extra=(), work=None):
        args = [command, "--work-dir", str(work or self.work),
                "--bucket", "s3://test-bucket", *extra]
        if command == "backup":
            args += ["--source", str(self.source), "--revision", "75afd8e"]
        else:
            args += ["--destination", str(self.destination)]
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = self.module.main(args, client=self.s3)
        self.last_error = errors.getvalue()
        self.last_output = output.getvalue()
        return status

    def backup(self):
        self.assertEqual(self.run_cli(), 0, self.last_error)
        return json.loads(self.last_output)

    def latest(self):
        return json.loads(self.s3.objects["backups/celld-local/latest.json"][0])

    def test_online_wal_snapshot_roundtrip_captures_only_committed_rows(self):
        self.db.execute("INSERT INTO objects VALUES('committed', X'34', 2, 1, '[]')")
        self.db.commit()
        self.db.execute("INSERT INTO objects VALUES('uncommitted', X'35', 3, 2, '[]')")
        result = self.backup()
        manifest = self.latest()
        self.assertEqual(manifest["snapshot_key"], result["key"])
        self.assertEqual(manifest["manifest_key"], result["manifest_key"])
        self.assertIn(result["manifest_key"], self.s3.objects)
        self.assertEqual(self.run_cli("restore"), 0, self.last_error)
        with sqlite3.connect(self.destination / "objects.sqlite3") as restored:
            self.assertEqual(restored.execute("SELECT key FROM objects ORDER BY key").fetchall(),
                             [("committed",), ("kv/log",)])
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.destination / "objects.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_missing_source_is_not_created(self):
        self.source = self.root / "missing.sqlite3"
        self.assertNotEqual(self.run_cli(), 0)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.s3.objects)

    def test_corrupt_source_is_rejected(self):
        self.source = self.root / "corrupt.sqlite3"
        self.source.write_bytes(b"not sqlite")
        self.assertNotEqual(self.run_cli(), 0)
        self.assertFalse(self.s3.objects)

    def test_wrong_schema_is_rejected(self):
        self.db.execute("DROP TABLE store_sequence")
        self.db.commit()
        self.assertNotEqual(self.run_cli(), 0)
        self.assertFalse(self.s3.objects)

    def test_restore_nonempty_refused_without_deleting_anything(self):
        self.backup()
        self.destination.mkdir()
        runtime = self.destination / "runtime"
        runtime.mkdir()
        self.assertNotEqual(self.run_cli("restore"), 0)
        self.assertTrue(runtime.is_dir())
        self.assertFalse((self.destination / "objects.sqlite3").exists())

    def test_digest_mismatch_leaves_no_partial_restore(self):
        self.backup()
        key = self.latest()["snapshot_key"]
        data, metadata = self.s3.objects[key]
        self.s3.objects[key] = (data[:-1] + bytes([data[-1] ^ 1]), metadata)
        self.assertNotEqual(self.run_cli("restore"), 0)
        self.assertFalse(self.destination.exists())

    def test_unique_immutable_keys_and_publication_order(self):
        first = self.backup()
        second = self.backup()
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(len(self.s3.objects), 5)
        self.assertEqual(self.s3.calls[-1], "backups/celld-local/latest.json")
        manifest = self.latest()
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["celld_version"], "v0.6.3")
        self.assertEqual(manifest["revision"], "75afd8e")
        self.assertTrue(manifest["created_at"].endswith("Z"))
        self.assertEqual(json.loads((self.work / "last-success.json").read_text()), manifest)

    def test_upload_and_manifest_failures_preserve_success_markers(self):
        self.backup()
        previous_latest = self.s3.objects["backups/celld-local/latest.json"]
        previous_success = (self.work / "last-success.json").read_bytes()
        for suffix in (".sqlite3", ".manifest.json", "/latest.json"):
            with self.subTest(suffix=suffix):
                self.s3.fail_suffix = suffix
                self.assertNotEqual(self.run_cli(), 0)
                self.assertNotIn("SECRET", self.last_error)
                self.assertEqual(self.last_output, "")
                self.assertEqual(self.s3.objects["backups/celld-local/latest.json"], previous_latest)
                self.assertEqual((self.work / "last-success.json").read_bytes(), previous_success)

    def test_head_mismatch_prevents_publication(self):
        self.s3.bad_head = True
        self.assertNotEqual(self.run_cli(), 0)
        self.assertNotIn("backups/celld-local/latest.json", self.s3.objects)
        self.assertFalse((self.work / "last-success.json").exists())

    def test_state_and_source_locks_refuse_overlapping_backup(self):
        import fcntl
        self.backup()
        for path in (self.work / "operation.lock",
                     self.source.parent / ".objects.sqlite3.backup.lock"):
            with self.subTest(path=path), path.open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                work = self.root / "other-work" if path.parent != self.work else self.work
                self.assertNotEqual(self.run_cli(work=work), 0)
                self.assertIn("locked", self.last_error)

    def test_prefix_and_bucket_traversal_rejected(self):
        for prefix in ("fleet", "backups/../fleet", "backups//x", "/backups/x",
                       "backups/%2e%2e/fleet", "backups/x\\y", "backups"):
            with self.subTest(prefix=prefix):
                self.assertNotEqual(self.run_cli(extra=["--prefix", prefix]), 0)
        self.assertNotEqual(self.run_cli(extra=["--bucket", "s3://test-bucket/../fleet"]), 0)
        self.assertFalse(self.s3.objects)

    def test_bucket_base_prefix_is_preserved(self):
        self.assertEqual(self.run_cli(extra=["--bucket", "s3://test-bucket/project"]), 0)
        self.assertIn("project/backups/celld-local/latest.json", self.s3.objects)

    def test_foreign_manifest_key_and_snapshot_key_rejected(self):
        self.backup()
        self.assertNotEqual(self.run_cli("restore", ["--manifest-key", "fleet/latest.json"]), 0)
        manifest = self.latest()
        for key in ("fleet/object", "backups/celld-local/../fleet", "backups/celld-local-other/x"):
            manifest["snapshot_key"] = key
            self.s3.objects["backups/celld-local/latest.json"] = (json.dumps(manifest).encode(), {})
            self.assertNotEqual(self.run_cli("restore"), 0)
        self.assertFalse(self.destination.exists())

    def test_manifest_types_and_versions_rejected(self):
        self.backup()
        original = self.latest()
        for name, value in (("format_version", 2), ("format_version", True),
                            ("celld_version", "v1.0.0"), ("size", -1),
                            ("size", True), ("sha256", "oops"), ("created_at", "yesterday")):
            with self.subTest(name=name, value=value):
                manifest = dict(original, **{name: value})
                self.s3.objects["backups/celld-local/latest.json"] = (json.dumps(manifest).encode(), {})
                self.assertNotEqual(self.run_cli("restore"), 0)
        self.assertFalse(self.destination.exists())

    def test_insecure_work_directory_and_lock_symlink_rejected(self):
        self.work.mkdir(mode=0o755)
        self.assertNotEqual(self.run_cli(), 0)
        self.work.chmod(0o700)
        target = self.root / "untouched"
        target.write_text("do not change")
        (self.work / "operation.lock").symlink_to(target)
        self.assertNotEqual(self.run_cli(), 0)
        self.assertEqual(target.read_text(), "do not change")

    def test_existing_insecure_lock_permissions_rejected(self):
        self.backup()
        for path in (self.work / "operation.lock",
                     self.source.parent / ".objects.sqlite3.backup.lock"):
            with self.subTest(path=path):
                path.chmod(0o644)
                self.assertNotEqual(self.run_cli(), 0)
                self.assertIn("lock_must_be_private_owned_file", self.last_error)
                path.chmod(0o600)

    def test_bucket_defaults_prefer_backup_bucket(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"CELLD_BACKUP_BUCKET": "s3://backup-bucket",
                                     "CELLD_BUCKET": "s3://fleet-bucket"}):
            self.assertEqual(self.module.parser().parse_args(
                ["backup", "--source", "objects.sqlite3", "--work-dir", "private"]).bucket,
                "s3://backup-bucket")

    def test_s3_client_uses_verified_tls_and_bounded_timeouts(self):
        from types import ModuleType
        from unittest.mock import Mock, patch
        boto = ModuleType("boto3")
        boto.client = Mock(return_value=self.s3)
        config_module = ModuleType("botocore.config")
        config_module.Config = Mock(return_value="configuration")
        with patch.dict(os.environ, {"S3_ENDPOINT": "https://example.com", "AWS_REGION": "auto"}), \
                patch.dict("sys.modules", {"boto3": boto, "botocore.config": config_module}):
            self.assertIs(self.module.make_client(), self.s3)
        config_module.Config.assert_called_once_with(
            connect_timeout=10, read_timeout=60,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"})
        boto.client.assert_called_once_with(
            "s3", endpoint_url="https://example.com", region_name="auto",
            verify=True, config="configuration")

    def test_insecure_endpoint_rejected_before_boto_import(self):
        from unittest.mock import patch
        for endpoint in ("http://example.com", "https://user:pass@example.com",
                         "https://example.com/path", "https://example.com?secret=x",
                         "https://example.com#fragment", "https://"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {"S3_ENDPOINT": endpoint}):
                with self.assertRaises(self.module.BackupError):
                    self.module.make_client()

    def test_readonly_error_is_actionable_but_redacted(self):
        from unittest.mock import patch
        error = sqlite3.OperationalError("SECRET")
        error.sqlite_errorcode = sqlite3.SQLITE_READONLY
        with patch.object(self.module, "_snapshot", side_effect=error):
            self.assertNotEqual(self.run_cli(), 0)
        self.assertIn("WAL/SHM permissions", self.last_error)
        self.assertIn("OperationalError", self.last_error)
        self.assertNotIn("SECRET", self.last_error)

    def test_manifest_can_select_older_immutable_snapshot(self):
        first = self.backup()
        manifest_key = next(key for key in self.s3.objects if key.endswith(".manifest.json"))
        self.backup()
        self.assertEqual(self.run_cli("restore", ["--manifest-key", manifest_key]), 0, self.last_error)
        self.assertEqual(json.loads(self.last_output)["key"], first["key"])

    def test_restore_empty_existing_directory_and_size_mismatch(self):
        self.backup()
        self.destination.mkdir()
        key = self.latest()["snapshot_key"]
        data, metadata = self.s3.objects[key]
        self.s3.objects[key] = (data + b"extra", metadata)
        self.assertNotEqual(self.run_cli("restore"), 0)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.s3.objects[key] = (data, metadata)
        self.assertEqual(self.run_cli("restore"), 0, self.last_error)

    def test_restore_rejects_corrupt_sqlite_even_with_matching_hash(self):
        import hashlib
        self.backup()
        manifest = self.latest()
        bad = b"not a sqlite database"
        manifest.update(size=len(bad), sha256=hashlib.sha256(bad).hexdigest())
        self.s3.objects[manifest["snapshot_key"]] = (bad, {})
        self.s3.objects["backups/celld-local/latest.json"] = (json.dumps(manifest).encode(), {})
        self.assertNotEqual(self.run_cli("restore"), 0)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
