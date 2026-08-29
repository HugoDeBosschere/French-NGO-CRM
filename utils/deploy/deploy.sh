#!/usr/bin/env bash
#
# Turnkey deployment for the campaign-mail import (run ON THE SERVER).
#
# The scripts run INSIDE the app container, via `docker exec`, so they run as the
# same user that owns and writes meetings.db (running them on the host as a
# different user hits "attempt to write a readonly database"). The container
# image does not ship utils/, so we `docker cp` the scripts in first. Everything
# is stdlib-only Python; the only requirement is that the container has outbound
# network (for IMAP and the elus.json fetch), which it does.
#
# Prerequisite: the server checkout carries this branch's utils/ (e.g.
#   git fetch romain && git checkout romain/<branch> -- utils/ )
# and the secrets env file contains IMAP_USER and IMAP_APP_PASSWORD.
#
# Usage (from the app dir, /opt/volunteer-apps/apps/website-meeting):
#     DRY_RUN=1 bash utils/deploy/deploy.sh        # preview, writes nothing
#     bash utils/deploy/deploy.sh                  # real run, moderation queue
#     AUTO=1 bash utils/deploy/deploy.sh           # real run, auto-publish
#     MBOX=~/groupe-campagne.mbox bash utils/deploy/deploy.sh   # + group history
#
# Overridable: CONTAINER, CONTAINER_DB, ENV_FILE, AUTO, MBOX, DRY_RUN.
set -euo pipefail

CONTAINER=${CONTAINER:-website-meeting-app}
CONTAINER_DB=${CONTAINER_DB:-/app/meetings.db}      # DB path *inside* the container
ENV_FILE=${ENV_FILE:-/opt/volunteer-apps/secrets/website-meeting.env}
AUTO=${AUTO:-0}
MBOX=${MBOX:-}
DRY_RUN=${DRY_RUN:-0}

# docker needs root here; the container's own user still owns any file it writes.
DOCKER="sudo docker"

DRY=""; [ "$DRY_RUN" = "1" ] && DRY="--dry-run"
PUB=""; [ "$AUTO" = "1" ] && PUB="--auto-publish"
[ -n "$PUB" ] && echo ">>> AUTO-PUBLISH is ON (writes to the real mails table)." \
              || echo ">>> Moderation mode (drafts land in /moderation)."

# Run a utils script inside the container, passing the secrets as env and the
# in-container DB path. --env-file loads IMAP_USER / IMAP_APP_PASSWORD etc.
in_container() {
    $DOCKER exec --env-file "$ENV_FILE" -e IMAP_DB_PATH="$CONTAINER_DB" \
        "$CONTAINER" python3 "/app/utils/$@"
}

echo "== 1/5  Copy scripts into the container =="
# Remove any previous copy first: `docker cp src container:/app/utils` NESTS into
# an existing dir (creating /app/utils/utils and leaving stale files behind), so
# a plain re-copy would keep running old code. Wipe then copy for a clean state.
$DOCKER exec "$CONTAINER" rm -rf /app/utils
$DOCKER cp utils "$CONTAINER":/app/utils
echo "   utils/ copied to $CONTAINER:/app/utils"

echo "== 2/5  Backup the database =="
if [ "$DRY_RUN" = "1" ]; then
    echo "   [dry-run] would copy $CONTAINER_DB -> $CONTAINER_DB.bak-<timestamp> (in container)"
else
    $DOCKER exec "$CONTAINER" cp "$CONTAINER_DB" "$CONTAINER_DB.bak-$(date +%F-%H%M%S)"
    echo "   backed up alongside $CONTAINER_DB"
fi

echo "== 3/5  Sync élu·e emails from elus.json (fills senators) =="
in_container sync_emails_from_elus.py --db "$CONTAINER_DB" $DRY

if [ -n "$MBOX" ]; then
    echo "== 4a/5 Historical backfill from mbox: $MBOX =="
    $DOCKER cp "$MBOX" "$CONTAINER":/tmp/backfill.mbox
    in_container import_campaign_mails.py --mbox /tmp/backfill.mbox $PUB $DRY
fi

echo "== 4b/5 IMAP backfill of the follow-up mailbox =="
in_container import_campaign_mails.py --backfill $PUB $DRY

echo "== 5/5  Install & enable the daily systemd timer =="
if [ "$DRY_RUN" = "1" ]; then
    echo "   [dry-run] would install import-campaign-mails.{service,timer} and enable the timer"
else
    sudo cp utils/deploy/import-campaign-mails.service /etc/systemd/system/
    sudo cp utils/deploy/import-campaign-mails.timer   /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now import-campaign-mails.timer
    systemctl list-timers | grep import-campaign || true
fi

echo
echo "Done."
echo "Reminder: the daily timer runs in MODERATION mode by default. To make the"
echo "daily run auto-publish, add this line to $ENV_FILE (takes effect next run):"
echo "    IMPORT_AUTO_PUBLISH=1"
echo "Logs:  journalctl -u import-campaign-mails.service -n 50"
