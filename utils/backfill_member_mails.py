#!/usr/bin/env python3
"""One-off backfill: attach past *manually-entered* member↔élu mails to members.

Before the automatic member-mail import existed, members logged their exchanges
with élu·es by hand via "+ Courriel". Those rows live in `mails` (+ `mail_persons`
for the élu·e) but carry no `mail_members` link — so they show in "Suivi des
échanges" but not as member exchanges (no 👥 tag, absent from member pages).

This script links each such mail to the right member, using the moderator who
logged it (`mails.received_by`) mapped to their @pauseia.fr address. Members are
upserted by e-mail, so a backfilled member MERGES with the one the automatic
import may already have created — no duplicates. Idempotent: a mail already
linked is skipped.

Mails whose logger isn't in MAPPING are left untouched and reported (e.g. members
who had no @pauseia.fr address at the time). Add them here and re-run once known.

Usage (in the app container):
    python3 utils/backfill_member_mails.py --dry-run
    python3 utils/backfill_member_mails.py
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_member_mails import ensure_member_tables, upsert_member  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# moderator name (mails.received_by -> moderators.name)  ->  (email, display)
# Fill in the unknowns and re-run to attach their mails too.
MAPPING = {
    "Élie (elie520)": ("elie@pauseia.fr", "Elie Goudout"),
    "Hugo": ("hugo@pauseia.fr", "Hugo De Bosschere"),
    # "Samy (@galaxam196)": ("?@pauseia.fr", "Samy ..."),   # email inconnu
    # "antinomie8": ("?@pauseia.fr", "..."),                # email inconnu
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    ensure_member_tables(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Manually-entered mails linked to an élu·e but not yet to a member.
    rows = db.execute(
        """
        SELECT x.id, m.name AS who
        FROM mails x
        JOIN mail_persons xp ON xp.mail_id = x.id
        LEFT JOIN moderators m ON m.id = x.received_by
        WHERE x.received_by IS NOT NULL
          AND x.id NOT IN (SELECT mail_id FROM mail_members)
        GROUP BY x.id
        ORDER BY x.id
        """
    ).fetchall()

    linked, skipped = {}, {}
    for r in rows:
        who = r["who"]
        if who not in MAPPING:
            skipped[who] = skipped.get(who, 0) + 1
            continue
        email, display = MAPPING[who]
        if args.dry_run:
            linked[who] = linked.get(who, 0) + 1
            continue
        member_id = upsert_member(db, email, display, now)
        db.execute(
            "INSERT OR IGNORE INTO mail_members (mail_id, member_id) VALUES (?, ?)",
            (r["id"], member_id),
        )
        linked[who] = linked.get(who, 0) + 1

    if not args.dry_run:
        db.commit()

    print(f"{'[dry-run] ' if args.dry_run else ''}Linked to members:")
    for who, n in sorted(linked.items(), key=lambda x: -x[1]):
        print(f"  {n:>4}  {who} -> {MAPPING[who][0]}")
    if skipped:
        print("Left untouched (logger not in MAPPING — add their email and re-run):")
        for who, n in sorted(skipped.items(), key=lambda x: -x[1]):
            print(f"  {n:>4}  {who}")


if __name__ == "__main__":
    main()
