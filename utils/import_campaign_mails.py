#!/usr/bin/env python3
"""Import campaign BCC mails into the CRM's moderation queue — cron-friendly.

Context (see the "Automatisation import des mails campagne → CRM" brief):
citizens use the site to write to their MP, and put a follow-up mailbox in BCC.
The copy landing in that mailbox keeps the MP's real address in the `To:` header,
so we can tell which parlementaire received a mail and when — without any manual
entry. This script reads that mailbox over IMAP, matches recipients against
`persons.email` (already filled for députés/sénateurs/eurodéputés by the insert
scripts), and stages one draft per matched person in `pending_mails`.

Nothing is published straight to the public record: drafts land in the very same
`pending_mails` queue as anonymous "declarer" submissions, so a certified (Tier 2)
member reviews each import on /moderation before it becomes a real mail. That is a
free safety net and reuses the existing flow.

Two output modes:
- Moderation (default): stage a draft in `pending_mails` for a Tier 2 member to
  validate on /moderation. Safest — use it while confirming matching is reliable.
- Auto-publish (`--auto-publish` or IMPORT_AUTO_PUBLISH=1): write straight into
  the real `mails` table with a structured `mail_persons` link to every matched
  person. Because matching yields a certain `persons.id` (email match or the
  X-Elu-Id marker), this is a clean full automation once the To: test is trusted.

Usage:
    # First run: sweep the whole mailbox history (into the moderation queue).
    python3 utils/import_campaign_mails.py --backfill

    # Daily cron: only messages with a higher IMAP UID than the last processed one.
    python3 utils/import_campaign_mails.py

    # See what would be staged/published without writing anything.
    python3 utils/import_campaign_mails.py --backfill --dry-run

    # Full automation: publish directly to the real mails table.
    python3 utils/import_campaign_mails.py --auto-publish

Configuration (never hard-code the password — read it from the server env file,
e.g. /opt/volunteer-apps/secrets/website-meeting.env):
    IMAP_HOST          IMAP server            (default: imap.gmail.com)
    IMAP_PORT          IMAP SSL port          (default: 993)
    IMAP_USER          mailbox login          (e.g. suivi-campagne@pauseia.fr)
    IMAP_APP_PASSWORD  app password           (from Vaultwarden)
    IMAP_MAILBOX       folder to read         (default: INBOX)
    IMAP_DB_PATH       path to meetings.db    (default: <repo>/meetings.db)

Anti-duplicate strategy (both belt and braces):
- Incremental runs remember the last IMAP UID processed per mailbox and only fetch
  UIDs above it (robust to timezones / missed days, unlike a date filter).
- Every message's Message-ID is recorded, so the same mail is never staged twice
  even if UIDs reset (mailbox recreated) or a backfill overlaps a daily run.

Matching:
- Primary: an explicit marker header injected by the site — `X-Elu-Id` /
  `X-Depute-Id` carrying a `persons.id`. This is match-certain even if the
  official address changes.
- Fallback: every address found in `To`, `Cc`, `X-Original-To` and `Delivered-To`
  is matched case-insensitively against `persons.email`. One draft per matched
  person (a single mail may target several élu·es).
"""
import argparse
import email
import imaplib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "meetings.db")

# Headers that may carry the real recipient address (the Google Group can rewrite
# some of them, hence the belt-and-braces list).
RECIPIENT_HEADERS = ("To", "Cc", "X-Original-To", "Delivered-To")
# Headers that may carry an explicit person id injected by the site.
MARKER_HEADERS = ("X-Elu-Id", "X-Depute-Id")

# Value written to pending_mails.submitted_by so a moderator can see the origin.
IMPORT_SOURCE = "Import automatique (mail campagne)"


def log(msg):
    print(msg, flush=True)


