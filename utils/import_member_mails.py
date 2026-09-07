#!/usr/bin/env python3
"""Import association members' correspondence with élu·es into the CRM.

Companion to import_campaign_mails.py. Where that one tracks *citizens* writing
to their élu·e (via the campaign BCC box), this one tracks the association's own
members (@pauseia.fr) exchanging mail with élu·es — both directions:

    member  → élu   (direction 'sent',     the association wrote)
    élu     → member (direction 'received', the association got a reply)

How the mail gets here (no per-member setup, new members covered automatically):
a Google Workspace **content-compliance / routing rule** copies every message
where one side is an élu·e address (@senat.fr / @assemblee-nationale.fr /
@europarl.europa.eu) and the other a @pauseia.fr account, into a single audit
mailbox (e.g. suivi-membres@pauseia.fr). This script reads that mailbox over
IMAP, matches the élu·e on persons.email, identifies the member from the
@pauseia.fr header, and records the mail — attributing the member.

Members are stored in a `members` table, created **on the fly** from the mail
headers (email + display name): a member appears the first time they mail an
élu·e, so nothing manual is needed for newcomers. Group addresses (campagne@,
contact@, all@, …) are excluded.

Config (env; read from the server secrets file, never hard-coded):
    MEMBER_IMAP_HOST          default: imap.gmail.com
    MEMBER_IMAP_PORT          default: 993
    MEMBER_IMAP_USER          the audit mailbox login (e.g. suivi-membres@pauseia.fr)
    MEMBER_IMAP_APP_PASSWORD  its app password
    MEMBER_IMAP_MAILBOX       default: INBOX
    IMAP_DB_PATH              path to meetings.db (shared with the other importer)
    IMPORT_AUTO_PUBLISH=1     publish straight to `mails` (else moderation queue)

Usage:
    python3 utils/import_member_mails.py --backfill          # first full pass
    python3 utils/import_member_mails.py                     # daily incremental
    python3 utils/import_member_mails.py --backfill --dry-run --verbose
    python3 utils/import_member_mails.py --auto-publish
"""
import argparse
import email
import imaplib
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from email.utils import getaddresses

# Reuse the building blocks already validated in the campaign importer.
from import_campaign_mails import (  # noqa: E402
    OFFICIAL_DOMAINS, decoded, ensure_state_table, get_last_uid, set_last_uid,
    load_email_index, already_imported, fetch_uids, mail_date_iso, log,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))
MEMBER_DOMAIN = "pauseia.fr"

# @pauseia.fr addresses that are groups/services, not individual members.
GROUP_ADDRESSES = {
    "campagne@pauseia.fr", "suivi-campagne@pauseia.fr", "suivi-membres@pauseia.fr",
    "contact@pauseia.fr", "all@pauseia.fr", "dons@pauseia.fr",
    "newsletter@pauseia.fr", "presse@pauseia.fr", "netlify@pauseia.fr",
    "admin@pauseia.fr", "lecteurs@pauseia.fr", "contributeurs-ml@pauseia.fr",
}

IMPORT_SOURCE = "Import automatique (mails membres)"


def is_official(addr):
    return addr.lower().endswith(tuple("@" + d for d in OFFICIAL_DOMAINS))


def is_member(addr):
    a = addr.lower()
    return a.endswith("@" + MEMBER_DOMAIN) and a not in GROUP_ADDRESSES


def addr_pairs(msg, *headers):
    """(display, address) pairs across the given headers, addresses lower-cased."""
    pairs = getaddresses(sum((msg.get_all(h, []) for h in headers), []))
    return [(d, a.lower()) for d, a in pairs if a]


