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
import re
import sqlite3
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maildomains  # noqa: E402
# La file d'attente est définie une seule fois, dans le pont CiviCRM :
# deux définitions du même schéma finiraient par diverger.
from civicrm_lookup import ensure_civicrm_tables, enqueue  # noqa: E402
import importruns  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "meetings.db")

# The domains of the organisations we follow. Used by --match-body for forwarded
# threads (a reply a citizen forwarded) where the real address is quoted in the
# body rather than sitting in a header. These three are only the floor —
# maildomains.refresh(db) widens the set to every média (and any other) domain
# the database already knows, so journalists are covered without anyone
# maintaining a list. See utils/maildomains.py.
OFFICIAL_DOMAINS = maildomains.SEED_DOMAINS

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
    """RFC 2047-decode a header value into a plain str (never raises).

    Trois formes arrivent réellement, et seule la première est conforme :

    - `=?UTF-8?Q?S=C3=A9curit=C3=A9?=`, l'encodage MIME normal ;
    - de l'UTF-8 **brut** dans l'en-tête. `email` le rend alors sous forme
      d'objet `Header` dont le jeu de caractères est `unknown-8bit`, et le
      convertir en texte remplace chaque octet par « ? » : un objet devenait
      « s??curit?? » en base, définitivement. On décode donc les morceaux
      nous-mêmes, en essayant l'UTF-8 puis les encodages Windows historiques ;
    - un jeu de caractères annoncé mais faux, traité par le même repli.
    """
    if not value:
        return ""
    try:
        parts = decode_header(value)
    except Exception:
        return str(value)

    out = []
    for chunk, charset in parts:
        if isinstance(chunk, str):
            out.append(chunk)
            continue
        # `unknown-8bit` n'est pas un encodage : c'est l'aveu que l'expéditeur
        # n'en a déclaré aucun. À nous de deviner, dans l'ordre du plus probable.
        candidates = [charset] if charset and charset != "unknown-8bit" else []
        candidates += ["utf-8", "cp1252", "latin-1"]
        for candidate in candidates:
            try:
                out.append(chunk.decode(candidate))
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            out.append(chunk.decode("utf-8", "replace"))
    return "".join(out)


