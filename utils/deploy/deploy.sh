#!/usr/bin/env bash
#
# Turnkey deployment for the campaign-mail import (run ON THE SERVER, as the app
# owner, e.g. `romain`). Host-side: the scripts are stdlib-only Python and write
# to the persisted SQLite DB that the Dockerised app also uses — no docker exec,
# no file mounting.
#
# Prerequisite: the app repo checkout on the server is already up to date with
# this branch (git pull + docker-compose build/up per DEPLOYMENT_DOCUMENTATION),
# so utils/ carries the three scripts. The secrets env file must contain
# IMAP_USER and IMAP_APP_PASSWORD.
#
# Usage:
#     # Preview everything, write nothing:
#     DRY_RUN=1 bash utils/deploy/deploy.sh
#
#     # Real run, drafts go to the moderation queue (recommended first time):
#     bash utils/deploy/deploy.sh
#
#     # Real run, publish straight to the mails table:
#     AUTO=1 bash utils/deploy/deploy.sh
#
#     # Also replay the group's history from a Google Takeout .mbox:
#     MBOX=~/groupe-campagne.mbox bash utils/deploy/deploy.sh
#
# Overridable: APP_DIR, DB, ENV_FILE, AUTO, MBOX, DRY_RUN.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/volunteer-apps/apps/website-meeting}
DB=${DB:-/opt/volunteer-apps/data/website-meeting-data/meetings.db}
ENV_FILE=${ENV_FILE:-/opt/volunteer-apps/secrets/website-meeting.env}
AUTO=${AUTO:-0}
MBOX=${MBOX:-}
DRY_RUN=${DRY_RUN:-0}

cd "$APP_DIR"
export IMAP_DB_PATH="$DB"
# Load IMAP_* / IMPORT_* from the secrets file. It is usually root-only, so read
# it via sudo and export just those keys — this keeps Python running as the
# current user (running it as root would leave the DB root-owned and break the
# app, which runs as uid 1000).
if [ -r "$ENV_FILE" ]; then
    reader() { cat "$ENV_FILE"; }
else
    echo "   (secrets file not readable directly — reading it via sudo)"
    reader() { sudo cat "$ENV_FILE"; }
fi
while IFS= read -r line; do
    export "$line"
done < <(reader | grep -E '^(IMAP_|IMPORT_)[A-Za-z_]+=')
if [ -z "${IMAP_USER:-}" ] || [ -z "${IMAP_APP_PASSWORD:-}" ]; then
    echo "ERROR: IMAP_USER / IMAP_APP_PASSWORD missing from $ENV_FILE." >&2
    echo "Add them (sudo nano $ENV_FILE) then re-run." >&2
    exit 1
fi

DRY=""; [ "$DRY_RUN" = "1" ] && DRY="--dry-run"
PUB=""; [ "$AUTO" = "1" ] && PUB="--auto-publish"
[ -n "$PUB" ] && echo ">>> AUTO-PUBLISH is ON (writes to the real mails table)." \
              || echo ">>> Moderation mode (drafts land in /moderation)."

echo "== 1/4  Backup the database =="
if [ "$DRY_RUN" = "1" ]; then
    echo "   [dry-run] would copy $DB -> $DB.bak-<timestamp>"
else
    cp -v "$DB" "$DB.bak-$(date +%F-%H%M%S)"
fi

echo "== 2/4  Sync élu·e emails from elus.json (fills senators) =="
python3 utils/sync_emails_from_elus.py --db "$DB" $DRY

if [ -n "$MBOX" ]; then
    echo "== 3a/4 Historical backfill from mbox: $MBOX =="
    python3 utils/import_campaign_mails.py --mbox "$MBOX" $PUB $DRY
fi

echo "== 3b/4 IMAP backfill of the follow-up mailbox =="
python3 utils/import_campaign_mails.py --backfill $PUB $DRY

echo "== 4/4  Install & enable the daily systemd timer =="
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
echo "daily run auto-publish, add this line to $ENV_FILE and it takes effect next run:"
echo "    IMPORT_AUTO_PUBLISH=1"
echo "Logs:  journalctl -u import-campaign-mails.service -n 50"