def decoded(value):
    """RFC 2047-decode a header value into a plain str (never raises)."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def ensure_state_table(db):
    """Tables owned by this script (kept separate from the app schema)."""
    db.executescript(
        """
        -- Last IMAP UID processed, per mailbox, for incremental runs.
        CREATE TABLE IF NOT EXISTS imported_mail_state (
            mailbox  TEXT PRIMARY KEY,
            last_uid INTEGER NOT NULL DEFAULT 0
        );
        -- Every message we have already handled, so we never stage it twice.
        CREATE TABLE IF NOT EXISTS imported_mails (
            message_id  TEXT PRIMARY KEY,
            uid         INTEGER,
            mailbox     TEXT,
            imported_at TEXT NOT NULL
        );
        """
    )
    db.commit()


def get_last_uid(db, mailbox):
    try:
        row = db.execute(
            "SELECT last_uid FROM imported_mail_state WHERE mailbox = ?", (mailbox,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0  # state table not created yet (e.g. a dry-run before first write)
    return row[0] if row else 0


def set_last_uid(db, mailbox, uid):
    db.execute(
        """
        INSERT INTO imported_mail_state (mailbox, last_uid) VALUES (?, ?)
        ON CONFLICT(mailbox) DO UPDATE SET last_uid = excluded.last_uid
        """,
        (mailbox, uid),
    )


def load_email_index(db):
    """persons.email (lower-cased) -> list of (id, name). Skips blank emails."""
    index = {}
    for pid, name, mail in db.execute(
        "SELECT id, name, email FROM persons WHERE email IS NOT NULL AND email != ''"
    ):
        index.setdefault(mail.strip().lower(), []).append((pid, name))
    return index


def person_by_id(db, pid):
    row = db.execute("SELECT id, name FROM persons WHERE id = ?", (pid,)).fetchone()
    return (row[0], row[1]) if row else None


def match_recipients(msg, db, email_index):
    """Return the list of (person_id, name) this message is addressed to.

    Deduplicated by person_id, preserving discovery order.
    """
    matches, seen = [], set()

    # 1) Explicit marker header wins (match-certain).
    for header in MARKER_HEADERS:
        for raw in msg.get_all(header, []):
            digits = "".join(c for c in str(raw) if c.isdigit())
            if not digits:
                continue
            hit = person_by_id(db, int(digits))
            if hit and hit[0] not in seen:
                seen.add(hit[0])
                matches.append(hit)

    # 2) Address headers matched against persons.email.
    pairs = []
    for header in RECIPIENT_HEADERS:
        pairs.extend(getaddresses(msg.get_all(header, [])))
    for _display, addr in pairs:
        addr = (addr or "").strip().lower()
        for pid, name in email_index.get(addr, []):
            if pid not in seen:
                seen.add(pid)
                matches.append((pid, name))
    return matches


def mail_date_iso(msg):
    """`Date:` header -> YYYY-MM-DD (falls back to today on a missing/bad date)."""
    raw = msg.get("Date")
    if raw:
        try:
            return parsedate_to_datetime(raw).date().isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).date().isoformat()


def stage_message(db, msg, uid, mailbox, matches, dry_run, auto_publish):
    """Record one mail (to all matched persons) and remember its Message-ID.

    Moderation mode -> one `pending_mails` draft, names joined in proposed_people.
    Auto-publish     -> one real `mails` row + a `mail_persons` link per person.
    A single citizen mail is one mail with several recipients, never duplicated.
    """
    message_id = (msg.get("Message-ID") or "").strip()
    subject = decoded(msg.get("Subject")) or "(sans objet)"
    sender = decoded(msg.get("From"))
    mail_date = mail_date_iso(msg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    names = [name for _pid, name in matches]
    summary = (
        f"Mail d'un citoyen à {', '.join(names)} — « {subject} »"
        + (f" (expéditeur : {sender})" if sender else "")
    )

    if dry_run:
        mode = "publish" if auto_publish else "stage"
        log(f"  [dry-run] {mode} -> {', '.join(names)} | {mail_date} | {subject!r}")
        return

    if auto_publish:
        cur = db.execute(
            """
            INSERT INTO mails (
                mail_date, direction, important, summary, follow_up_date,
                received_by, validated_by, document_stored_name,
                document_orig_name, created_at
            ) VALUES (?, 'sent', 0, ?, NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (mail_date, summary, now),
        )
        db.executemany(
            "INSERT INTO mail_persons (mail_id, person_id) VALUES (?, ?)",
            [(cur.lastrowid, pid) for pid, _name in matches],
        )
    else:
        db.execute(
            """
            INSERT INTO pending_mails (
                mail_date, direction, important, summary, follow_up_date,
                proposed_people, submitted_by, document_stored_name,
                document_orig_name, created_at
            ) VALUES (?, 'sent', 0, ?, NULL, ?, ?, NULL, NULL, ?)
            """,
            (mail_date, summary, ", ".join(names), IMPORT_SOURCE, now),
        )

    if message_id:
        db.execute(
            """
            INSERT OR IGNORE INTO imported_mails (message_id, uid, mailbox, imported_at)
            VALUES (?, ?, ?, ?)
            """,
            (message_id, uid, mailbox, now),
        )


