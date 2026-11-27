#!/usr/bin/env python3
"""Consistent celld v0.6.3 local-store snapshots; not a scheduler or fleet backup."""

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


class BackupError(Exception):
    """Messages are fixed codes, never upstream exceptions or configuration values."""


def _require(condition, code):
    if not condition:
        raise BackupError(code)


def _safe_path(value):
    _require(isinstance(value, str) and bool(value), "invalid_object_path")
    _require(all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part)
                 for part in value.split("/")), "invalid_object_path")
    return value


def _location(bucket_uri, prefix):
    _safe_path(prefix)
    _require(prefix.startswith("backups/"), "prefix_must_be_under_backups")
    _require(isinstance(bucket_uri, str), "bucket_required")
    uri = urlsplit(bucket_uri)
    _require(uri.scheme == "s3" and bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
                                                    uri.netloc)),
             "invalid_bucket")
    _require(not uri.query and not uri.fragment, "invalid_bucket")
    base = uri.path[1:]
    if base:
        _safe_path(base)
    return uri.netloc, "/".join(part for part in (base, prefix) if part)


def make_client():
    """Use the existing boto credential chain only after endpoint validation."""
    endpoint = os.environ.get("S3_ENDPOINT", "")
    uri = urlsplit(endpoint)
    _require(uri.scheme == "https" and bool(uri.hostname)
             and not uri.username and not uri.password
             and uri.path in ("", "/") and not uri.query and not uri.fragment
             and not any(c.isspace() for c in endpoint), "invalid_https_endpoint")
    try:
        _require(uri.port in (None, 443), "invalid_https_endpoint")
        import boto3
        from botocore.config import Config
        return boto3.client(
            "s3", endpoint_url=endpoint,
            region_name=os.environ.get("AWS_REGION", "auto"), verify=True,
            config=Config(connect_timeout=10, read_timeout=60,
                          retries={"mode": "standard", "total_max_attempts": 3},
                          s3={"addressing_style": "path"}))
    except BackupError:
        raise
    except Exception:
        raise BackupError("s3_client_unavailable") from None


def _private_directory(path):
    path = Path(path).absolute()
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and info.st_mode & 0o077 == 0, "work_dir_must_be_private_owned_directory")
    return path.resolve()


@contextmanager
def _lock(path):
    parent = path.parent.stat()
    _require(parent.st_mode & 0o022 == 0, "lock_parent_must_not_be_writable_by_others")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise BackupError("unsafe_lock_path") from None
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                 and info.st_mode & 0o077 == 0 and info.st_nlink == 1,
                 "lock_must_be_private_owned_file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("operation_locked") from None
        yield
    finally:
        os.close(fd)


def _readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _validate_sqlite(path):
    with closing(_readonly(path)) as db:
        _require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                 "sqlite_integrity_failed")
        # Schema from denoland/celld v0.6.3 crates/celld/local_store.rs.
        expected = {
            "objects": [("key", "TEXT", 0, 1), ("body", "BLOB", 1, 0),
                        ("etag", "INTEGER", 1, 0), ("modified_ms", "INTEGER", 1, 0),
                        ("attributes", "TEXT", 1, 0)],
            "store_sequence": [("singleton", "INTEGER", 0, 1),
                               ("next_etag", "INTEGER", 1, 0)],
        }
        for table, columns in expected.items():
            kind = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
            actual = [(row[1], row[2].upper(), row[3], row[5])
                      for row in db.execute("PRAGMA table_info(" + table + ")")]
            _require(kind == ("table",) and actual == columns, "invalid_localstore_schema")
        _require(db.execute("SELECT singleton, next_etag FROM store_sequence").fetchall()
                 and db.execute("SELECT count(*) FROM store_sequence WHERE singleton != 1 "
                                "OR typeof(next_etag) != 'integer' OR next_etag < 1").fetchone()[0] == 0,
                 "invalid_localstore_sequence")


