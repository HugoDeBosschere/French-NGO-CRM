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
import unicodedata
from datetime import datetime, timezone
from email.utils import getaddresses

# Reuse the building blocks already validated in the campaign importer.
from import_campaign_mails import (  # noqa: E402
    OFFICIAL_DOMAINS, _OFFICIAL_RE, body_text, decoded, ensure_state_table,
    get_last_uid, set_last_uid, load_email_index, already_imported, fetch_uids,
    mail_date_iso, log,
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
        -- Learned non-official addresses of an élu·e (cabinet, attaché, personal),
        -- discovered via thread linking, so later mails match them directly.
        CREATE TABLE IF NOT EXISTS person_emails (
            email     TEXT PRIMARY KEY,
            person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            source    TEXT,
            created_at TEXT NOT NULL
        );
        -- Full body of a member mail (internal correspondence), for reading the
        -- exchange in full in the CRM. Citizen mails are NOT stored here.
        CREATE TABLE IF NOT EXISTS mail_bodies (
            mail_id INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
            body    TEXT
        );
        -- Conversation grouping: mails sharing a thread_key are one exchange.
        CREATE TABLE IF NOT EXISTS mail_thread (
            mail_id    INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
            thread_key TEXT NOT NULL,
            message_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mail_thread_key ON mail_thread(thread_key);
        """
    )
    db.commit()


def load_email_index_with_aliases(db):
    """persons.email plus learned person_emails aliases -> [(id, name)]."""
    index = load_email_index(db)
    try:
        rows = db.execute(
            "SELECT pe.email, p.id, p.name FROM person_emails pe "
            "JOIN persons p ON p.id = pe.person_id"
        )
        for mail, pid, name in rows:
            index.setdefault(mail.strip().lower(), []).append((pid, name))
    except sqlite3.OperationalError:
        pass
    return index


def _canon(s):
    """Lower-case, strip accents, reduce every non-letter run to a single dot."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "")
                if not unicodedata.combining(c))
    return re.sub(r"\.+", ".", re.sub(r"[^a-z]+", ".", s.lower())).strip(".")


def build_name_pattern_index(db):
    """Index of élu·es for fuzzy address-local-part matching.

    Returns (persons, by_nom) where each entry is (id, name, prenom, nom) with
    `prenom` the first name and `nom` the surname tokens concatenated (both
    accent-stripped, lower-cased, letters only). `by_nom` groups them by surname.
    """
    persons, by_nom = [], {}
    for pid, name in db.execute("SELECT id, name FROM persons"):
        toks = [t for t in _canon(name).split(".") if t]
        if len(toks) < 2:
            continue
        prenom, nom = toks[0], "".join(toks[1:])
        entry = (pid, name, prenom, nom)
        persons.append(entry)
        by_nom.setdefault(nom, []).append(entry)
    return persons, by_nom


def name_pattern_match(local_part, index):
    """Return (id, name) if the address local-part maps to EXACTLY ONE élu·e by
    name, else None. Handles: prenom.nom, p.nom, a prefix of the first name +
    surname (e.g. rom.deleglise), both orders, and the same without a dot
    (romdeleglise). Requires a unique match, so homonyms are skipped, never
    guessed — and every hit is routed to moderation anyway.
    """
    persons, by_nom = index
    s = _canon(local_part)
    toks = [t for t in s.split(".") if t]
    cand = set()

    def add_prefix(pref, nom):
        if not pref:
            return
        for pid, name, prenom, pnom in by_nom.get(nom, []):
            if prenom.startswith(pref):
                cand.add((pid, name))

    if len(toks) == 2:
        a, b = toks
        add_prefix(a, b)   # <prefix-prénom>.<nom>
        add_prefix(b, a)   # <nom>.<prefix-prénom>

    joined = "".join(toks)  # no-dot concatenation, e.g. romdeleglise
    if joined:
        for pid, name, prenom, pnom in persons:
            if not pnom:
                continue
            if joined.endswith(pnom):          # <prefix-prénom><nom>
                pref = joined[:-len(pnom)]
                if pref and prenom.startswith(pref):
                    cand.add((pid, name))
            if joined.startswith(pnom):        # <nom><prefix-prénom>
                pref = joined[len(pnom):]
                if pref and prenom.startswith(pref):
                    cand.add((pid, name))

    return next(iter(cand)) if len(cand) == 1 else None


def learn_alias(db, email_addr, person_id, now):
    db.execute(
        "INSERT OR IGNORE INTO person_emails (email, person_id, source, created_at) "
        "VALUES (?, ?, 'thread', ?)",
        (email_addr.lower(), person_id, now),
    )


def _referenced_ids(msg):
    """Message-IDs this message replies to (In-Reply-To + References)."""
    raw = " ".join(msg.get_all("In-Reply-To", []) + msg.get_all("References", []))
    return set(re.findall(r"<[^>]+>", raw))


def _norm_subject(subject):
    """Subject without Re:/Fwd:/Tr: prefixes, lower-cased — thread fallback key."""
    s = re.sub(r"^\s*(re|ré|fwd?|tr|aw)\s*:\s*", "", subject or "", flags=re.I)
    while s != (s2 := re.sub(r"^\s*(re|ré|fwd?|tr|aw)\s*:\s*", "", s, flags=re.I)):
        s = s2
    return s.strip().lower()


def thread_key_of(msg):
    """Stable key grouping a message with the rest of its conversation.

    The thread root's own Message-ID: a reply carries it as the first entry of
    References (or In-Reply-To), while the root message uses its own Message-ID —
    so all messages of a thread share one key. Falls back to the normalised
    subject when no Message-ID is available.
    """
    refs = re.findall(r"<[^>]+>", " ".join(msg.get_all("References", [])))
    if refs:
        return refs[0]
    in_reply = re.findall(r"<[^>]+>", " ".join(msg.get_all("In-Reply-To", [])))
    if in_reply:
        return in_reply[0]
    own = (msg.get("Message-ID") or "").strip()
    if own:
        return own
    subj = _norm_subject(decoded(msg.get("Subject")))
    return f"subj:{subj}" if subj else None


def extract_body(msg, limit=100000):
    """Readable body text: prefer text/plain, else HTML with tags stripped."""
    plain, html = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain":
            try:
                plain.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
        elif ctype == "text/html":
            try:
                html.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    text = "\n".join(plain).strip()
    if not text and html:
        text = re.sub(r"<[^>]+>", " ", "\n".join(html))
        text = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()
    return text[:limit]


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


def classify(msg, db, email_index, name_patterns=None):
    """Work out (direction, matches, member, learn, low_confidence).

    direction 'sent'     = a member wrote to an élu·e (member in From, élu in To/Cc)
    direction 'received' = an élu·e wrote to a member (élu in From, member in To/Cc)
    Falls back to thread linking so a reply from a NON-official élu·e address is
    still attributed via In-Reply-To / References. Returns (None, [], None) when
    it isn't a clear member↔élu message.
    """
    from_pairs = addr_pairs(msg, "From")
    to_pairs = addr_pairs(msg, "To", "Cc")

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

    # 1) Address-based: resolve against the index (official emails + learned
    #    aliases), not just by domain — so a known non-official address matches.
    to_matches = resolve(a for _d, a in to_pairs)
    from_matches = resolve(a for _d, a in from_pairs)
    if from_member and to_matches:
        return "sent", to_matches, (from_member[0][1], from_member[0][0]), None, False
    if from_matches and to_member:
        return "received", from_matches, (to_member[0][1], to_member[0][0]), None, False

    # 2) Body scan: a reply usually quotes the original, which carries the élu·e's
    #    official address even when the reply's From is something else.
    if to_member and not from_member:
        body_official = {a.lower() for a in _OFFICIAL_RE.findall(body_text(msg))}
        matches = resolve(body_official)
        if matches:
            return "received", matches, (to_member[0][1], to_member[0][0]), None, False

    # 3) Thread linking: inherit the élu·e from the mail this one replies to, and
    #    LEARN the reply's non-official From address as an alias of that élu·e.
    inherited = thread_persons_lookup(db, msg)
    if inherited:
        if to_member and not from_member:          # élu·e (other address) → member
            learn = None
            non_official_from = [a for _d, a in from_pairs
                                 if not is_official(a) and not is_member(a)]
            if len(inherited) == 1 and non_official_from:
                learn = (non_official_from[0], inherited[0][0])
            return "received", inherited, (to_member[0][1], to_member[0][0]), learn, False
        if from_member:                            # member follow-up in the thread
            return "sent", inherited, (from_member[0][1], from_member[0][0]), None, False

    # 4) Name-pattern (last resort, LOW CONFIDENCE → always moderation): the other
    #    party's address local-part matches a unique élu·e name (prenom.nom / p.nom).
    #    Guards against homonyms via the ambiguity-pruned index, but the address
    #    owner may still not be the élu, so a human confirms.
    if name_patterns:
        if from_member and not to_matches:
            for _d, a in to_pairs:
                if is_member(a):
                    continue
                hit = name_pattern_match(a.split("@", 1)[0], name_patterns)
                if hit:
                    return "sent", [hit], (from_member[0][1], from_member[0][0]), None, True
        if to_member and not from_matches:
            for _d, a in from_pairs:
                if is_member(a):
                    continue
                hit = name_pattern_match(a.split("@", 1)[0], name_patterns)
                if hit:
                    return "received", [hit], (to_member[0][1], to_member[0][0]), None, True
    return None, [], None, None, False


def record(db, msg, direction, matches, member, learn, low_confidence,
           dry_run, auto_publish):
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
    if low_confidence:
        summary += " [à confirmer : élu·e identifié·e par nom]"

    # Low-confidence (name-pattern) matches always go to moderation, even in
    # auto-publish mode, so a human confirms the élu·e before it is published.
    publish = auto_publish and not low_confidence

    if dry_run:
        note = f" [+alias {learn[0]}]" if learn else ""
        note += " [MODÉRATION: nom]" if low_confidence else ""
        log(f"  [dry-run] {'publish' if publish else 'stage'} {direction} -> "
            f"{member_name} / {elu_names} | {mail_date} | {subject!r}{note}")
        return

    if learn:
        learn_alias(db, learn[0], learn[1], now)  # remember élu·e's non-official addr
    member_id = upsert_member(db, member_email, member_display, now)

    if publish:
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
        body = extract_body(msg)
        if body:
            db.execute("INSERT OR REPLACE INTO mail_bodies (mail_id, body) "
                       "VALUES (?, ?)", (mail_id, body))
        tkey = thread_key_of(msg)
        if tkey:
            db.execute("INSERT OR REPLACE INTO mail_thread (mail_id, thread_key, "
                       "message_id) VALUES (?, ?, ?)", (mail_id, tkey, message_id))
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
    # Aliases read is tolerant of the table not existing yet (e.g. dry-run first run).
    email_index = load_email_index_with_aliases(db)
    name_patterns = build_name_pattern_index(db)
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
                    direction, matches, member, learn, low_conf = classify(
                        msg, db, email_index, name_patterns)
                    if direction:
                        record(db, msg, direction, matches, member, learn,
                               low_conf, args.dry_run, auto_publish)
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