def ensure_member_tables(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS members (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT UNIQUE NOT NULL,
            name       TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mail_members (
            mail_id   INTEGER NOT NULL REFERENCES mails(id)    ON DELETE CASCADE,
            member_id INTEGER NOT NULL REFERENCES members(id)  ON DELETE CASCADE,
            PRIMARY KEY (mail_id, member_id)
        );
        -- Message-ID -> élu·e person ids (CSV), so a reply from a NON-official
        -- address can still be attributed to the right élu·e via In-Reply-To /
        -- References (thread linking).
        CREATE TABLE IF NOT EXISTS thread_persons (
            message_id TEXT PRIMARY KEY,
            person_ids TEXT NOT NULL
        );
        """
    )
    db.commit()


def _referenced_ids(msg):
    """Message-IDs this message replies to (In-Reply-To + References)."""
    raw = " ".join(msg.get_all("In-Reply-To", []) + msg.get_all("References", []))
    return set(re.findall(r"<[^>]+>", raw))


def remember_thread(db, message_id, person_ids):
    if message_id and person_ids:
        db.execute(
            "INSERT OR REPLACE INTO thread_persons (message_id, person_ids) VALUES (?, ?)",
            (message_id, ",".join(str(p) for p in person_ids)),
        )


def thread_persons_lookup(db, msg):
    """Return [(pid, name)] inherited from the thread this message replies to."""
    refs = _referenced_ids(msg)
    if not refs:
        return []
    pids = []
    for ref in refs:
        row = db.execute(
            "SELECT person_ids FROM thread_persons WHERE message_id = ?", (ref,)
        ).fetchone()
        if row:
            pids.extend(int(x) for x in row[0].split(",") if x)
    out, seen = [], set()
    for pid in pids:
        if pid in seen:
            continue
        r = db.execute("SELECT id, name FROM persons WHERE id = ?", (pid,)).fetchone()
        if r:
            seen.add(pid)
            out.append((r[0], r[1]))
    return out


def upsert_member(db, email_addr, display, now):
    """Return the member id for this @pauseia.fr address, creating it on first
    sight. Fills/upgrades the name when the header provides a better one."""
    email_addr = email_addr.lower()
    row = db.execute("SELECT id, name FROM members WHERE email = ?",
                     (email_addr,)).fetchone()
    name = (display or "").strip() or None
    if row is None:
        cur = db.execute(
            "INSERT INTO members (email, name, created_at) VALUES (?, ?, ?)",
            (email_addr, name, now),
        )
        return cur.lastrowid
    mid, existing = row
    if name and not existing:
        db.execute("UPDATE members SET name = ? WHERE id = ?", (name, mid))
    return mid


def classify(msg, db, email_index):
    """Work out (direction, élu matches, (member_email, member_display)).

    direction 'sent'     = a member wrote to an élu·e (member in From, élu in To/Cc)
    direction 'received' = an élu·e wrote to a member (élu in From, member in To/Cc)
    Falls back to thread linking so a reply from a NON-official élu·e address is
    still attributed via In-Reply-To / References. Returns (None, [], None) when
    it isn't a clear member↔élu message.
    """
    from_pairs = addr_pairs(msg, "From")
    to_pairs = addr_pairs(msg, "To", "Cc")

    from_official = [a for _d, a in from_pairs if is_official(a)]
    to_official = [a for _d, a in to_pairs if is_official(a)]
    from_member = [(d, a) for d, a in from_pairs if is_member(a)]
    to_member = [(d, a) for d, a in to_pairs if is_member(a)]

    def resolve(addrs):
        out, seen = [], set()
        for a in addrs:
            for pid, name in email_index.get(a, []):
                if pid not in seen:
                    seen.add(pid)
                    out.append((pid, name))
        return out

    # 1) Address-based (official élu·e domain in the headers).
    if from_member and to_official:
        matches = resolve(to_official)
        if matches:
            return "sent", matches, (from_member[0][1], from_member[0][0])
    if from_official and to_member:
        matches = resolve(from_official)
        if matches:
            return "received", matches, (to_member[0][1], to_member[0][0])

    # 2) Thread linking: a reply involving a member, from/to an address we can't
    #    match directly — inherit the élu·e from the mail it replies to.
    inherited = thread_persons_lookup(db, msg)
    if inherited:
        if to_member and not from_member:          # élu·e (other address) → member
            return "received", inherited, (to_member[0][1], to_member[0][0])
        if from_member:                            # member follow-up in the thread
            return "sent", inherited, (from_member[0][1], from_member[0][0])
    return None, [], None


def record(db, msg, direction, matches, member, dry_run, auto_publish):
    message_id = (msg.get("Message-ID") or "").strip()
    subject = decoded(msg.get("Subject")) or "(sans objet)"
    mail_date = mail_date_iso(msg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    member_email, member_display = member
    member_name = (member_display or "").strip() or member_email
    elu_names = ", ".join(n for _p, n in matches)
    if direction == "sent":
        summary = f"Mail de {member_name} à {elu_names} — « {subject} »"
    else:
        summary = f"Mail de {elu_names} à {member_name} — « {subject} »"

    if dry_run:
        log(f"  [dry-run] {'publish' if auto_publish else 'stage'} {direction} -> "
            f"{member_name} / {elu_names} | {mail_date} | {subject!r}")
        return

    member_id = upsert_member(db, member_email, member_display, now)

    if auto_publish:
        cur = db.execute(
            """
            INSERT INTO mails (mail_date, direction, important, summary,
                follow_up_date, received_by, validated_by, document_stored_name,
                document_orig_name, created_at)
            VALUES (?, ?, 0, ?, NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (mail_date, direction, summary, now),
        )
        mail_id = cur.lastrowid
        db.executemany("INSERT INTO mail_persons (mail_id, person_id) VALUES (?, ?)",
                       [(mail_id, pid) for pid, _n in matches])
        db.execute("INSERT OR IGNORE INTO mail_members (mail_id, member_id) "
                   "VALUES (?, ?)", (mail_id, member_id))
        remember_thread(db, message_id, [pid for pid, _n in matches])
    else:
        db.execute(
            """
            INSERT INTO pending_mails (mail_date, direction, important, summary,
                follow_up_date, proposed_people, submitted_by,
                document_stored_name, document_orig_name, created_at)
            VALUES (?, ?, 0, ?, NULL, ?, ?, NULL, NULL, ?)
            """,
            (mail_date, direction, summary, elu_names, IMPORT_SOURCE, now),
        )
        remember_thread(db, message_id, [pid for pid, _n in matches])

    if message_id:
        db.execute(
            "INSERT OR IGNORE INTO imported_mails (message_id, uid, mailbox, "
            "imported_at) VALUES (?, ?, ?, ?)",
            (message_id, None, "members", now),
        )


def connect_imap():
    host = os.environ.get("MEMBER_IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("MEMBER_IMAP_PORT", "993"))
    user = os.environ.get("MEMBER_IMAP_USER")
    password = os.environ.get("MEMBER_IMAP_APP_PASSWORD")
    if not user or not password:
        sys.exit("MEMBER_IMAP_USER and MEMBER_IMAP_APP_PASSWORD must be set "
                 "(the audit mailbox that the Gmail routing rule copies into).")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    return conn


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true",
                        help="sweep the whole mailbox instead of only new UIDs")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be recorded without writing")
    parser.add_argument("--auto-publish", action="store_true",
                        help="publish to the real mails table (also IMPORT_AUTO_PUBLISH=1)")
    parser.add_argument("--verbose", action="store_true",
                        help="print messages that don't classify as member↔élu")
    args = parser.parse_args()

    auto_publish = args.auto_publish or os.environ.get("IMPORT_AUTO_PUBLISH") == "1"
    db_path = os.environ.get("IMAP_DB_PATH", DEFAULT_DB)
    mailbox = os.environ.get("MEMBER_IMAP_MAILBOX", "INBOX")

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")
    if not args.dry_run:
        ensure_state_table(db)
        ensure_member_tables(db)
    email_index = load_email_index(db)
    log(f"Loaded {len(email_index)} élu·e e-mail(s) from {db_path}. "
        f"Output: {'auto-publish' if auto_publish else 'moderation queue'}.")

    conn = connect_imap()
    try:
        last_uid = get_last_uid(db, "members")
        uids = fetch_uids(conn, mailbox, last_uid, args.backfill)
        log(f"Audit mailbox {mailbox!r}: {len(uids)} message(s) to inspect.")
        imported = dup = skipped = max_uid = 0
        max_uid = last_uid
        for uid in uids:
            status, data = conn.uid("fetch", str(uid), "(RFC822)")
            if status == "OK" and data and data[0]:
                msg = email.message_from_bytes(data[0][1])
                mid = (msg.get("Message-ID") or "").strip()
                if already_imported(db, mid):
                    dup += 1
                else:
                    direction, matches, member = classify(msg, db, email_index)
                    if direction:
                        record(db, msg, direction, matches, member,
                               args.dry_run, auto_publish)
                        imported += 1
                    else:
                        skipped += 1
                        if args.verbose:
                            log(f"  [skip] {decoded(msg.get('Subject'))!r}")
            max_uid = max(max_uid, uid)

        if not args.dry_run:
            set_last_uid(db, "members", max_uid)
            db.commit()
        verb = "published" if auto_publish else "staged"
        log(f"Done. Mails {verb}: {imported} | already-imported skipped: {dup} | "
            f"not member↔élu: {skipped} | last UID now: "
            f"{max_uid if not args.dry_run else last_uid}.")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
