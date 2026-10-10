#!/usr/bin/env python3
"""Resolve unknown mail addresses against CiviCRM — the on-demand half of the sync.

CiviCRM holds ~12 900 journalists. Copying them here would break the tool: the
rencontre and courriel forms render one <option> *and* one checkbox per person,
so a 17 000-row `persons` table turns the main data-entry screens into
multi-megabyte pages. And a journal of interactions has no use for 12 900 people
nobody has ever written to.

So a journalist earns a fiche the day a member actually exchanges mail with them:

  1. import_member_mails.py meets an address it cannot match and, instead of
     dropping it, records it in `civicrm_pending` (see enqueue below).
  2. This script prints those addresses (--list-pending); the host looks them up
     with `cv api4` — see utils/deploy/civicrm-sync.sh.
  3. This script reads the answer (--apply) and creates the fiche, with its
     média and its alignement.
  4. import_member_mails.py --backfill runs again; the address now matches, and
     the mail is recorded like any other. `imported_mails` keeps that sweep from
     duplicating anything.

An address CiviCRM doesn't know is marked 'absent' so it isn't looked up again
every night; --retry-absent puts them back in the queue after CiviCRM has been
enriched.

    python3 utils/civicrm_lookup.py --list-pending
    python3 utils/civicrm_lookup.py --apply civi-contacts.json --commit
    python3 utils/civicrm_lookup.py --stats
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from civicrm import (  # noqa: E402
    CONTACT_FIELDS, CONTACT_FIELDS_OPTIONAL, ContractError, assert_contract,
    clean_email, looks_like_an_address,
    contact_to_person, norm_name,
)
from import_civicrm_medias import link_person_media, load_media_index  # noqa: E402
import maildomains  # noqa: E402
import mailpatterns  # noqa: E402
import learn_conventions  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))

# Addresses never worth asking CiviCRM about: shared desks and robots. They
# belong to a newsroom or a machine, not a person, so a fiche would be
# meaningless. Everything below was drawn from what the audit mailbox actually
# produced on the first real run (notify@mail.notion.so, automated@airbnb.com,
# bonjour@fresquedesrisquesdelia.org …).
GENERIC_LOCALPARTS = {
    # newsroom desks
    "contact", "redaction", "info", "infos", "presse", "press", "contactez-nous",
    "abonnement", "abonnements", "service-client", "newsletter", "courrier",
    "lecteurs", "moderation", "webmaster", "admin", "support", "bonjour",
    "hello", "team", "equipe", "communication", "secretariat", "accueil",
    # robots and transactional senders
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "ne-pas-repondre", "nepasrepondre", "mailer-daemon", "postmaster", "mailer",
    "notify", "notifications", "notification", "automated", "automatic", "auto",
    "alerts", "alert", "news", "updates", "update", "bounce", "bounces",
    "reply", "noreponse", "systeme", "system", "root", "daemon", "billing",
    "facture", "facturation", "invoice", "receipt", "confirmation",
}

# Substrings that make a local part a robot wherever they sit inside it.
# GENERIC_LOCALPARTS matches the whole local part, which let
# `drive-shares-dm-noreply@google.com` through twenty times.
ROBOT_FRAGMENTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "nepasrepondre", "ne-pas-repondre", "mailer-daemon", "bounce",
    "notification", "unsubscribe", "desabonnement",
)

# Subdomains email service providers send bulk from. A person's address almost
# never sits on one; a newsletter's very often does —
# `laredoute@news.laredoute.fr` is what prompted this.
BULK_SUBDOMAINS = (
    "news", "newsletter", "newsletters", "mailing", "mailings", "emailing",
    "email", "emails", "mail", "envoi", "envois", "bounce", "bounces",
    "noreply", "no-reply", "reply", "notifications", "notify", "alerts",
    "marketing", "campaign", "campaigns", "sendgrid", "mailgun", "mandrill",
)

# Our own properties. A mail to or from one of these is internal — the Fresque
# site, the association's own domains — never a contact to file. pauseia.fr is
# already excluded as the members' domain; this covers the rest. Comma-separated
# in CRM_OWN_DOMAINS to add one without touching the code.
OWN_DOMAINS = frozenset(
    d.strip().lower()
    for d in os.environ.get(
        "CRM_OWN_DOMAINS",
        "pauseia.fr,fresquedesrisquesdelia.org,pauseai.info").split(",")
    if d.strip()
)

# Headers that mean "a machine sent this to a list", in order of how standard
# they are. A newsletter, a Notion notification or an Airbnb receipt carries at
# least one; a person writing to a member carries none. This is the filter that
# actually holds — a word list will always lag behind the next SaaS robot.
BULK_HEADERS = (
    "List-Unsubscribe", "List-Id", "List-Post", "Feedback-ID",
    "X-Auto-Response-Suppress", "X-Mailer-Daemon", "X-Campaign-Id",
)


def log(msg):
    print(msg, flush=True)


def note(msg):
    """A human-facing line from a command whose stdout is machine-read.

    `--patterns` writes JSON that civicrm-sync.sh parses. Its summary went to
    stdout too, which made that JSON unparseable — and the caller's `|| echo {}`
    swallowed the failure, so step 4b silently reported "no convention applies"
    for weeks. Anything printed alongside machine output belongs on stderr.
    """
    print(msg, file=sys.stderr, flush=True)


def ensure_civicrm_tables(db):
    """The lookup queue. Created by whoever gets there first (import or resolve)."""
    db.executescript(
        """
        -- One row per external address the mail import could not match. 'pending'
        -- is waiting for a CiviCRM lookup, 'resolved' got a fiche, 'absent' means
        -- CiviCRM doesn't know it either (kept, so we stop asking every night).
        -- 'ignored' is set from /echanges/a-rattacher when a human says this is
        -- no one we follow — a robot, a supplier, a personal mail. No script
        -- here ever sets or revisits it: --retry-absent touches 'absent' only,
        -- and enqueue() updates the counters without ever rewriting the status,
        -- so a human decision is never undone by the nightly sweep.
        CREATE TABLE IF NOT EXISTS civicrm_pending (
            email       TEXT PRIMARY KEY,
            display     TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            seen_count  INTEGER NOT NULL DEFAULT 1,
            status      TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT,
            person_id   INTEGER REFERENCES persons(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_civicrm_pending_status
            ON civicrm_pending(status);
        -- Same definition as in import_member_mails.ensure_member_tables: a
        -- person's other addresses, so they match directly next time. Created
        -- here too because the CiviCRM sync may well run before the mail import
        -- ever has on a fresh database.
        CREATE TABLE IF NOT EXISTS person_emails (
            email      TEXT PRIMARY KEY,
            person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            source     TEXT,
            created_at TEXT NOT NULL
        );
        """
    )


def is_generic(address):
    """True for a newsroom desk, a robot, or one of our own domains.

    Three reasons an address is not worth a fiche, and none of them is about
    CiviCRM: nobody is behind it, it is a machine, or it is us.
    """
    address = (address or "").strip().lower()
    if "@" not in address:
        return True
    local, _, domain = address.partition("@")
    local = local.split("+", 1)[0]
    if domain in OWN_DOMAINS:
        return True
    # mail.notion.so, email.airbnb.com, e.sendgrid.net…: robots live on a
    # subdomain of the service as often as on its apex.
    if any(domain == d or domain.endswith("." + d) for d in OWN_DOMAINS):
        return True
    if local in GENERIC_LOCALPARTS:
        return True
    if any(fragment in local for fragment in ROBOT_FRAGMENTS):
        return True
    # news.laredoute.fr, email.airbnb.com…: the first label of a multi-part
    # domain naming a sending platform rather than an organisation.
    labels = domain.split(".")
    return len(labels) > 2 and labels[0] in BULK_SUBDOMAINS


def is_bulk(msg):
    """True when the message was sent by a machine to a list, not by a person.

    Checked before anything else: it catches the next SaaS notifier without
    anyone adding a word to GENERIC_LOCALPARTS.
    """
    for header in BULK_HEADERS:
        if msg.get(header):
            return True
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk"):
        return True
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    return bool(auto) and auto != "no"


def enqueue(db, address, display, now):
    """Record an unmatched external address for the next CiviCRM lookup.

    Called by import_member_mails.py. Never raises: a queueing problem must not
    cost us the mail import itself.
    """
    address = clean_email(address)
    if not address or is_generic(address):
        return False
    # Une adresse qui a déjà sa fiche n'a rien à faire dans la file : la file
    # sert à dire « de qui s'agit-il ? », et la réponse existe déjà. Le cas
    # vient de l'élargissement aux corps de courriels : un citoyen qui écrit à
    # un·e élu·e emploie forcément les mots « député » ou « sénateur », donc le
    # même échange arrivait une seconde fois dans « à rattacher » alors qu'il
    # était déjà enregistré sous son nom. Le courriel, lui, est déjà protégé :
    # la mise en file n'a lieu que lorsque le classement a échoué. Ici c'est
    # l'ADRESSE qu'on dédoublonne — `persons.email` et les alias appris dans
    # `person_emails`.
    try:
        if db.execute(
            "SELECT 1 FROM person_emails WHERE LOWER(TRIM(email)) = ? "
            "UNION ALL "
            "SELECT 1 FROM persons WHERE LOWER(TRIM(email)) = ? LIMIT 1",
            (address, address),
        ).fetchone():
            return False
    except sqlite3.Error:
        # person_emails n'existe pas encore sur une base jamais ouverte par
        # l'application : rien à dédoublonner, on continue.
        pass
    # « Ne jamais enregistrer » : l'adresse ne revient plus en file et ses
    # courriels n'entrent plus tout seuls. Un dépôt manuel reste possible,
    # c'est un geste délibéré — voir handle_one_message(rattacher_a=…).
    try:
        if db.execute(
            "SELECT 1 FROM civicrm_pending WHERE email = ? AND status = 'refused'",
            (address,),
        ).fetchone():
            return False
    except sqlite3.Error:
        pass
    display = (display or "").strip() or None
    try:
        # `rowcount` can't tell an insert from an ON CONFLICT update — both report
        # one row — and the caller counts genuinely new addresses, so ask first.
        seen = db.execute(
            "SELECT 1 FROM civicrm_pending WHERE email = ?", (address,)
        ).fetchone() is not None
        if seen:
            db.execute(
                """
                UPDATE civicrm_pending
                   SET last_seen  = ?,
                       seen_count = seen_count + 1,
                       display    = COALESCE(NULLIF(display, ''), ?)
                 WHERE email = ?
                """,
                (now, display, address),
            )
            return False
        db.execute(
            "INSERT INTO civicrm_pending (email, display, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            (address, display, now, now),
        )
        return True
    except sqlite3.Error:
        return False


def load_person_index(db):
    """(folded name, contact_type) -> person id, to attach rather than duplicate.

    The 133 journalists seeded from the old press CRM have a name but no address.
    When CiviCRM hands us the same person, we fill that fiche in instead of
    creating a second one — the same "match by name within a type" rule the
    insert_* scripts follow.
    """
    index = {}
    for pid, name, ctype in db.execute(
        "SELECT id, name, contact_type FROM persons"
    ):
        index.setdefault((norm_name(name), ctype), pid)
    return index


def existing_emails(db):
    out = set()
    for (mail,) in db.execute(
        "SELECT email FROM persons WHERE email IS NOT NULL AND email != ''"
    ):
        out.add(mail.strip().lower())
    return out


def create_or_attach(db, row, now, person_index, media_index):
    """Give a CiviCRM contact a fiche here. Returns (person_id, action).

    action is 'attached' when an existing fiche gained the address, 'created'
    otherwise. An existing address is never overwritten: a hand-typed one wins.
    """
    key = (norm_name(row["name"]), row["contact_type"])
    person_id = person_index.get(key)
    if person_id is not None:
        # Fill the blanks only. The 133 fiches seeded from the old press CRM have
        # a name and nothing else, so they gain everything; a fiche a moderator
        # actually filled in keeps every value they typed. `stance` counts as
        # blank while it is still the default 'Inconnu' — nobody chose that.
        db.execute(
            """
            UPDATE persons
               SET email        = CASE WHEN COALESCE(TRIM(email), '') = ''
                                       THEN ? ELSE email END,
                   social_links = CASE WHEN COALESCE(TRIM(social_links), '') = ''
                                       THEN ? ELSE social_links END,
                   stance       = CASE WHEN stance = 'Inconnu' AND ? != 'Inconnu'
                                       THEN ? ELSE stance END,
                   notes        = CASE WHEN COALESCE(TRIM(notes), '') = ''
                                       THEN ? ELSE notes END
             WHERE id = ?
            """,
            (row["email"], row["social_links"], row["stance"], row["stance"],
             row["notes"], person_id),
        )
        action = "attached"
    else:
        person_id = db.execute(
            """
            INSERT INTO persons (name, contact_type, stance, email, social_links,
                notes, added_by, validated_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (row["name"], row["contact_type"], row["stance"], row["email"],
             row["social_links"], row["notes"], now),
        ).lastrowid
        person_index[key] = person_id
        action = "created"
    # CiviCRM holds ~1.6 addresses per journalist (21 401 for 12 987 contacts),
    # so the address a member wrote to is often not the fiche's. Keep it as an
    # alias, in the very table the mail import resolves against, or the next mail
    # from it would be queued all over again.
    current = db.execute("SELECT email FROM persons WHERE id = ?",
                         (person_id,)).fetchone()
    if row["email"] and current and (current[0] or "").strip().lower() != row["email"]:
        db.execute(
            "INSERT OR IGNORE INTO person_emails (email, person_id, source, "
            "created_at) VALUES (?, ?, 'civicrm', ?)",
            (row["email"], person_id, now),
        )
    if row["media_name"]:
        link_person_media(db, person_id, row["media_name"], now, media_index)
    return person_id, action


def cmd_list_pending(db, args):
    rows = db.execute(
        "SELECT email FROM civicrm_pending WHERE status = 'pending' "
        "ORDER BY seen_count DESC, first_seen LIMIT ?",
        (args.limit,),
    ).fetchall()
    for (email,) in rows:
        print(email)
    return 0


def cmd_stats(db, _args):
    log("File d'attente CiviCRM :")
    for status, total in db.execute(
        "SELECT status, COUNT(*) FROM civicrm_pending GROUP BY status ORDER BY status"
    ):
        log(f"  {status:<10} {total}")
    return 0


def cmd_retry_absent(db, args):
    cur = db.execute(
        "UPDATE civicrm_pending SET status = 'pending', resolved_at = NULL "
        "WHERE status = 'absent'"
    )
    if args.commit:
        db.commit()
        log(f"{cur.rowcount} adresse(s) remise(s) en file.")
    else:
        log(f"[dry-run] {cur.rowcount} adresse(s) seraient remises en file.")
    return 0


# Above this, a seed stops being a seed. The rencontre and courriel forms render
# one <option> *and* one checkbox per person, so a few hundred fiches is a
# working tool and several thousand is an unusable one. --force is there for a
# deliberate choice, never for a slip.
SEED_SOFT_CAP = 1500


def cmd_seed(db, args):
    """Create fiches for a whole CiviCRM group, to get the cycle started.

    The on-demand path needs something to start from. An address arriving in the
    audit mailbox is only resolvable against fiches that exist, and a média's
    address convention is only learnable from addresses already held — so with
    an empty press half, every journalist mail is queued and none is ever
    attributed. Seeding one narrow group breaks that.

    (It is NOT needed to make Workspace copy press mail: the rule matching
    `@pauseia.fr` over full headers already copies the whole correspondence. An
    earlier version of this docstring said otherwise.)

    Narrow is the point. This is not the bulk import we deliberately did not do.
    """
    with open(args.seed, encoding="utf-8") as fh:
        records = json.load(fh)
    try:
        assert_contract(records, CONTACT_FIELDS, "contact",
                        optional=CONTACT_FIELDS_OPTIONAL, warn=log)
    except ContractError as exc:
        log(f"ABANDON — contrat CiviCRM non respecté : {exc}")
        return 1

    usable = [r for r in records if clean_email(r.get("email_primary.email"))]
    log(f"{len(records)} contact(s) dans l'export, {len(usable)} avec une adresse.")
    if len(usable) > SEED_SOFT_CAP and not args.force:
        log(f"ABANDON — {len(usable)} fiches dépassent le plafond de "
            f"{SEED_SOFT_CAP}. Un amorçage doit rester étroit : au-delà, les "
            f"sélecteurs de personnes des formulaires deviennent inutilisables. "
            f"Choisissez un groupe plus restreint, ou --force si c'est voulu.")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now[:10]
    person_index = load_person_index(db)
    media_index = load_media_index(db)
    known = existing_emails(db)

    created = attached = skipped = desks = 0
    for record in usable:
        row = contact_to_person(record, today)
        if row is None:
            # Either nameless, or a newsroom desk CiviCRM records as a contact
            # ("debats@lefigaro.fr"). Neither deserves a fiche.
            if looks_like_an_address(record.get("display_name") or ""):
                desks += 1
            else:
                skipped += 1
            continue
        if row["email"] in known:
            skipped += 1
            continue
        key = (norm_name(row["name"]), row["contact_type"])
        if args.commit:
            _pid, action = create_or_attach(db, row, now, person_index, media_index)
        else:
            # Mirror what --commit would do, including for a name appearing twice
            # in the same export: Pierre Dandumont writes for MacGeneration *and*
            # iGeneration, so the second row attaches a média to one fiche rather
            # than creating a second. Without seeding the index here, the dry run
            # promises more fiches than the real run creates.
            action = "attached" if key in person_index else "created"
            person_index[key] = -1
        known.add(row["email"])
        if action == "created":
            created += 1
        else:
            attached += 1
        if args.verbose:
            media = f" — {row['media_name']}" if row["media_name"] else ""
            log(f"  {'+' if action == 'created' else '~'} {row['name']}{media}")

    if args.commit:
        db.commit()
    prefix = "" if args.commit else "[dry-run] "
    log(f"{prefix}Amorçage. Fiches créées : {created} | fiches complétées : "
        f"{attached} | déjà connues ou sans nom : {skipped} | adresses de "
        f"rédaction écartées : {desks}.")
    if args.commit and (created or attached):
        log("Les domaines désormais reconnus (scan du corps des mails, mise en "
            "file) :")
        log("  python3 utils/maildomains.py --list")
    return 0


def domain_conventions(db):
    """{domain: {"media": name, "template": how addresses are built there}}.

    Two sources, in this order of trust:

    1. `mail_conventions`, learned by `learn_conventions.py` from *all* of
       CiviCRM's journalists — hundreds of médias, each convention backed by
       dozens of addresses. This is what makes `cbouchouchi@nouvelobs.com` or
       `emmanuel.pall@francetv.fr` recognisable: those newsrooms have one fiche
       here, far too few to deduce anything from, but hundreds in CiviCRM.
    2. The fiches we hold ourselves, which fill the gaps — a média CiviCRM has
       nothing on, or a domain we learned from a mail thread.

    A domain appears only when its convention is unambiguous AND we know which
    média it belongs to, since resolving needs both: the template to rebuild an
    address, the média to know whose names to compare it against.
    """
    pairs, domain_media = [], {}
    for name, mail, media in db.execute(
        """
        SELECT p.name, p.email, o.name
          FROM persons p
          LEFT JOIN person_organisations po ON po.person_id = p.id
          LEFT JOIN organisations o ON o.id = po.organisation_id
                                   AND o.org_type = 'Média'
         WHERE p.email IS NOT NULL AND p.email != ''
        """
    ):
        pairs.append((name, mail))
        domain = (mail or "").strip().lower().partition("@")[2]
        if domain and media:
            domain_media.setdefault(domain, {}).setdefault(media, 0)
            domain_media[domain][media] += 1

    out = {}
    for domain, template in mailpatterns.learn(pairs).items():
        medias = domain_media.get(domain)
        if not medias:
            continue
        # A domain can carry a couple of mislinked fiches; the média most of
        # them point at is the one that owns it.
        best = max(medias.items(), key=lambda kv: kv[1])[0]
        out[domain] = {"media": best, "template": template}

    # CiviCRM-wide learning wins where the two disagree: more examples.
    out.update(learn_conventions.load_conventions(db))
    return out


def cmd_patterns(db, args):
    """Print the address conventions learned from the fiches.

    By default, only those needed for the addresses still waiting — that is
    what civicrm-sync.sh feeds back into `cv api4 Contact.get` to fetch those
    journalists by name. With --all, every convention, which is what you want
    when reviewing a seed: an empty queue would otherwise print `{}` and hide
    the forty-odd conventions that were in fact learned.
    """
    conventions = domain_conventions(db)
    if args.all:
        print(json.dumps(conventions, ensure_ascii=False, indent=2,
                         sort_keys=True))
        by_template = {}
        for entry in conventions.values():
            by_template[entry["template"]] = by_template.get(entry["template"], 0) + 1
        note(f"{len(conventions)} convention(s) apprise(s) : "
             + ", ".join(f"{n}× {t}" for t, n in
                         sorted(by_template.items(), key=lambda kv: -kv[1])))
        return 0

    wanted = {}
    for (address,) in db.execute(
        "SELECT email FROM civicrm_pending WHERE status IN ('pending', 'absent')"
    ):
        domain = (address or "").partition("@")[2]
        if domain in conventions:
            wanted[domain] = conventions[domain]
    print(json.dumps(wanted, ensure_ascii=False, indent=2))
    note(f"{len(wanted)} domaine(s) utile(s) aux adresses en attente, sur "
         f"{len(conventions)} convention(s) apprise(s). "
         f"Utilisez --patterns --all pour les voir toutes.")
    return 0


def cmd_apply_names(db, args):
    """Resolve addresses by convention, against journalists CiviCRM knows by name.

    The last resort, and the only step that identifies someone from something
    other than their actual address. A convention is a habit rather than a rule,
    so every fiche it produces says so in its notes — and an address two
    journalists could both own is refused, never split.
    """
    with open(args.apply_names, encoding="utf-8") as fh:
        records = json.load(fh)

    conventions = domain_conventions(db)
    # Candidates are indexed by the domain of their own address FIRST, and by
    # their employer only as a fallback. Grouping by média was the original
    # design and it under-reached badly: CiviCRM labels employers regionally, so
    # francetv.fr comes out as "FRANCE 3 PARIS ILE-DE-FRANCE" and only that one
    # newsroom's journalists would have been compared — a few dozen out of the
    # 2 055 people writing from francetv.fr. The domain is what is stable.
    by_domain, by_media = {}, {}
    for record in records:
        name = (record.get("display_name") or "").strip()
        if not name:
            continue
        entry = (record.get("id"), name, record)
        mail = clean_email(record.get("email_primary.email"))
        domain = mail.rpartition("@")[2] if mail else None
        if domain:
            by_domain.setdefault(domain, []).append(entry)
        media = (record.get("employer_id.display_name") or "").strip()
        if media:
            by_media.setdefault(norm_name(media), []).append(entry)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now[:10]
    person_index = load_person_index(db)
    media_index = load_media_index(db)

    pending = [r[0] for r in db.execute(
        "SELECT email FROM civicrm_pending WHERE status IN ('pending', 'absent')")]
    resolved = ambiguous = unmatched = untouched = 0
    for address in pending:
        domain = address.partition("@")[2]
        convention = conventions.get(domain)
        if not convention:
            untouched += 1
            continue
        # Both pools: people writing from that domain, plus people CiviCRM
        # attributes to the média but whose own address is elsewhere (a
        # freelance on gmail). Deduplicated on the CiviCRM contact id.
        candidates, seen = [], set()
        for entry in (by_domain.get(domain, [])
                      + by_media.get(norm_name(convention["media"] or ""), [])):
            if entry[0] in seen:
                continue
            seen.add(entry[0])
            candidates.append(entry)
        hits = mailpatterns.resolve_all(
            address, convention["template"], [(c[0], c[1]) for c in candidates])
        # Ambiguity is a matter of NAMES, not of CiviCRM ids. CiviCRM carries
        # duplicate contacts for the same journalist — `pierre.debaudouin@
        # francetv.fr` matched five records all reading "Pierre de Baudouin" —
        # and refusing to choose between someone and themself would leave every
        # duplicated journalist unresolvable forever. Collapse on the name; the
        # first record wins, and create_or_attach dedups on the name again.
        by_person = {}
        for cid, display in hits:
            by_person.setdefault(norm_name(display), (cid, display))
        if len(by_person) > 1:
            # A real collision — two different journalists whose names build the
            # same address. Worth a human eye, unlike a plain miss.
            ambiguous += 1
            names = ", ".join(n for _i, n in by_person.values())
            log(f"  ! {address} : {names} — impossible de trancher, laissée en file")
            continue
        hits = list(by_person.values())
        if not hits:
            unmatched += 1
            continue
        hit = hits[0]
        record = next(c[2] for c in candidates if c[0] == hit[0])
        row = contact_to_person(record, today)
        if row is None:
            untouched += 1
            continue
        row["email"] = address
        # The DOMAIN is what the convention was learned from, so it is what the
        # note names. Quoting convention['media'] read wrong: it is the label
        # most of the domain's addresses carry ("FRANCE 3 PARIS ILE-DE-FRANCE"),
        # while this journalist's own fiche says FRANCE 3 HAUTS DE FRANCE.
        row["notes"] += (
            f"\nAdresse reconnue par la convention de {domain} "
            f"(« {convention['template']} ») : à confirmer.")
        if args.commit:
            person_id, _action = create_or_attach(
                db, row, now, person_index, media_index)
            db.execute(
                "UPDATE civicrm_pending SET status = 'resolved', resolved_at = ?, "
                "person_id = ? WHERE email = ?", (now, person_id, address))
        resolved += 1
        log(f"  ? {row['name']} — {row.get('media_name') or domain} <{address}> "
            f"[motif {convention['template']} sur {domain}, à confirmer]")

    if args.commit:
        db.commit()
    prefix = "" if args.commit else "[dry-run] "
    log(f"{prefix}Par convention. Reconnues : {resolved} | ambiguës (plusieurs "
        f"journalistes possibles) : {ambiguous} | aucun nom ne correspond : "
        f"{unmatched} | sans convention applicable : {untouched}.")
    return 0


def cmd_report(db, args):
    """What the last import actually produced — short enough to read.

    A --backfill sweep can inspect thousands of messages, and --verbose prints a
    line per skipped one. This shows the outcome instead: the press mail that
    got attached, what the queue holds, and a sample of what could not be
    attributed, so the sample is representative rather than the first N.
    """
    limit = args.limit

    log("== Contacts ==")
    for ctype, total, with_mail in db.execute(
        """
        SELECT contact_type, COUNT(*),
               SUM(CASE WHEN COALESCE(TRIM(email), '') != '' THEN 1 ELSE 0 END)
          FROM persons GROUP BY contact_type ORDER BY COUNT(*) DESC
        """
    ):
        log(f"  {ctype:<24} {total:>5}   dont {with_mail or 0} avec adresse")

    log("\n== Courriels rattachés à un·e journaliste ==")
    rows = db.execute(
        """
        SELECT m.mail_date, m.direction, p.name,
               COALESCE(o.name, '—'), COALESCE(m.subject, '(sans objet)')
          FROM mails m
          JOIN mail_persons mp ON mp.mail_id = m.id
          JOIN persons p ON p.id = mp.person_id AND p.contact_type = 'Journaliste'
          LEFT JOIN person_organisations po ON po.person_id = p.id
          LEFT JOIN organisations o ON o.id = po.organisation_id
                                   AND o.org_type = 'Média'
         GROUP BY m.id
         ORDER BY m.mail_date DESC, m.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    total_press = db.execute(
        "SELECT COUNT(DISTINCT m.id) FROM mails m "
        "JOIN mail_persons mp ON mp.mail_id = m.id "
        "JOIN persons p ON p.id = mp.person_id AND p.contact_type = 'Journaliste'"
    ).fetchone()[0]
    if rows:
        for date, direction, name, media, subject in rows:
            arrow = "→" if direction == "sent" else "←"
            log(f"  {date} {arrow} {name} ({media}) — {subject[:60]}")
        log(f"  … {total_press} courriel(s) presse au total.")
    else:
        log("  aucun — soit aucun échange presse dans la boîte, soit le "
            "rapprochement n'a rien trouvé.")

    log("\n== File d'attente CiviCRM ==")
    for status, total in db.execute(
        "SELECT status, COUNT(*) FROM civicrm_pending GROUP BY status ORDER BY status"
    ):
        log(f"  {status:<10} {total}")

    log("\n== Adresses non rattachées, les plus fréquentes ==")
    # Ordered by how often each turned up: what is worth a human's attention is
    # the address seen twenty times, not the first one alphabetically.
    unresolved = db.execute(
        "SELECT email, COALESCE(display, ''), seen_count, status "
        "FROM civicrm_pending WHERE status != 'resolved' "
        "ORDER BY seen_count DESC, first_seen LIMIT ?", (limit,)
    ).fetchall()
    for mail, display, seen, status in unresolved:
        who = f" — {display}" if display else ""
        log(f"  {seen:>3}× {mail}{who}  [{status}]")
    if not unresolved:
        log("  aucune.")

    conventions = domain_conventions(db)
    log(f"\n== Conventions d'adresses apprises : {len(conventions)} ==")
    log("  (détail : --patterns --all)")
    return 0


def cmd_prune(db, args):
    """Drop queued addresses the filters now reject.

    The filters get tightened as the audit mailbox shows what it really carries
    (the first real run queued four robots and no journalist), so addresses
    already in the queue have to be re-judged against the current rules.

    Two rules apply here. The old one: a generic address (`contact@`,
    `redaction@`) names a desk, not a person. The new one, and the reason this
    command matters now: the queue only admits an address the association has a
    positive reason to follow (see maildomains.in_scope). Everything queued
    before that rule existed — a member's doctor, their bank, a supplier — is
    personal data we never had any business keeping, so it goes.

    `resolved` rows are left alone: they carry a fiche and a real link.
    """
    maildomains.refresh_scope(db)
    doomed = [r[0] for r in db.execute(
        "SELECT email FROM civicrm_pending WHERE status IN ('pending', 'absent', "
        "'ignored')")
        if is_generic(r[0]) or not maildomains.in_scope(r[0])]
    for address in doomed:
        log(f"  - {address}")
        if args.commit:
            db.execute("DELETE FROM civicrm_pending WHERE email = ?", (address,))
    if args.commit:
        db.commit()
    prefix = "" if args.commit else "[dry-run] "
    log(f"{prefix}{len(doomed)} adresse(s) retirée(s) de la file (générique, ou "
        f"hors du périmètre que l'association a une raison de suivre).")
    return 0


def cmd_apply(db, args):
    with open(args.apply, encoding="utf-8") as fh:
        records = json.load(fh)
    try:
        assert_contract(records, CONTACT_FIELDS, "contact",
                        optional=CONTACT_FIELDS_OPTIONAL, warn=log)
    except ContractError as exc:
        log(f"ABANDON — contrat CiviCRM non respecté : {exc}")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now[:10]

    by_email, by_id = {}, {}
    for record in records:
        by_id[record.get("id")] = record
        mail = clean_email(record.get("email_primary.email"))
        if mail:
            by_email.setdefault(mail, record)

    # A journalist often has several addresses in CiviCRM and the one a member
    # wrote to is frequently not the primary, so --emails carries the
    # address -> contact mapping that `Email.get` produced. Without it we fall
    # back to primary addresses only, which is how this started and why a
    # secondary address used to be filed as "unknown to CiviCRM".
    addr_to_contact = {}
    if args.emails:
        with open(args.emails, encoding="utf-8") as fh:
            for row in json.load(fh):
                mail = clean_email(row.get("email"))
                cid = row.get("contact_id")
                if mail and cid is not None:
                    addr_to_contact.setdefault(mail, cid)

    pending = [r[0] for r in db.execute(
        "SELECT email FROM civicrm_pending WHERE status = 'pending'")]
    person_index = load_person_index(db)
    media_index = load_media_index(db)
    known = existing_emails(db)

    created = attached = absent = already = out_of_scope = 0
    for address in pending:
        record = by_email.get(address) or by_id.get(addr_to_contact.get(address))
        if record is None:
            db.execute(
                "UPDATE civicrm_pending SET status = 'absent', resolved_at = ? "
                "WHERE email = ?", (now, address))
            absent += 1
            continue
        if address in known:
            # Someone typed the fiche in while the lookup was in flight.
            db.execute(
                "UPDATE civicrm_pending SET status = 'resolved', resolved_at = ? "
                "WHERE email = ?", (now, address))
            already += 1
            continue
        row = contact_to_person(record, today)
        if row is not None and row["contact_type"] == "Autre" \
                and not args.include_other:
            # CiviCRM holds the association's own volunteers, members and allies
            # alongside its journalists, and a lookup by address finds them just
            # as readily. Their CiviCRM sub-type maps to no CONTACT_TYPE here, so
            # they would land as "Autre" — and this CRM follows *external*
            # relations: a fiche for a teammate is simply wrong. The first real
            # sync was about to create 26 of them, 21 being volunteers.
            out_of_scope += 1
            db.execute(
                "UPDATE civicrm_pending SET status = 'absent', resolved_at = ? "
                "WHERE email = ?", (now, address))
            continue
        if row is not None:
            # The address the member actually corresponded with is the one worth
            # storing — it is what future mails will carry.
            row["email"] = address
        if row is None or not row["email"]:
            db.execute(
                "UPDATE civicrm_pending SET status = 'absent', resolved_at = ? "
                "WHERE email = ?", (now, address))
            absent += 1
            continue
        if args.commit:
            person_id, action = create_or_attach(
                db, row, now, person_index, media_index)
            db.execute(
                "UPDATE civicrm_pending SET status = 'resolved', resolved_at = ?, "
                "person_id = ? WHERE email = ?", (now, person_id, address))
        else:
            action = ("attached"
                      if (norm_name(row["name"]), row["contact_type"]) in person_index
                      else "created")
        known.add(address)
        if action == "created":
            created += 1
        else:
            attached += 1
        media = f" — {row['media_name']}" if row["media_name"] else ""
        log(f"  {'+' if action == 'created' else '~'} {row['name']}{media} "
            f"<{row['email']}>")

    if args.commit:
        db.commit()
    prefix = "" if args.commit else "[dry-run] "
    log(f"{prefix}Terminé. Fiches créées : {created} | fiches complétées : "
        f"{attached} | déjà connues : {already} | inconnues de CiviCRM : {absent} "
        f"| hors périmètre (bénévoles, sympathisant·es…) : {out_of_scope} "
        f"| en file au départ : {len(pending)}.")
    if created or attached:
        log("Relancez ensuite : import_member_mails.py --backfill "
            "(les mails en attente se rattacheront aux nouvelles fiches).")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-pending", action="store_true",
                       help="print the addresses awaiting a CiviCRM lookup")
    group.add_argument("--apply", metavar="FILE",
                       help="JSON written by `cv api4 Contact.get`")
    parser.add_argument("--emails", metavar="FILE",
                        help="JSON written by `cv api4 Email.get` (address -> "
                             "contact), so secondary addresses resolve too")
    group.add_argument("--stats", action="store_true", help="queue counts by status")
    group.add_argument("--report", action="store_true",
                       help="what the last import produced: press mail attached, "
                            "queue state, and the most frequent unresolved "
                            "addresses")
    group.add_argument("--retry-absent", action="store_true",
                       help="put addresses CiviCRM didn't know back in the queue")
    group.add_argument("--prune", action="store_true",
                       help="drop queued addresses the current filters reject")
    group.add_argument("--patterns", action="store_true",
                       help="print the média conventions the pending addresses "
                            "need, as JSON")
    group.add_argument("--apply-names", metavar="FILE",
                       help="JSON of journalists by média: resolve the remaining "
                            "addresses through their média's convention")
    group.add_argument("--seed", metavar="FILE",
                       help="create fiches for a whole CiviCRM group, to get the "
                            "cycle started (keep it narrow — see SEED_SOFT_CAP)")
    parser.add_argument("--include-other", action="store_true",
                        help="--apply: also create fiches for CiviCRM contacts "
                             "whose sub-type maps to no CONTACT_TYPE here "
                             "(bénévoles, sympathisant·es…). Off by default: "
                             "this CRM follows external relations, not the "
                             "association's own people.")
    parser.add_argument("--all", action="store_true",
                        help="--patterns: show every learned convention, not "
                             "only those the queue needs")
    parser.add_argument("--force", action="store_true",
                        help="--seed: accept an export past the soft cap")
    parser.add_argument("--verbose", action="store_true",
                        help="--seed: name every fiche")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to meetings.db")
    parser.add_argument("--commit", action="store_true",
                        help="write; without it nothing is saved")
    parser.add_argument("--limit", type=int, default=500,
                        help="max rows printed by --list-pending (default 500) "
                             "or by each section of --report (use ~20 there)")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_civicrm_tables(db)
        if args.list_pending:
            return cmd_list_pending(db, args)
        if args.stats:
            return cmd_stats(db, args)
        if args.report:
            return cmd_report(db, args)
        if args.retry_absent:
            return cmd_retry_absent(db, args)
        if args.prune:
            return cmd_prune(db, args)
        if args.seed:
            return cmd_seed(db, args)
        if args.patterns:
            return cmd_patterns(db, args)
        if args.apply_names:
            return cmd_apply_names(db, args)
        return cmd_apply(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