def _snapshot(source, target):
    _require(source.is_file(), "source_missing_or_not_file")
    with closing(_readonly(source)) as reader, closing(sqlite3.connect(target)) as writer:
        deadline = time.monotonic() + 120

        def progress(status, remaining, total):
            _require(time.monotonic() < deadline, "snapshot_timeout")

        reader.backup(writer, pages=256, progress=progress, sleep=0.05)
        writer.execute("PRAGMA journal_mode=DELETE")
    _validate_sqlite(target)


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path, value):
    fd, name = tempfile.mkstemp(prefix=".last-success-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _backup(args, client, bucket, prefix, work):
    source = Path(args.source).resolve()
    _require(source.is_file(), "source_missing_or_not_file")
    with _lock(source.parent / ("." + source.name + ".backup.lock")):
        created = datetime.now(timezone.utc)
        identity = created.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex
        snapshot_key = prefix + "/" + identity + ".sqlite3"
        manifest_key = prefix + "/" + identity + ".manifest.json"
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=work) as stage:
            snapshot = Path(stage) / "objects.sqlite3"
            # SQLite must never create a world-readable temporary snapshot.
            snapshot.touch(mode=0o600)
            _snapshot(source, snapshot)
            size, digest = _digest(snapshot)
            _require(size <= 5 * 1024**3, "snapshot_exceeds_single_upload_limit")
            manifest = {
                "format_version": 1, "celld_version": "v0.6.3", "revision": args.revision,
                "created_at": created.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "snapshot_key": snapshot_key, "sha256": digest, "size": size,
            }
            with snapshot.open("rb") as body:
                client.put_object(Bucket=bucket, Key=snapshot_key, Body=body,
                                  ContentLength=size, ContentType="application/vnd.sqlite3",
                                  Metadata={"sha256": digest})
            head = client.head_object(Bucket=bucket, Key=snapshot_key)
            _require(head.get("ContentLength") == size
                     and head.get("Metadata", {}).get("sha256") == digest,
                     "remote_snapshot_verification_failed")
            encoded = _json_bytes(manifest)
            client.put_object(Bucket=bucket, Key=manifest_key, Body=encoded,
                              ContentType="application/json")
            client.put_object(Bucket=bucket, Key=prefix + "/latest.json", Body=encoded,
                              ContentType="application/json")
            _write_json(work / "last-success.json", manifest)
    return {"status": "backed_up", "key": snapshot_key, "sha256": digest,
            "created_at": manifest["created_at"]}


def _contained_key(key, prefix):
    _safe_path(key)
    _require(key.startswith(prefix + "/"), "object_outside_backup_prefix")
    return key


def _manifest(client, bucket, key, prefix):
    response = client.get_object(Bucket=bucket, Key=_contained_key(key, prefix))
    with closing(response["Body"]) as stream:
        data = stream.read(65537)
    _require(len(data) <= 65536, "manifest_too_large")
    value = json.loads(data)
    _require(isinstance(value, dict), "invalid_manifest")
    _require(type(value.get("format_version")) is int and value["format_version"] == 1
             and value.get("celld_version") == "v0.6.3", "unsupported_manifest_version")
    _require(type(value.get("size")) is int and 0 < value["size"] <= 5 * 1024**3,
             "invalid_manifest_size")
    _require(isinstance(value.get("sha256"), str)
             and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]), "invalid_manifest_digest")
    _require(isinstance(value.get("revision"), str)
             and re.fullmatch(r"(?:unknown|[0-9a-fA-F]{7,40})", value["revision"]),
             "invalid_manifest_revision")
    created = value.get("created_at")
    _require(isinstance(created, str) and created.endswith("Z"), "invalid_manifest_time")
    datetime.fromisoformat(created[:-1] + "+00:00")
    _contained_key(value.get("snapshot_key"), prefix)
    _require(value["snapshot_key"].endswith(".sqlite3"), "invalid_snapshot_key")
    return value


def _empty_destination(destination):
    _require(not destination.is_symlink(), "destination_must_be_empty_directory")
    if destination.exists():
        _require(destination.is_dir() and not any(destination.iterdir()),
                 "destination_must_be_empty_directory")
    else:
        _require(destination.parent.is_dir(), "destination_parent_missing")


