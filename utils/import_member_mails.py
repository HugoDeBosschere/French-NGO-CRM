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
    body_text, decoded, ensure_state_table, get_last_uid, set_last_uid,
    load_email_index, already_imported, fetch_uids, mail_date_iso, log,
)
# The set of domains belonging to organisations we follow — read from the data
# rather than hard-coded, so journalists' médias count too. See maildomains.py.
import maildomains  # noqa: E402
import importruns  # noqa: E402
# CiviCRM holds ~12 900 journalists this CRM does not. An address we cannot match
# is queued here rather than dropped, and civicrm_lookup.py turns it into a fiche.
from civicrm_lookup import (  # noqa: E402
    enqueue,
    ensure_civicrm_tables,
    is_bulk,
    is_generic,
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

# Commit every N messages instead of once at the very end. A --backfill sweep
# inspects thousands of messages, and a single transaction over the whole run
# holds the write lock for minutes: the Flask app cannot write meanwhile, and
# any write it does attempt first makes this script die on "database is
# locked". Short transactions keep both alive.
#
# It also makes an interrupted run resumable rather than wasted, since
# `last_uid` advances with each commit — at the cost of the all-or-nothing
# property: stopping half way now leaves the first half imported. That is safe,
# because `imported_mails` dedups by Message-ID and a rerun picks up where this
# left off.
COMMIT_EVERY = 50


def is_official(addr):
    """On a domain of an organisation we follow (a chamber, a média, …).

    Names kept as-is: this started life meaning "a parliamentary address" and is
    read that way all through classify(). It now covers média domains too, which
    is exactly what lets the same pipeline follow press correspondence.
    """
    return maildomains.is_known(addr)


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
        try:
            row = db.execute(
                "SELECT person_ids FROM thread_persons WHERE message_id = ?", (ref,)
            ).fetchone()
        except sqlite3.OperationalError:
            return []  # tables not created yet (e.g. --dry-run before first import)
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
        body_official = maildomains.find_addresses(body_text(msg))
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


def queue_unknown_counterparts(db, msg, now, enforce_scope=True):
    """Queue the outside address of a member mail we failed to match.

    Only mails with a member on exactly one side are of interest: those are
    someone from PauseIA writing to, or hearing from, a person we have no fiche
    for. Anything else (newsletters, member-to-member, robots) is left alone.
    Returns the number of addresses newly queued.
    """
    # A newsletter, a Notion notification, an Airbnb receipt: sent by a machine
    # to a list, so there is no person behind the address to look up. Cheapest
    # and broadest test, hence first.
    if is_bulk(msg):
        return 0, 0

    from_pairs = addr_pairs(msg, "From")
    to_pairs = addr_pairs(msg, "To", "Cc")
    from_member = [p for p in from_pairs if is_member(p[1])]
    to_member = [p for p in to_pairs if is_member(p[1])]

    if from_member and not to_member:
        candidates = to_pairs      # a member wrote to someone unknown
    elif to_member and not from_member:
        candidates = from_pairs    # someone unknown wrote to a member
    else:
        return 0, 0

    queued = out_of_scope = 0
    for display, address in candidates:
        # Only the member's own side is skipped. NOT is_official(): that tests
        # the *domain*, and an unknown address on a known média domain — a
        # Figaro journalist we have no fiche for — is the single best candidate
        # for a CiviCRM lookup. Skipping it is why the first full sweep queued
        # 195 addresses and not one of them sat on a press domain.
        #
        # The skip was defensible while "known" meant the three parliamentary
        # domains: every élu·e's address there was already in the index, so an
        # unmatched one was a cabinet address, which thread-based alias learning
        # handles. Once média domains joined the list, it started throwing away
        # exactly what this queue exists for.
        if is_member(address):
            continue
        # Une raison positive d'entrer, et non la seule absence de raison d'en
        # sortir : la règle Workspace copie toute la correspondance externe de
        # l'association, y compris les mails personnels des membres. Sans ce
        # test, le médecin d'un membre finissait dans une page consultable par
        # toute l'équipe. Voir maildomains.in_scope().
        # `enforce_scope=False` pour un courriel déposé à la main : quelqu'un
        # a délibérément choisi de le confier au CRM, ce qui est précisément le
        # consentement qui manque à la capture automatique. La restriction
        # protège des mails qu'on reçoit sans les avoir demandés ; elle n'a pas
        # de sens sur un dépôt volontaire.
        if enforce_scope and not maildomains.in_scope(address):
            out_of_scope += 1
            continue
        if enqueue(db, address, display, now):
            queued += 1
    return queued, out_of_scope


def record(db, msg, direction, matches, member, learn, low_confidence,
           dry_run, auto_publish):
    message_id = (msg.get("Message-ID") or "").strip()
    subject = decoded(msg.get("Subject")) or "(sans objet)"
    mail_date = mail_date_iso(msg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    member_email, member_display = member
    member_name = (member_display or "").strip() or member_email
    elu_names = ", ".join(n for _p, n in matches)
    body = extract_body(msg)
    # "Corps du texte" (the `summary` column) holds the real mail body. Fall back
    # to a generated one-liner only when the body couldn't be extracted, so the
    # field is never empty. The sender/recipients are conveyed by the direction
    # and the linked member/élu·e, not by stuffing them into the body.
    if body:
        summary = body
    elif direction == "sent":
        summary = f"Mail de {member_name} à {elu_names} : « {subject} »"
    else:
        summary = f"Mail de {elu_names} à {member_name} : « {subject} »"

    # A low-confidence (name-pattern) match is flagged in "personnes concernées"
    # for the moderator, never mixed into the body.
    proposed = elu_names
    if low_confidence:
        proposed += " (à confirmer : personne identifiée par son nom)"

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
            INSERT INTO mails (mail_date, direction, important, subject, summary,
                follow_up_date, received_by, validated_by, document_stored_name,
                document_orig_name, created_at)
            VALUES (?, ?, 0, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (mail_date, direction, subject, summary, now),
        )
        mail_id = cur.lastrowid
        db.executemany("INSERT INTO mail_persons (mail_id, person_id) VALUES (?, ?)",
                       [(mail_id, pid) for pid, _n in matches])
        db.execute("INSERT OR IGNORE INTO mail_members (mail_id, member_id) "
                   "VALUES (?, ?)", (mail_id, member_id))
        remember_thread(db, message_id, [pid for pid, _n in matches])
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
            INSERT INTO pending_mails (mail_date, direction, important, subject,
                summary, follow_up_date, proposed_people, submitted_by,
                document_stored_name, document_orig_name, created_at)
            VALUES (?, ?, 0, ?, ?, NULL, ?, ?, NULL, NULL, ?)
            """,
            (mail_date, direction, subject, summary, proposed, IMPORT_SOURCE, now),
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


def contrepartie_refusee(db, msg):
    """Vrai si une adresse externe de ce courriel est marquée « ne jamais
    enregistrer ». Le réglage vaut pour la capture automatique seule : un dépôt
    manuel est un geste délibéré, il passe outre."""
    for _display, address in addr_pairs(msg, "From") + addr_pairs(msg, "To", "Cc"):
        addr = (address or "").strip().lower()
        if not addr or is_member(addr):
            continue
        try:
            if db.execute(
                "SELECT 1 FROM civicrm_pending WHERE email = ? AND status = 'refused'",
                (addr,),
            ).fetchone():
                return addr
        except sqlite3.Error:
            return None
    return None


def apprendre_contrepartie(db, msg, person_id):
    """Rattache à `person_id` la ou les adresses externes de ce courriel.

    « Externe » = tout ce qui n'est pas une adresse de membre : c'est la
    contrepartie, celle dont on veut se souvenir. Les adresses génériques
    (contact@, no-reply@) sont écartées, elles ne désignent personne.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    vues = set()
    for _display, address in addr_pairs(msg, "From") + addr_pairs(msg, "To", "Cc"):
        addr = (address or "").strip().lower()
        if not addr or addr in vues or is_member(addr) or is_generic(addr):
            continue
        vues.add(addr)
        db.execute(
            "INSERT OR IGNORE INTO person_emails (email, person_id, source, created_at)"
            " VALUES (?, ?, 'depot', ?)", (addr, person_id, now))
    return len(vues)


def handle_one_message(db, msg, auto_publish=True, enforce_scope=False,
                       rattacher_a=None):
    """Traiter un courriel isolé, exactement comme l'import le ferait.

    Écrit pour le dépôt manuel (`/echanges/deposer`), qui doit passer par le
    même classifieur que la capture automatique : une seconde implémentation
    divergerait au premier correctif. Retourne un état lisible par l'interface :

        'imported'    rattaché à une personne et à un membre
        'duplicate'   ce Message-ID est déjà en base
        'queued'      personne reconnue, adresse mise en file « à rattacher »
        'unmatched'   ni l'un ni l'autre (aucun membre PauseIA dans le courriel)

    `rattacher_a` (identifiant de fiche) répond au cas le plus courant du dépôt
    manuel : le journaliste qui écrit depuis son gmail. Sans lui, un dépôt dont
    l'adresse est inconnue repartait en file d'attente et LE COURRIEL N'ÉTAIT PAS
    ENREGISTRÉ — alors que la personne qui dépose sait très bien de qui il
    s'agit. Avec lui, l'adresse est apprise AVANT le classement, donc le
    classifieur la reconnaît, le courriel entre, et tous les suivants de cette
    adresse entreront seuls. Une validation humaine ne devrait jamais avoir à
    être refaite.

    L'appelant est responsable du commit : un dépôt de dix fichiers est une
    seule transaction, ou rien.
    """
    # `imported_mails` (le dédoublonnage par Message-ID) vient des scripts, pas
    # de init_db() : sans elle, un dépôt échoue sur une base que seul l'app a
    # créée. Les trois sont idempotentes.
    ensure_state_table(db)
    ensure_member_tables(db)
    ensure_civicrm_tables(db)
    maildomains.refresh(db)
    maildomains.refresh_scope(db)

    message_id = (msg.get("Message-ID") or "").strip()
    if message_id and db.execute(
            "SELECT 1 FROM imported_mails WHERE message_id = ?",
            (message_id,)).fetchone() is not None:
        return "duplicate", decoded(msg.get("Subject")) or "(sans objet)"

    subject = decoded(msg.get("Subject")) or "(sans objet)"
    if rattacher_a:
        apprendre_contrepartie(db, msg, int(rattacher_a))
    email_index = load_email_index_with_aliases(db)
    name_patterns = build_name_pattern_index(db)
    direction, matches, member, learn, low_conf = classify(
        msg, db, email_index, name_patterns)
    if direction:
        record(db, msg, direction, matches, member, learn, low_conf,
               dry_run=False, auto_publish=auto_publish)
        return "imported", subject

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    queued, _ignored = queue_unknown_counterparts(
        db, msg, now, enforce_scope=enforce_scope)
    return ("queued" if queued else "unmatched"), subject


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
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    if not args.dry_run:
        ensure_state_table(db)
        ensure_member_tables(db)
        ensure_civicrm_tables(db)
    # Aliases read is tolerant of the table not existing yet (e.g. dry-run first run).
    email_index = load_email_index_with_aliases(db)
    name_patterns = build_name_pattern_index(db)
    domains = maildomains.refresh(db)
    scope = maildomains.refresh_scope(db)
    log(f"Known domains: {len(domains)} (from the fiches themselves). "
        f"Queue scope: {len(scope)} domain(s) + public institutions.")
    log(f"Loaded {len(email_index)} élu·e e-mail(s) from {db_path}. "
        f"Output: {'auto-publish' if auto_publish else 'moderation queue'}.")

    # One row per run, so /echanges can say how fresh the data is — and say so
    # loudly when a run fails. Started before connect_imap(): an expired
    # password is exactly the failure that must leave a trace.
    tracker = importruns.track(db, "member_mails", enabled=not args.dry_run)
    run = tracker.__enter__()
    failure = None

    conn = connect_imap()
    try:
        last_uid = get_last_uid(db, "members")
        uids = fetch_uids(conn, mailbox, last_uid, args.backfill)
        log(f"Audit mailbox {mailbox!r}: {len(uids)} message(s) to inspect.")
        imported = dup = skipped = queued = out_of_scope = refuses = max_uid = 0
        max_uid = last_uid
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for index, uid in enumerate(uids):
            status, data = conn.uid("fetch", str(uid), "(RFC822)")
            if status == "OK" and data and data[0]:
                msg = email.message_from_bytes(data[0][1])
                mid = (msg.get("Message-ID") or "").strip()
                if already_imported(db, mid):
                    dup += 1
                else:
                    direction, matches, member, learn, low_conf = classify(
                        msg, db, email_index, name_patterns)
                    refusee = contrepartie_refusee(db, msg)
                    if direction and refusee:
                        # « Ne jamais enregistrer » : on s'arrête ici, mais on
                        # note le Message-ID, sinon chaque balayage réexaminerait
                        # le même courriel jusqu'à la fin des temps.
                        if not args.dry_run and mid:
                            db.execute(
                                "INSERT OR IGNORE INTO imported_mails "
                                "(message_id, uid, mailbox, imported_at) "
                                "VALUES (?, ?, ?, ?)",
                                (mid, None, mailbox, now))
                        refuses += 1
                        if args.verbose:
                            log(f"  [refusé] {refusee} — "
                                f"{decoded(msg.get('Subject'))!r}")
                    elif direction:
                        record(db, msg, direction, matches, member, learn,
                               low_conf, args.dry_run, auto_publish)
                        imported += 1
                    else:
                        skipped += 1
                        if not args.dry_run:
                            new_queued, ignored = queue_unknown_counterparts(
                                db, msg, now)
                            queued += new_queued
                            out_of_scope += ignored
                        if args.verbose:
                            log(f"  [skip] {decoded(msg.get('Subject'))!r}")
            max_uid = max(max_uid, uid)
            if not args.dry_run and (index + 1) % COMMIT_EVERY == 0:
                # UIDs come in ascending order, so recording the highest one
                # seen is an honest resume point.
                set_last_uid(db, "members", max_uid)
                db.commit()

        if not args.dry_run:
            set_last_uid(db, "members", max_uid)
            db.commit()
        verb = "published" if auto_publish else "staged"
        run.imported, run.inspected = imported, len(uids)
        run.detail = (f"{imported} courriel(s) intégré(s), {dup} déjà connu(s), "
                      f"{skipped} non rattaché(s), {queued} adresse(s) en file.")
        log(f"Done. Mails {verb}: {imported} | already-imported skipped: {dup} | "
            f"not member↔élu: {skipped} | queued for CiviCRM: {queued} | "
            f"outside the queue's scope (personal mail, suppliers): {out_of_scope} | "
            f"refused addresses: {refuses} | "
            f"last UID now: {max_uid if not args.dry_run else last_uid}.")
    except BaseException as exc:             # noqa: BLE001 — recorded, re-raised
        # Including KeyboardInterrupt: a run cut short did not finish its sweep,
        # and the interface must not present it as a healthy one.
        failure = exc
        run.detail = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        tracker.__exit__(type(failure) if failure else None, failure, None)
        try:
            conn.logout()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
