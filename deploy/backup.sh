#!/usr/bin/env bash
#
# Back the database up to <install>/backups, keeping the newest N of each kind.
#
#     backup.sh              nightly, from cron
#     backup.sh pre-deploy   from deploy.sh, before anything is touched
#
# The SQLite file is the event's entire record - positions, incidents, status
# history, accounts. Until this existed the only copy was the one deploy.sh
# took, so between releases there was exactly one copy on one disk.
#
# .backup rather than cp: the database runs in WAL mode and a plain copy taken
# mid-write can be inconsistent, silently. The rotation is per label, so a
# burst of deploys cannot push the nightlies out.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
LABEL="${1:-nightly}"
KEEP="${KEEP:-14}"
DB="${DB_PATH:-$APP_DIR/data/courseops.sqlite3}"
DIR="${BACKUP_DIR:-$APP_DIR/backups}"

if [ ! -f "$DB" ]; then
    echo "no database at $DB - nothing to back up"
    exit 0
fi

# Mode 700: the backups hold everything the live database does, and the app
# user is the only one who needs them.
mkdir -p "$DIR"
chmod 700 "$DIR"

OUT="$DIR/$LABEL-$(date +%F-%H%M%S).sqlite3"
# Two runs inside one second would otherwise overwrite each other silently.
[ -e "$OUT" ] && OUT="${OUT%.sqlite3}-$$.sqlite3"
sqlite3 "$DB" ".backup '$OUT'"
chmod 600 "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Unbounded backups fill a small VPS disk, and a full disk takes the app down
# in a way that looks nothing like a disk problem.
ls -1t "$DIR/$LABEL-"*.sqlite3 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

# Restore: stop the service, copy the file over data/courseops.sqlite3 (and
# delete any -wal and -shm beside it), start the service.