def already_imported(db, message_id):
    if not message_id:
        return False
    try:
        return db.execute(
            "SELECT 1 FROM imported_mails WHERE message_id = ?", (message_id,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False  # state table not created yet (e.g. a dry-run before first write)


def connect_imap():
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER")
    password = os.environ.get("IMAP_APP_PASSWORD")
    if not user or not password:
        sys.exit(
            "IMAP_USER and IMAP_APP_PASSWORD must be set (read them from the server "
            "env file, e.g. /opt/volunteer-apps/secrets/website-meeting.env)."
        )
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    return conn


def fetch_uids(conn, mailbox, last_uid, backfill):
    """Return the sorted list of UIDs to process."""
    status, _ = conn.select(mailbox, readonly=True)
    if status != "OK":
        sys.exit(f"Cannot select mailbox {mailbox!r}.")
    criterion = "ALL" if backfill else f"UID {last_uid + 1}:*"
    status, data = conn.uid("search", None, criterion)
    if status != "OK":
        sys.exit("IMAP search failed.")
    uids = [int(x) for x in data[0].split()]
    # An open-ended `UID n:*` search always returns at least the last message even
    # when none is newer; drop anything we have already passed.
    if not backfill:
        uids = [u for u in uids if u > last_uid]
    return sorted(uids)


def handle_message(db, msg, uid, mailbox, email_index, dry_run, auto_publish,
                   verbose):
    """Process one parsed message. Returns 'dup', 'staged' or 'unmatched'."""
    message_id = (msg.get("Message-ID") or "").strip()
    if already_imported(db, message_id):
        return "dup"
    matches = match_recipients(msg, db, email_index)
    if matches:
        stage_message(db, msg, uid, mailbox, matches, dry_run, auto_publish)
        return "staged"
    if verbose:
        addrs = sorted({a.lower() for _d, a in getaddresses(
            sum((msg.get_all(h, []) for h in RECIPIENT_HEADERS), [])) if a})
        log(f"  [no match] {decoded(msg.get('Subject'))!r} "
            f"-> {', '.join(addrs) or '(no recipient header)'}")
    return "unmatched"


def run_mbox(db, path, email_index, dry_run, auto_publish, verbose):
    """Import from a local .mbox export (e.g. Google Takeout of the group's
    archive) — the messages keep their original To: headers, unlike a manual
    Gmail forward. Dedup is by Message-ID only (no IMAP UIDs here)."""
    import mailbox as mailbox_mod
    box = mailbox_mod.mbox(path)
    log(f"mbox {path!r}: {len(box)} message(s) to inspect.")
    imported = skipped_dup = unmatched = 0
    for key in box.keys():
        msg = box[key]  # email.message.Message
        result = handle_message(db, msg, None, f"mbox:{os.path.basename(path)}",
                                email_index, dry_run, auto_publish, verbose)
        imported += result == "staged"
        skipped_dup += result == "dup"
        unmatched += result == "unmatched"
    if not dry_run:
        db.commit()
    verb = "published" if auto_publish else "staged"
    log(f"Done. Mails {verb}: {imported} | already-imported skipped: "
        f"{skipped_dup} | no match: {unmatched}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mbox",
                        help="import from a local .mbox file (Google Takeout of "
                             "the group's archive) instead of IMAP — for the "
                             "one-off historical backfill")
    parser.add_argument("--backfill", action="store_true",
                        help="sweep the whole mailbox instead of only new UIDs")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be staged without writing anything")
    parser.add_argument("--auto-publish", action="store_true",
                        help="publish directly to the real mails table instead of "
                             "the moderation queue (also enabled by IMPORT_AUTO_PUBLISH=1)")
    parser.add_argument("--verbose", action="store_true",
                        help="print subject and recipient addresses of unmatched "
                             "messages (to diagnose why they don't match a person)")
    args = parser.parse_args()

    auto_publish = args.auto_publish or os.environ.get("IMPORT_AUTO_PUBLISH") == "1"

    db_path = os.environ.get("IMAP_DB_PATH", DEFAULT_DB)
    mailbox = os.environ.get("IMAP_MAILBOX", "INBOX")

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")
    if not args.dry_run:
        ensure_state_table(db)  # creating tables is a write — skip it in dry-run
    email_index = load_email_index(db)
    log(f"Loaded {len(email_index)} distinct person e-mail(s) from {db_path}.")
    mode = "auto-publish (real mails)" if auto_publish else "moderation queue"
    log(f"Output: {mode}.")

    # Historical backfill from a local .mbox export — no IMAP.
    if args.mbox:
        try:
            run_mbox(db, args.mbox, email_index, args.dry_run, auto_publish,
                     args.verbose)
        finally:
            db.close()
        return

    conn = connect_imap()
    try:
        last_uid = get_last_uid(db, mailbox)
        uids = fetch_uids(conn, mailbox, last_uid, args.backfill)
        log(f"Mailbox {mailbox!r}: {len(uids)} message(s) to inspect "
            f"({'backfill' if args.backfill else f'UID > {last_uid}'}).")

        imported, skipped_dup, unmatched, max_uid = 0, 0, 0, last_uid
        for uid in uids:
            status, data = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not data or data[0] is None:
                continue
            msg = email.message_from_bytes(data[0][1])
            result = handle_message(db, msg, uid, mailbox, email_index,
                                    args.dry_run, auto_publish, args.verbose)
            imported += result == "staged"
            skipped_dup += result == "dup"
            unmatched += result == "unmatched"
            max_uid = max(max_uid, uid)

        if not args.dry_run:
            set_last_uid(db, mailbox, max_uid)
            db.commit()

        verb = "published" if auto_publish else "staged"
        log(f"Done. Mails {verb}: {imported} | already-imported skipped: "
            f"{skipped_dup} | no match: {unmatched} | last UID now: "
            f"{max_uid if not args.dry_run else last_uid}.")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