def ensure_state_table(db):
    """Tables owned by this script (kept separate from the app schema)."""
    db.executescript(
        """
        -- Last IMAP UID processed, per mailbox, for incremental runs.
        CREATE TABLE IF NOT EXISTS imported_mail_state (
            mailbox  TEXT PRIMARY KEY,
            last_uid INTEGER NOT NULL DEFAULT 0
        );
        -- Le corps des courriels, à part de `mails`. Défini ici et pas dans
        -- le pipeline membres parce que c'est lui qui dépend de ce module, et
        -- que les deux en ont besoin : deux définitions du même schéma
        -- finiraient par diverger.
        CREATE TABLE IF NOT EXISTS mail_bodies (
            mail_id INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
            body    TEXT NOT NULL
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
    """Adresse (en minuscules) -> [(id, nom)], adresses secondaires comprises.

    L'index ne lisait que `persons.email`. Or une personne écrit et reçoit
    depuis plusieurs adresses, et `person_emails` est précisément la table où
    l'import des courriels de membres et le pont CiviCRM enregistrent les
    autres. Le pipeline citoyen les ignorait : un mail adressé à l'adresse de
    cabinet d'un·e élu·e, alors que sa fiche porte l'adresse parlementaire,
    n'était rattaché à personne. Les deux pipelines lisent désormais la même
    chose.
    """
    index = {}
    for pid, name, mail in db.execute(
        "SELECT id, name, email FROM persons WHERE email IS NOT NULL AND email != ''"
    ):
        index.setdefault(mail.strip().lower(), []).append((pid, name))
    try:
        rows = db.execute(
            "SELECT pe.email, p.id, p.name FROM person_emails pe "
            "JOIN persons p ON p.id = pe.person_id")
    except sqlite3.Error:
        return index          # table absente : base neuve, rien à ajouter
    for mail, pid, name in rows:
        entries = index.setdefault((mail or "").strip().lower(), [])
        if not any(existing[0] == pid for existing in entries):
            entries.append((pid, name))
    return index


def person_by_id(db, pid):
    row = db.execute("SELECT id, name FROM persons WHERE id = ?", (pid,)).fetchone()
    return (row[0], row[1]) if row else None


def body_text(msg):
    """Concatenated text/plain + text/html payloads of a message (decoded)."""
    chunks = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                chunks.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    return "\n".join(chunks)


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


def match_recipients(msg, db, email_index, match_body=False):
    """Return the list of (person_id, name) this message is addressed to.

    Deduplicated by person_id, preserving discovery order. With `match_body`,
    also match official-domain addresses found in the body — for forwarded
    threads where the élu·e is quoted in the text, not in a header. Off by default
    so the live pipeline stays header-only (precise).
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

    # 3) Optional: official-domain addresses quoted in the body.
    if match_body:
        for addr in maildomains.find_addresses(body_text(msg)):
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


# Un objet qui commence par « Re: » (ou Rép., Ref., AW:, RE:) dit qu'il y a eu
# un aller-retour ; « TR: », « Fwd: », « Fw: » qu'on nous transfère quelque
# chose. Dans les deux cas, ce qui arrive dans la boîte de campagne n'est plus
# le premier envoi du citoyen à son élu·e, mais la suite de l'échange — le plus
# souvent parce que l'élu·e a répondu.
PREFIXE_REPONSE = re.compile(r"^\s*(re|r[ée]p|ref|aw)\s*(\[\d+\])?\s*:", re.I)
PREFIXE_TRANSFERT = re.compile(r"^\s*(tr|fwd?|wg)\s*:", re.I)


def destinataires_visibles(msg):
    """Les adresses des en-têtes To / Cc, en minuscules.

    Volontairement sans Bcc : une copie cachée ne figure pas dans le message
    reçu, et c'est précisément ce silence qui nous renseigne.
    """
    pairs = getaddresses(sum((msg.get_all(h, []) for h in ("To", "Cc")), []))
    return {(a or "").strip().lower() for _d, a in pairs if a}


def etape_echange(subject, msg=None, adresses_elus=(), notre_adresse=None):
    """« envoi » (le citoyen écrit à son élu·e) ou « reponse » (le fil revient).

    Le pipeline citoyen ne voit qu'un côté : le citoyen met PauseIA en copie
    cachée de ce qu'il envoie. Sans indice, un premier courriel et le même fil
    trois semaines plus tard, une fois l'élu·e sorti du silence, se ressemblent
    trait pour trait dans la liste.

    Deux indices, du plus sûr au moins sûr :

    1. À QUI c'est adressé. Si l'élu·e est dans To/Cc, le citoyen lui écrit et
       nous met en copie cachée : premier envoi. Si c'est NOTRE adresse qui est
       dans To/Cc et pas celle de l'élu·e, le citoyen s'adresse à nous — il
       nous transmet quelque chose, le plus souvent la réponse obtenue. C'est
       structurel : ça ne dépend ni de la langue ni du client de messagerie.
    2. L'objet, à défaut. « Re: », « TR: », « Fwd: » disent qu'il y a eu un
       aller-retour. Moins fiable — un citoyen peut se répondre à lui-même, et
       certains clients ne préfixent rien.

    Aucun des deux ne prouve que c'est l'élu·e qui a répondu. D'où un sens
    corrigeable à la main sur la fiche, et le corps conservé à côté : c'est lui
    qui tranche, et c'est un humain qui le lit.
    """
    if msg is not None:
        visibles = destinataires_visibles(msg)
        if visibles:
            elus = {a.strip().lower() for a in adresses_elus if a}
            nous = (notre_adresse or "").strip().lower()
            if elus & visibles:
                return "envoi"
            if nous and nous in visibles:
                return "reponse"

    subject = subject or ""
    if PREFIXE_TRANSFERT.match(subject):
        return "transfert"
    if PREFIXE_REPONSE.match(subject):
        return "reponse"
    return "envoi"


def stage_message(db, msg, uid, mailbox, matches, dry_run, auto_publish):
    """Record one mail (to all matched persons) and remember its Message-ID.

    Moderation mode -> one `pending_mails` draft, names joined in proposed_people.
    Auto-publish     -> one real `mails` row + a `mail_persons` link per person.
    A single citizen mail is one mail with several recipients, never duplicated.
    """
    message_id = (msg.get("Message-ID") or "").strip()
    subject = decoded(msg.get("Subject")) or "(sans objet)"
    mail_date = mail_date_iso(msg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    names = [name for _pid, name in matches]
    # Les adresses connues des élu·es destinataires : `persons.email` et les
    # alias appris au fil des échanges. C'est leur présence (ou leur absence)
    # dans To/Cc qui dit le sens.
    marques = ",".join("?" * len(matches))
    ids = [pid for pid, _name in matches]
    adresses = set()
    for table, col in (("persons", "email"), ("person_emails", "email")):
        cle = "id" if table == "persons" else "person_id"
        try:
            for (addr,) in db.execute(
                f"SELECT {col} FROM {table} WHERE {cle} IN ({marques}) "
                f"AND {col} IS NOT NULL AND {col} != ''", ids):
                adresses.add(addr.strip().lower())
        except sqlite3.Error:
            pass        # `person_emails` peut manquer sur une base ancienne
    etape = etape_echange(subject, msg, adresses, os.environ.get("IMAP_USER"))
    # « Envoyé » quand le citoyen écrit à son élu·e, « Reçu » quand le fil
    # revient — une réponse ou un transfert. C'est une déduction, pas une
    # certitude : un citoyen peut se répondre à lui-même. Elle est corrigeable
    # à la main sur la fiche du courriel, et le corps conservé permet de
    # trancher.
    sens = "sent" if etape == "envoi" else "received"
    # Le corps est conservé depuis octobre 2026. Il ne l'était pas : on gardait
    # l'objet et le nom de l'élu·e, pas le message. En pratique la liste devenait
    # illisible — impossible de distinguer un premier envoi d'un fil où l'élu·e
    # avait fini par répondre — et la minimisation était illusoire, puisque le
    # citoyen met lui-même PauseIA en copie cachée de ce qu'il écrit : c'est un
    # acte délibéré de sa part, pas une captation.
    #
    # Ce que ça change, et qu'il faut savoir : le corps porte la signature du
    # citoyen, donc son nom et parfois ses coordonnées, dans une page que lit
    # toute personne ayant le mot de passe partagé. L'en-tête From reste, lui,
    # ni lu ni stocké.
    body = extract_body(msg)
    # `summary` garde l'étiquette : c'est elle qui, dans la liste, dit d'un coup
    # d'œil qu'il s'agit d'un mail citoyen, et c'est sur elle que repose la
    # colonne « Origine » (voir _conversation_groups dans app.py). Le vrai corps
    # vit à côté, dans `mail_bodies`, comme pour les courriels membres.
    summary = f"Mail d'un citoyen à {', '.join(names)} : « {subject} »"

    if dry_run:
        mode = "publish" if auto_publish else "stage"
        log(f"  [dry-run] {mode} -> {', '.join(names)} | {mail_date} | {subject!r}")
        return

    if auto_publish:
        cur = db.execute(
            """
            INSERT INTO mails (
                mail_date, direction, important, subject, summary, follow_up_date,
                received_by, validated_by, document_stored_name,
                document_orig_name, created_at
            ) VALUES (?, ?, 0, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (mail_date, sens, subject, summary, now),
        )
        db.executemany(
            "INSERT INTO mail_persons (mail_id, person_id) VALUES (?, ?)",
            [(cur.lastrowid, pid) for pid, _name in matches],
        )
        if body:
            db.execute(
                "INSERT OR REPLACE INTO mail_bodies (mail_id, body) VALUES (?, ?)",
                (cur.lastrowid, body))
    else:
        db.execute(
            """
            INSERT INTO pending_mails (
                mail_date, direction, important, subject, summary, follow_up_date,
                proposed_people, submitted_by, document_stored_name,
                document_orig_name, created_at
            ) VALUES (?, ?, 0, ?, ?, NULL, ?, ?, NULL, NULL, ?)
            """,
            (mail_date, sens, subject, summary, ", ".join(names),
             IMPORT_SOURCE, now),
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
                   verbose, match_body=False):
    """Process one parsed message. Returns 'dup', 'staged' or 'unmatched'."""
    message_id = (msg.get("Message-ID") or "").strip()
    if already_imported(db, message_id):
        return "dup"
    matches = match_recipients(msg, db, email_index, match_body=match_body)
    if matches:
        stage_message(db, msg, uid, mailbox, matches, dry_run, auto_publish)
        return "staged"
    queued = 0
    if not dry_run:
        queued = queue_unknown_recipients(db, msg)
    if verbose:
        addrs = sorted({a.lower() for _d, a in getaddresses(
            sum((msg.get_all(h, []) for h in RECIPIENT_HEADERS), [])) if a})
        log(f"  [no match] {decoded(msg.get('Subject'))!r} "
            f"-> {', '.join(addrs) or '(no recipient header)'}")
    return "queued" if queued else "unmatched"


def queue_unknown_recipients(db, msg):
    """Mettre en file les DESTINATAIRES qu'aucune fiche ne reconnaît.

    **Seulement les destinataires, jamais l'expéditeur** : dans ce pipeline,
    l'expéditeur est le citoyen, et ce pipeline ne stocke jamais son identité.
    C'est la règle RGPD qui le fonde depuis l'origine (on enregistre qu'un·e
    élu·e a reçu un courriel et quand, pas qui l'a écrit), et elle est plus
    importante que le confort d'une file mieux remplie. D'où une fonction
    distincte de celle de l'import des membres, où les deux côtés sont
    légitimes : la symétrie serait ici une fuite.

    Le périmètre habituel s'applique (`maildomains.in_scope`) : une adresse doit
    relever d'un domaine que l'association a une raison de suivre.
    """
    try:
        ensure_civicrm_tables(db)
    except sqlite3.Error:
        return 0
    maildomains.refresh_scope(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    queued = 0
    pairs = []
    for header in RECIPIENT_HEADERS:
        pairs.extend(getaddresses(msg.get_all(header, [])))
    for display, address in pairs:
        address = (address or "").strip().lower()
        if not address or not maildomains.in_scope(address):
            continue
        if enqueue(db, address, display, now):
            queued += 1
    return queued


def iter_leaf_messages(msg):
    """Yield the real messages to process from an mbox entry.

    A Google Groups *digest* / *abridged* delivery bundles several posts into one
    email, each embedded as a `message/rfc822` part that preserves the original
    To: header. Explode those into individual messages so digest-mode history is
    matched like individual deliveries. A normal (individual) mail has no such
    part and is yielded as-is.
    """
    embedded = [
        part.get_payload(0)
        for part in msg.walk()
        if part.get_content_type() == "message/rfc822"
    ]
    if embedded:
        yield from embedded
    else:
        yield msg


def run_mbox(db, path, email_index, dry_run, auto_publish, verbose):
    """Import from a local .mbox export (e.g. Google Takeout of a mailbox that
    received the group). Individual deliveries keep the original To: header;
    digest/abridged deliveries are exploded into their embedded messages. Dedup
    is by Message-ID only (no IMAP UIDs here)."""
    import mailbox as mailbox_mod
    box = mailbox_mod.mbox(path)
    log(f"mbox {path!r}: {len(box)} mbox entrie(s) to inspect "
        "(digests are exploded into individual messages).")
    imported = skipped_dup = unmatched = queued = 0
    for key in box.keys():
        for msg in iter_leaf_messages(box[key]):
            result = handle_message(db, msg, None, f"mbox:{os.path.basename(path)}",
                                    email_index, dry_run, auto_publish, verbose)
            imported += result == "staged"
            skipped_dup += result == "dup"
            queued += result == "queued"
            unmatched += result == "unmatched"
    if not dry_run:
        db.commit()
    verb = "published" if auto_publish else "staged"
    log(f"Done. Mails {verb}: {imported} | already-imported skipped: "
        f"{skipped_dup} | queued for identification: {queued} | "
        f"no match: {unmatched}.")


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_IDX = {m: i for i, m in enumerate(_MONTHS) if m}
_PASTE_DATE = re.compile(r"\b(" + "|".join(_MONTHS[1:]) + r") (\d{1,2}), (20\d\d)")


_TOPIC_MARKER = re.compile(r"^\d+ of \d+$")


def extract_pasted_recipients(text):
    """Parse text copy-pasted from the Google Groups web view.

    Each message shows a `to <addr>` / `À : <addr>` recipient line. We keep only
    official-domain (élu·e) addresses, each tagged with the nearest preceding
    date and the current topic title as its subject. The topic title is the line
    just after a "N of M" marker; forwarded blocks carry an "Objet :" line.
    Returns an ordered list of (address, date_iso|None, subject|None).
    """
    current = None
    subject = None
    expect_subject = False
    out = []
    for line in text.splitlines():
        dm = _PASTE_DATE.search(line)
        if dm:
            current = f"{dm.group(3)}-{_MONTH_IDX[dm.group(1)]:02d}-{int(dm.group(2)):02d}"
        s = line.strip()
        low = s.lower()
        if _TOPIC_MARKER.match(s):
            expect_subject = True
            continue
        if expect_subject and s:
            subject = s
            expect_subject = False
            continue
        if low.startswith("objet :") or low.startswith("objet:"):
            subject = s.split(":", 1)[1].strip() or subject
        is_recipient = (low.startswith("to ") or s.startswith("À :")
                        or low.startswith("à :") or s.startswith("A : "))
        if is_recipient:
            for addr in maildomains.find_addresses(line):
                out.append((addr, current, subject))
    return out


def run_pasted(db, path, email_index, dry_run, auto_publish, verbose):
    """Import mails from text pasted out of the Groups web view (last resort when
    no mbox/eml export is possible). Matches the `to:` élu address, skips media,
    and skips anything already recorded for the same person on the same date
    (so it won't duplicate the mbox/IMAP imports)."""
    import hashlib
    from email.message import EmailMessage

    text = open(path, encoding="utf-8", errors="replace").read()
    entries = extract_pasted_recipients(text)
    log(f"pasted {path!r}: {len(entries)} recipient occurrence(s) with an official domain.")

    existing = set()
    try:
        for pid, d in db.execute(
            "SELECT xp.person_id, m.mail_date FROM mails m "
            "JOIN mail_persons xp ON xp.mail_id = m.id "
            "WHERE m.summary LIKE 'Mail d''un citoyen%'"
        ):
            existing.add((pid, d))
    except sqlite3.OperationalError:
        pass

    imported = dup = overlap = unmatched = 0
    seen = set()
    for idx, (addr, date_iso, subject) in enumerate(entries):
        people = email_index.get(addr, [])
        if not people:
            unmatched += 1
            if verbose:
                log(f"  [no match] {addr} ({date_iso})")
            continue
        pid, name = people[0]
        key = (pid, date_iso)
        if date_iso and (key in existing or key in seen):
            overlap += 1
            continue
        seen.add(key)
        mid = "<pasted-%s@campagne>" % hashlib.sha1(
            f"{addr}|{date_iso}|{idx}".encode()).hexdigest()[:16]
        if already_imported(db, mid):
            dup += 1
            continue
        msg = EmailMessage()
        msg["To"] = addr
        msg["Message-ID"] = mid
        msg["Subject"] = subject or "Mail campagne (import historique)"
        if date_iso:
            y, m, d = date_iso.split("-")
            msg["Date"] = f"{int(d)} {_MONTHS[int(m)]} {y} 00:00:00 +0000"
        if dry_run:
            log(f"  [dry-run] {'publish' if auto_publish else 'stage'} -> {name} | {date_iso}")
        else:
            stage_message(db, msg, None, f"pasted:{os.path.basename(path)}",
                          [(pid, name)], dry_run, auto_publish)
        imported += 1

    if not dry_run:
        db.commit()
    verb = "published" if auto_publish else "staged"
    log(f"Done. Mails {verb}: {imported} | already-imported skipped: {dup} | "
        f"overlap with existing skipped: {overlap} | no match: {unmatched}.")


def run_eml_dir(db, path, email_index, dry_run, auto_publish, verbose, match_body):
    """Import a directory of .eml files (e.g. a Takeout of individual messages).

    Digests are exploded like in the mbox path. With `match_body`, forwarded
    threads whose élu·e address sits in the body are matched too — used for the
    historical 'Fw:' exports where the recipient header is gone.
    """
    files = sorted(f for f in os.listdir(path) if f.lower().endswith(".eml"))
    log(f"eml dir {path!r}: {len(files)} .eml file(s) to inspect"
        + (" (matching body addresses)" if match_body else "") + ".")
    imported = skipped_dup = unmatched = queued = 0
    for name in files:
        with open(os.path.join(path, name), "rb") as fh:
            top = email.message_from_binary_file(fh)
        for msg in iter_leaf_messages(top):
            result = handle_message(db, msg, None, f"eml:{name}", email_index,
                                    dry_run, auto_publish, verbose, match_body)
            imported += result == "staged"
            skipped_dup += result == "dup"
            queued += result == "queued"
            unmatched += result == "unmatched"
    if not dry_run:
        db.commit()
    verb = "published" if auto_publish else "staged"
    log(f"Done. Mails {verb}: {imported} | already-imported skipped: "
        f"{skipped_dup} | queued for identification: {queued} | "
        f"no match: {unmatched}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eml-dir",
                        help="import a directory of .eml files instead of IMAP "
                             "(one-off historical backfill from a Takeout)")
    parser.add_argument("--pasted",
                        help="import from text copy-pasted out of the Groups web "
                             "view (parses the 'to: <élu>' lines; last-resort "
                             "historical backfill)")
    parser.add_argument("--match-body", action="store_true",
                        help="also match official-domain élu·e addresses quoted in "
                             "the body (for forwarded threads; use with --eml-dir)")
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
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    if not args.dry_run:
        ensure_state_table(db)  # creating tables is a write — skip it in dry-run
    email_index = load_email_index(db)
    domains = maildomains.refresh(db)
    log(f"Loaded {len(email_index)} distinct person e-mail(s) from {db_path}.")
    log(f"Known domains: {len(domains)} (derived from the fiches themselves).")
    mode = "auto-publish (real mails)" if auto_publish else "moderation queue"
    log(f"Output: {mode}.")

    # Historical backfill from local files — no IMAP.
    if args.mbox or args.eml_dir or args.pasted:
        try:
            if args.mbox:
                run_mbox(db, args.mbox, email_index, args.dry_run, auto_publish,
                         args.verbose)
            if args.eml_dir:
                run_eml_dir(db, args.eml_dir, email_index, args.dry_run,
                            auto_publish, args.verbose, args.match_body)
            if args.pasted:
                run_pasted(db, args.pasted, email_index, args.dry_run,
                           auto_publish, args.verbose)
        finally:
            db.close()
        return

    conn = connect_imap()
    tracker = importruns.track(db, "campaign_mails", enabled=not args.dry_run)
    run = tracker.__enter__()
    failure = None

    try:
        last_uid = get_last_uid(db, mailbox)
        uids = fetch_uids(conn, mailbox, last_uid, args.backfill)
        log(f"Mailbox {mailbox!r}: {len(uids)} message(s) to inspect "
            f"({'backfill' if args.backfill else f'UID > {last_uid}'}).")

        imported, skipped_dup, unmatched, queued, max_uid = 0, 0, 0, 0, last_uid
        for uid in uids:
            status, data = conn.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not data or data[0] is None:
                continue
            msg = email.message_from_bytes(data[0][1])
            result = handle_message(db, msg, uid, mailbox, email_index,
                                    args.dry_run, auto_publish, args.verbose)
            imported += result == "staged"
            skipped_dup += result == "dup"
            queued += result == "queued"
            unmatched += result == "unmatched"
            max_uid = max(max_uid, uid)

        if not args.dry_run:
            set_last_uid(db, mailbox, max_uid)
            db.commit()

        verb = "published" if auto_publish else "staged"
        run.imported, run.inspected = imported, len(uids)
        run.detail = (f"{imported} courriel(s) intégré(s), {skipped_dup} déjà "
                      f"connu(s), {queued} destinataire(s) en file, "
                      f"{unmatched} sans correspondance.")
        log(f"Done. Mails {verb}: {imported} | already-imported skipped: "
            f"{skipped_dup} | queued for identification: {queued} | "
            f"no match: {unmatched} | last UID now: "
            f"{max_uid if not args.dry_run else last_uid}.")
    except BaseException as exc:             # noqa: BLE001 — recorded, re-raised
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
