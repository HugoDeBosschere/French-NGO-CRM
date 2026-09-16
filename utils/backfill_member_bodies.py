#!/usr/bin/env python3
"""One-off backfill: put the real mail body into "Corps du texte" (summary) for
member mails imported before the fix.

Early versions of import_member_mails.py stored a generated one-liner ("Mail de X
à Y — « objet »") in `summary` while keeping the real body only in `mail_bodies`.
Since the CRM's "Corps du texte" field is `summary`, imported member mails showed
that one-liner instead of the actual content. This script repairs existing rows:

- Published mails (table `mails`): the real body is already in `mail_bodies`, so
  copy it into `summary`. No network needed.
- Pending mails (table `pending_mails`, awaiting moderation): the body was never
  stored, so re-read the audit mailbox over IMAP and match each staged row by
  (direction, mail_date, subject) to recover its body. Needs MEMBER_IMAP_*.

Idempotent and safe to re-run. Use --dry-run first.
"""
import argparse
import email
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_campaign_mails import (  # noqa: E402
    decoded, mail_date_iso, fetch_uids, log,
)
from import_member_mails import (  # noqa: E402
    IMPORT_SOURCE, DEFAULT_DB, ensure_member_tables, classify, extract_body,
    connect_imap, load_email_index_with_aliases, build_name_pattern_index,
)


def backfill_published(db, dry_run):
    rows = db.execute(
        """
        SELECT x.id, x.summary, b.body
        FROM mails x
        JOIN mail_members mm ON mm.mail_id = x.id
        JOIN mail_bodies  b  ON b.mail_id  = x.id
        WHERE b.body IS NOT NULL AND trim(b.body) <> '' AND x.summary <> b.body
        """
    ).fetchall()
    for mail_id, _summary, body in rows:
        if not dry_run:
            db.execute("UPDATE mails SET summary = ? WHERE id = ?", (body, mail_id))
    log(f"Published mails fixed from mail_bodies: {len(rows)}")
    return len(rows)


def backfill_pending(db, dry_run):
    pend = db.execute(
        """
        SELECT id, direction, mail_date, COALESCE(subject, '')
        FROM pending_mails WHERE submitted_by = ?
        """,
        (IMPORT_SOURCE,),
    ).fetchall()
    if not pend:
        log("Pending member mails: none.")
        return 0
    if not (os.environ.get("MEMBER_IMAP_USER") and
            os.environ.get("MEMBER_IMAP_APP_PASSWORD")):
        log(f"Pending member mails: {len(pend)} found, but MEMBER_IMAP_* not set "
            f"— skipping (needs the audit mailbox to recover their body).")
        return 0

    email_index = load_email_index_with_aliases(db)
    name_patterns = build_name_pattern_index(db)
    mailbox = os.environ.get("MEMBER_IMAP_MAILBOX", "INBOX")
    conn = connect_imap()
    bodies = {}  # (direction, mail_date, subject) -> body
    try:
        for uid in fetch_uids(conn, mailbox, 0, True):
            status, data = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            direction, _m, _mem, _l, _low = classify(
                msg, db, email_index, name_patterns)
            if not direction:
                continue
            body = extract_body(msg)
            if not body:
                continue
            subject = decoded(msg.get("Subject")) or "(sans objet)"
            bodies[(direction, mail_date_iso(msg), subject)] = body
    finally:
        conn.logout()

    fixed = 0
    for pid, direction, mail_date, subject in pend:
        body = bodies.get((direction, mail_date, subject))
        if body:
            if not dry_run:
                db.execute("UPDATE pending_mails SET summary = ? WHERE id = ?",
                           (body, pid))
            fixed += 1
    log(f"Pending mails matched and fixed: {fixed}/{len(pend)}")
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("IMAP_DB_PATH", DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    ensure_member_tables(db)
    backfill_published(db, args.dry_run)
    backfill_pending(db, args.dry_run)
    if not args.dry_run:
        db.commit()
    db.close()
    log("Done." + (" (dry-run, nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
