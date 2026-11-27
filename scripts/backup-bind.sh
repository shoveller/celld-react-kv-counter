#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

exec "${CELLD_BACKUP_PYTHON:-python3}" scripts/celld_backup.py backup \
  --source "${CELLD_BACKUP_SOURCE:-data/dev/objects.sqlite3}" \
  --work-dir "${CELLD_BACKUP_WORK_DIR:-data/backups}" \
  --revision "$(git rev-parse HEAD)" \
  "$@"
