#!/usr/bin/env bash
#
# Deploy a tagged release on the server. Run by the GitHub Actions workflow over
# SSH, and safe to run by hand:
#
#     sudo -u courseops /opt/courseops/deploy/deploy.sh v0.1.1
#
# The shape of this is decided by one fact: nobody is watching. A deploy that
# half-works at 03:00 and leaves the app down until somebody notices is worse
# than one that refuses to start. So it backs up first, verifies afterwards, and
# puts the previous version back if the new one does not answer.
set -euo pipefail

TAG="${1:?usage: deploy.sh <tag>, e.g. deploy.sh v0.1.1}"
APP_DIR="${APP_DIR:-/opt/courseops}"
SERVICE="${SERVICE:-courseops}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/healthz}"
HEALTH_TRIES="${HEALTH_TRIES:-20}"

cd "$APP_DIR"

say() { printf '\n==> %s\n' "$*"; }

# --- what is running now, so we can put it back ---------------------------
PREVIOUS="$(git rev-parse HEAD)"
say "Currently on $(git describe --tags --always) ($PREVIOUS)"

# --- back up before touching anything -------------------------------------
#
# .backup rather than cp: the database runs in WAL mode and a plain copy taken
# mid-write can be inconsistent. This is the event's entire record - positions,
# incidents, status history, accounts - and a deploy is exactly when you find
# out whether you had a backup.
DB="${DB_PATH:-$APP_DIR/data/courseops.sqlite3}"
if [ -f "$DB" ]; then
    BACKUP="$APP_DIR/data/pre-deploy-$(date +%F-%H%M%S).sqlite3"
    say "Backing up the database to $BACKUP"
    sqlite3 "$DB" ".backup '$BACKUP'"
    # Keep the last ten. Unbounded backups fill a small VPS disk, and a full
    # disk takes the app down in a way that looks nothing like a disk problem.
    ls -1t "$APP_DIR"/data/pre-deploy-*.sqlite3 2>/dev/null | tail -n +11 | xargs -r rm --
fi

# --- fetch and check out the tag ------------------------------------------
say "Fetching $TAG"
git fetch --tags --prune origin
git -c advice.detachedHead=false checkout --force "$TAG"

say "Installing"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet .

say "Restarting $SERVICE"
sudo systemctl restart "$SERVICE"

# --- prove it works -------------------------------------------------------
say "Waiting for $HEALTH_URL"
healthy=0
for attempt in $(seq 1 "$HEALTH_TRIES"); do
    sleep 2
    if body="$(curl --silent --fail --max-time 5 "$HEALTH_URL" 2>/dev/null)"; then
        say "Healthy: $body"
        healthy=1
        break
    fi
    printf '    attempt %s/%s\n' "$attempt" "$HEALTH_TRIES"
done

if [ "$healthy" -ne 1 ]; then
    say "NOT healthy after $((HEALTH_TRIES * 2))s - rolling back to $PREVIOUS"
    git -c advice.detachedHead=false checkout --force "$PREVIOUS"
    .venv/bin/pip install --quiet .
    sudo systemctl restart "$SERVICE"

    if curl --silent --fail --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        say "Rolled back and healthy. The tag $TAG was NOT deployed."
    else
        # Both versions are down, which is not a deploy problem any more.
        say "ROLLED BACK AND STILL UNHEALTHY - the service needs a person."
        say "Try: journalctl -u $SERVICE -n 50 --no-pager"
    fi
    exit 1
fi

say "Deployed $TAG"
