#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Inserts the French bishops (produced by utils/extract_eveques.py) into the
CRM's `persons` table as religieux·ses, and their diocèses into
`organisations` as cultes:

    python3 utils/extract_eveques.py      # refresh the JSON first
    python3 utils/insert_eveques.py       # dry run
    python3 utils/insert_eveques.py --commit

What each entry becomes:

    « Mgr Marc Aillet » / « Évêque de Bayonne »
      -> persons     : Marc Aillet, Religieux·se, Catholicisme,
                       role « Évêque », territoire « Bayonne »
      -> organisations: « Diocèse de Bayonne », Culte, Catholicisme
      -> person_organisations: the two, linked

The honorific is dropped from the name (« Mgr », « S. Ém. le cardinal »): the
CRM stores plain names, and « cardinal » is a fonction, so it is added to the
role list instead — which is exactly why a person can hold several. A title
saying « émérite » sets `in_office = 0`, the same flag the élu·e importers use
for an ended mandate, so retired bishops stay on file without looking current.

Idempotent, and matched by name among religieux·ses only, so a bishop who
happens to share a name with an élu·e or a journaliste stays a separate fiche
and a rerun adds nothing.
"""
import argparse
import ast
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "actual_dataset", "eveques.json")
DB = os.path.join(ROOT, "meetings.db")

DEFAULT_ADDED_BY = "Hugo"
RELIGION = "Catholicisme"
ROLE_SEP = ", "
NOTE_SOURCE = "Importé depuis l'annuaire de la Conférence des évêques de France"

# Honorifics the annuaire prefixes to a name. Stripped so `persons.name` holds
# a plain name, as everywhere else in the table.
HONORIFICS = re.compile(
    r"^(?:Mgr|S\.\s*Ém\.\s*le\s+cardinal|S\.\s*Exc\.|Son\s+Éminence|"
    r"le\s+cardinal|Père|Abbé)\s+", re.I)

# Title prefix -> fonction, longest first so « Évêque auxiliaire » is tested
# before « Évêque ». The tail of the title is the see, and becomes both the
# diocèse's name and the person's « Territoire assigné ».
#
# `diocesan` says whether the title implies a diocèse worth creating: a nonce
# or a curia prefect answers for no French see, so they get a territoire and no
# organisation rather than a made-up one.
TITLE_ROLES = [
    ("Archevêque émérite",      "Archevêque",        True),
    ("Archevêque-Évêque",       "Archevêque",        True),
    ("Archevêque-évêque",       "Archevêque",        True),
    ("Archevêque",              "Archevêque",        True),
    ("Évêque auxiliaire",       "Évêque auxiliaire", True),
    ("Évêque coadjuteur",       "Évêque",            True),
    ("Évêque émérite",          "Évêque",            True),
    ("Évêque",                  "Évêque",            True),
    ("Nonce apostolique",       "Nonce apostolique", False),
    ("Administrateur apostolique", "Évêque",         False),
]

# Prepositions that join a title to its see: kept in the diocèse's name
# (« Diocèse aux Armées françaises » is the real name of that one), stripped
# from the territoire, which reads better bare (« Bayonne », « Le Havre »).
PREPOSITIONS = ("de la ", "de l'", "de l’", "des ", "du ", "de ", "d'", "d’",
                "aux ", "au ", "à ")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(name):
    """Case- and accent-insensitive match key."""
    folded = unicodedata.normalize("NFKD", (name or "").strip().casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def app_constant(name):
    """Read a top-level constant from app.py as plain text (no import — the app
    pulls in Flask and libmagic). The guard the élu·e importers use: a label
    renamed in app.py must fail loudly here rather than produce rows the form
    cannot edit. This is why RELIGIOUS_ROLES_BY_RELIGION stays a plain literal
    — a comprehension would not survive ast.literal_eval."""
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} introuvable dans app.py")


def clean_name(raw):
    return HONORIFICS.sub("", (raw or "").strip()).strip()


def split_title(titre):
    """« Évêque auxiliaire de Paris » -> ('Évêque auxiliaire', 'de Paris', True).

    Returns (role, see_with_preposition, diocesan). role is None when the title
    matches nothing known, so the caller can report it instead of guessing.
    """
    t = (titre or "").strip()
    for prefix, role, diocesan in TITLE_ROLES:
        if t.lower().startswith(prefix.lower()):
            return role, find_see(t[len(prefix):]), diocesan
    return None, "", False


# Where the see starts: at the first preposition standing as its own word.
# Most titles are already there (« Évêque | de Bayonne »), but a few carry a
# canonical formula in between — « Administrateur apostolique *sede vacante et
# ad nutum Sanctae Sedis* de l'éparchie… » — and taking the remainder verbatim
# would file that person under « sede vacante ».
SEE_START = re.compile(
    r"\b(?:de la|de l['’]|des|du|de|d['’]|aux|au|à)\s", re.I)


def find_see(rest):
    rest = rest.strip()
    match = SEE_START.search(rest)
    return rest[match.start():].strip() if match else rest


def diocese_name(see):
    """« de Bordeaux et évêque émérite de Bazas » -> « Diocèse de Bordeaux ».

    A few titles name two sees or carry a canonical formula. Everything from
    the first « et » or comma is dropped, and anything still long is refused:
    a wrong diocèse is worse than none, and the report names what was skipped.
    """
    see = re.split(r"\s+et\s+|,", see)[0].strip()
    if not see or len(see) > 60:
        return None
    return f"Diocèse {see}"


def territoire(see):
    """The see without its preposition: « de Bayonne » -> « Bayonne ».

    Cut at the same points as diocese_name: a couple of titles name a second
    see (« … de Bordeaux et évêque émérite de Bazas »), and the territoire is
    the seat someone answers for, not the whole sentence.
    """
    see = re.split(r"\s+et\s+|,", see)[0].strip()
    low = see.lower()
    for prep in PREPOSITIONS:
        if low.startswith(prep):
            return see[len(prep):].strip()
    return see


def moderator_id(db, name):
    for mid, existing in db.execute("SELECT id, name FROM moderators"):
        if key(existing) == key(name):
            return mid
    return db.execute("INSERT INTO moderators (name) VALUES (?)", (name,)).lastrowid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it, only report")
    parser.add_argument("--added-by", default=DEFAULT_ADDED_BY,
                        help=f"utilisateurice recorded as having added the rows "
                             f"(default: {DEFAULT_ADDED_BY})")
    args = parser.parse_args()

    catholic = app_constant("RELIGIOUS_ROLES_BY_RELIGION")[RELIGION]
    catholic += app_constant("RELIGIOUS_COMMON_ROLES")
    produced = {r for _, r, _ in TITLE_ROLES} | {"Cardinal"}
    unknown = sorted(produced - set(catholic))
    if unknown:
        raise SystemExit(
            f"Fonctions absentes de RELIGIOUS_ROLES_BY_RELIGION['{RELIGION}'] "
            f"dans app.py : {unknown}")

    if not os.path.exists(SRC):
        sys.exit(f"{SRC} introuvable — lancez d'abord utils/extract_eveques.py")
    if not os.path.exists(DB):
        sys.exit(f"Base introuvable : {DB}")
    entries = json.load(open(SRC, encoding="utf-8"))["eveques"]

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    cols = [r[1] for r in db.execute("PRAGMA table_info(persons)")]
    if "religion" not in cols or "territoire" not in cols:
        sys.exit("Les colonnes `persons.religion` / `persons.territoire` "
                 "n'existent pas encore : démarrez l'app une fois pour "
                 "appliquer la migration, puis relancez ce script.")

    who = moderator_id(db, args.added_by)
    stamp = now()

    people = {
        key(r["name"]): r["id"]
        for r in db.execute(
            "SELECT id, name FROM persons WHERE contact_type = 'Religieux·se'")
    }
    orgs = {
        key(r["name"]): r["id"]
        for r in db.execute(
            "SELECT id, name FROM organisations WHERE org_type = 'Culte'")
    }

    added, skipped, no_role, no_diocese, new_orgs, links = [], 0, [], [], 0, 0
    for entry in entries:
        name = clean_name(entry["nom"])
        role, see, diocesan = split_title(entry["titre"])
        if role is None:
            no_role.append(f"{name} — {entry['titre']}")
        roles = [role] if role else []
        # « S. Ém. le cardinal … » in the annuaire is a fonction in the CRM.
        if re.search(r"cardinal", entry["nom"], re.I):
            roles.insert(0, "Cardinal")
        # Keep ROLES' own order, the order the app writes the column in.
        roles = [r for r in catholic if r in set(roles)]
        # « émérite » anywhere in the title: no current charge.
        in_office = 0 if "mérite" in (entry["titre"] or "") else 1

        if key(name) in people:
            skipped += 1
            continue
        pid = db.execute(
            """
            INSERT INTO persons (name, contact_type, religion, role, territoire,
                stance, in_office, notes, added_by, validated_by, created_at)
            VALUES (?, 'Religieux·se', ?, ?, ?, 'Inconnu', ?, ?, ?, ?, ?)
            """,
            (name, RELIGION, ROLE_SEP.join(roles) or None,
             territoire(see) or None, in_office,
             f"{NOTE_SOURCE} — « {entry['titre']} ». {entry['profil'] or ''}".strip(),
             who, who, stamp),
        ).lastrowid
        people[key(name)] = pid
        added.append(f"{name} — {entry['titre']}")

        if not diocesan:
            continue
        dio = diocese_name(see)
        if dio is None:
            no_diocese.append(f"{name} — {entry['titre']}")
            continue
        oid = orgs.get(key(dio))
        if oid is None:
            oid = db.execute(
                """
                INSERT INTO organisations (name, org_type, religion, stance,
                    notes, added_by, validated_by, created_at)
                VALUES (?, 'Culte', ?, 'Inconnu', ?, ?, ?, ?)
                """,
                (dio, RELIGION, NOTE_SOURCE, who, who, stamp),
            ).lastrowid
            orgs[key(dio)] = oid
            new_orgs += 1
        db.execute(
            "INSERT OR IGNORE INTO person_organisations (person_id, "
            "organisation_id) VALUES (?, ?)", (pid, oid))
        links += 1

    print(f"évêques ajoutés     : {len(added)} (sur {len(entries)})")
    print(f"déjà présents       : {skipped}")
    print(f"diocèses créés      : {new_orgs}")
    print(f"liens évêque ↔ diocèse : {links}")
    if no_role:
        print(f"\n! titre non reconnu, fiche créée sans fonction ({len(no_role)}) :")
        for line in no_role:
            print(f"    {line}")
    if no_diocese:
        print(f"\n! diocèse non déduit, à rattacher à la main ({len(no_diocese)}) :")
        for line in no_diocese:
            print(f"    {line}")

    # A religieux·se has no groupe politique; the mirror column must stay NULL.
    stray = db.execute(
        "SELECT COUNT(*) FROM persons WHERE contact_type = 'Religieux·se' "
        "AND political_group IS NOT NULL"
    ).fetchone()[0]
    print(f"\nreligieux·ses avec un groupe politique résiduel : {stray}")
    totals = db.execute(
        "SELECT (SELECT COUNT(*) FROM organisations WHERE org_type='Culte') c, "
        "(SELECT COUNT(*) FROM persons WHERE contact_type='Religieux·se') p"
    ).fetchone()
    print(f"la base contient maintenant : {totals['c']} cultes, "
          f"{totals['p']} religieux·ses")
    if args.commit:
        db.commit()
        print("→ écrit dans", DB)
    else:
        db.rollback()
        print("→ simulation (relancer avec --commit pour écrire)")
    db.close()


if __name__ == "__main__":
    main()