def _publish(snapshot, destination):
    _empty_destination(destination)
    made_directory = not destination.exists()
    if made_directory:
        destination.mkdir(mode=0o700)
    temporary = None
    published = False
    try:
        fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=destination)
        with os.fdopen(fd, "wb") as target, snapshot.open("rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        # Reject a concurrently populated destination; link refuses replacement.
        _require(list(destination.iterdir()) == [Path(temporary)],
                 "destination_must_be_empty_directory")
        os.link(temporary, destination / "objects.sqlite3")
        published = True
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        if made_directory and not published:
            destination.rmdir()
    _sync_directory(destination)


def _restore(args, client, bucket, prefix, work):
    destination = Path(args.destination).absolute()
    _empty_destination(destination)
    # A separate stable sibling lock protects restores using different work dirs.
    with _lock(destination.parent / ("." + destination.name + ".restore.lock")):
        manifest = _manifest(client, bucket, args.manifest_key or prefix + "/latest.json", prefix)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=work) as stage:
            snapshot = Path(stage) / "objects.sqlite3"
            response = client.get_object(Bucket=bucket, Key=manifest["snapshot_key"])
            size, digest = 0, hashlib.sha256()
            with closing(response["Body"]) as body, snapshot.open("xb") as output:
                os.chmod(snapshot, 0o600)
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    _require(size <= manifest["size"], "snapshot_size_mismatch")
                    digest.update(chunk)
                    output.write(chunk)
            _require(size == manifest["size"], "snapshot_size_mismatch")
            _require(digest.hexdigest() == manifest["sha256"], "snapshot_digest_mismatch")
            _validate_sqlite(snapshot)
            _publish(snapshot, destination)
    return {"status": "restored", "key": manifest["snapshot_key"],
            "sha256": manifest["sha256"], "created_at": manifest["created_at"]}


def parser():
    result = argparse.ArgumentParser(
        description="Snapshot/restore one celld v0.6.3 local objects.sqlite3; no scheduling or deletion.")
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("backup", "restore"):
        sub = commands.add_parser(command)
        sub.add_argument("--work-dir", required=True,
                         help="Owned private directory (0700); parent must exist")
        sub.add_argument("--bucket", default=os.environ.get("CELLD_BACKUP_BUCKET")
                         or os.environ.get("CELLD_BUCKET"),
                         help="s3://bucket[/base-prefix]; default CELLD_BACKUP_BUCKET or CELLD_BUCKET")
        sub.add_argument("--prefix", default="backups/celld-local",
                         help="Dedicated safe path under backups/ (default: backups/celld-local)")
        if command == "backup":
            sub.add_argument("--source", required=True, help="Existing celld local objects.sqlite3")
            sub.add_argument("--revision", default="unknown", help="7–40 character git SHA or unknown")
        else:
            sub.add_argument("--destination", required=True,
                             help="Empty celld dev directory; no existing runtime/db/data allowed")
            sub.add_argument("--manifest-key",
                             help="Full key under the dedicated prefix (default: <prefix>/latest.json)")
    return result


def main(argv=None, *, client=None):
    args = parser().parse_args(argv)
    try:
        bucket, prefix = _location(args.bucket, args.prefix)
        if args.command == "backup":
            _require(bool(re.fullmatch(r"(?:unknown|[0-9a-fA-F]{7,40})", args.revision)),
                     "invalid_revision")
        work = _private_directory(args.work_dir)
        with ExitStack() as stack:
            stack.enter_context(_lock(work / "operation.lock"))
            if client is None:
                client = make_client()
            if args.command == "backup":
                result = _backup(args, client, bucket, prefix, work)
            else:
                result = _restore(args, client, bucket, prefix, work)
        print(json.dumps(result, sort_keys=True))
        return 0
    except BackupError as error:
        print("celld_backup: " + str(error), file=sys.stderr)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xff == sqlite3.SQLITE_READONLY:
            print("celld_backup: sqlite_readonly (OperationalError); check source directory "
                  "WAL/SHM permissions; source permissions were not changed", file=sys.stderr)
        else:
            print("celld_backup: sqlite_operation_failed (OperationalError; details redacted)",
                  file=sys.stderr)
    except Exception as error:
        # boto/sqlite/OS errors can contain endpoints, paths, SQL values or credentials.
        print("celld_backup: operation_failed (" + type(error).__name__
              + "; upstream details redacted)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
