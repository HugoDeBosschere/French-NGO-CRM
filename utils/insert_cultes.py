#!/usr/bin/env python3
"""Seed the religious organisations into the CRM, from data baked in here.

The companion of utils/insert_medias_journalistes.py for the third type de
contact. It creates the umbrella organisations of each culte plus the bodies
PauseIA would actually write to — the Conférence des évêques de France, the
Fédération protestante de France, the Consistoire central, the Grande Mosquée
de Paris and so on — so that a religieux·se can be filed under something real
from the first day, without anyone retyping them.

Deliberately small. There is no open dataset of French religious organisations
worth importing wholesale: SIRENE's NAF 94.91Z holds some 18 000 rows but no
religion label and no people, and nothing at all is published for the Muslim
side since the CFCM lapsed and FORIF's membership was never released. Diocèses
are the one exception, and they have their own pair of scripts
(extract_eveques.py / insert_eveques.py). Everything else is added by hand as
contact is made.

Idempotent, and matched by name among cultes only, the way the journaliste
importer matches within its own type: a « Consistoire » that already exists as
some other org_type is left alone and a rerun adds nothing.

    uv run python utils/insert_cultes.py            # dry run
    uv run python utils/insert_cultes.py --commit    # write

`--added-by` names the utilisateurice recorded in « Qui a ajouté » and
« Validé par »; they are created if absent.
"""

import argparse
import os
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "meetings.db")

# Provenance of every row below, unless --added-by says otherwise.
DEFAULT_ADDED_BY = "Hugo"

NOTE = ("Pré-rempli le 16/09/2026 (connaissances publiques) — "
        "instance représentative, à compléter au fil des contacts.")

# (name, religion, link). The stance is "Inconnu" for all of them: nobody has
# asked any of these bodies what they think of PauseIA yet, and recording a
# guess would be worse than recording nothing.
#
# Two kinds of row, deliberately mixed: the culte itself (« Église catholique »
# — what someone reaches for when they know only the religion) and the body
# that actually speaks for it (« Conférence des évêques de France » — who you
# write to). Both are organisations; which one a person belongs to is a
# judgement the person's fiche records.
CULTES = [
    # --- Catholicisme ---
    ("Église catholique en France", "Catholicisme", "https://eglise.catholique.fr"),
    ("Conférence des évêques de France (CEF)", "Catholicisme",
     "https://eglise.catholique.fr/conference-des-eveques-de-france/"),
    ("Conférence des religieux et religieuses de France (CORREF)", "Catholicisme",
     "https://www.viereligieuse.fr"),
    # --- Protestantisme ---
    ("Fédération protestante de France (FPF)", "Protestantisme",
     "https://www.protestants.org"),
    ("Église protestante unie de France (EPUdF)", "Protestantisme",
     "https://www.eglise-protestante-unie.fr"),
    ("Conseil national des évangéliques de France (CNEF)", "Protestantisme",
     "https://www.lecnef.org"),
    # --- Christianisme orthodoxe ---
    ("Assemblée des évêques orthodoxes de France (AEOF)", "Christianisme orthodoxe",
     "https://aeof.fr"),
    # --- Judaïsme ---
    ("Consistoire central israélite de France", "Judaïsme",
     "https://www.consistoire.org"),
    ("Grand Rabbinat de France", "Judaïsme", "https://www.consistoire.org"),
    ("Conseil représentatif des institutions juives de France (CRIF)", "Judaïsme",
     "https://www.crif.org"),
    # --- Islam ---
    ("Grande Mosquée de Paris", "Islam", "https://www.mosqueedeparis.net"),
    ("Forum de l'islam de France (FORIF)", "Islam", "https://leforif.fr"),
    ("Union des mosquées de France (UMF)", "Islam", "https://unionmosquees.fr"),
    # --- Bouddhisme ---
    ("Union bouddhiste de France (UBF)", "Bouddhisme",
     "https://www.bouddhisme-france.org"),
    # --- Interreligieux ---
    ("Conférence des responsables de culte en France (CRCF)",
     "Autre culte / Interreligieux", None),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(name):
    """Case- and accent-insensitive match key, so « Eglise » finds « Église »."""
    folded = unicodedata.normalize("NFKD", (name or "").strip().casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def moderator_id(db, name):
    """The utilisateurice id for `name`, created if they don't exist yet."""
    for mid, existing in db.execute("SELECT id, name FROM moderators"):
        if key(existing) == key(name):
            return mid
    return db.execute("INSERT INTO moderators (name) VALUES (?)", (name,)).lastrowid


def app_religions():
    """RELIGIONS as app.py defines it, read without importing the app.

    The same guard the élu·e importers use: a religion renamed in app.py must
    fail here loudly rather than produce rows whose value the form cannot
    offer. app.py is not imported because it pulls in Flask and libmagic;
    ast.literal_eval is why RELIGIONS has to stay a plain list literal.
    """
    import ast
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "RELIGIONS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit("RELIGIONS introuvable dans app.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it, only report")
    parser.add_argument("--added-by", default=DEFAULT_ADDED_BY,
                        help=f"utilisateurice recorded as having added the rows "
                             f"(default: {DEFAULT_ADDED_BY})")
    args = parser.parse_args()

    known = app_religions()
    unknown = sorted({r for _, r, _ in CULTES} - set(known))
    if unknown:
        raise SystemExit(f"Religions absentes de RELIGIONS dans app.py : {unknown}")

    if not os.path.exists(DB):
        sys.exit(f"Base introuvable : {DB}")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    cols = [r[1] for r in db.execute("PRAGMA table_info(organisations)")]
    if "religion" not in cols:
        sys.exit("La colonne `organisations.religion` n'existe pas encore : "
                 "démarrez l'app une fois pour appliquer la migration, puis "
                 "relancez ce script.")

    who = moderator_id(db, args.added_by)
    stamp = now()

    # Existing cultes only. A média or a groupe politique sharing a name is a
    # different organisation and must not be updated in place.
    existing = {
        key(r["name"]): r["id"]
        for r in db.execute(
            "SELECT id, name FROM organisations WHERE org_type = 'Culte'")
    }

    added, skipped = [], []
    for name, religion, link in CULTES:
        if key(name) in existing:
            skipped.append(name)
            continue
        db.execute(
            """
            INSERT INTO organisations (name, org_type, religion, stance, link,
                notes, added_by, validated_by, created_at)
            VALUES (?, 'Culte', ?, 'Inconnu', ?, ?, ?, ?, ?)
            """,
            (name, religion, link, NOTE, who, who, stamp),
        )
        added.append(f"{name} — {religion}")

    print(f"cultes ajoutés   : {len(added)} (sur {len(CULTES)})")
    for line in added:
        print(f"  + {line}")
    if skipped:
        print(f"déjà présents    : {len(skipped)}")
        for line in skipped:
            print(f"  = {line}")

    total = db.execute(
        "SELECT COUNT(*) FROM organisations WHERE org_type = 'Culte'"
    ).fetchone()[0]
    print(f"\nla base contient maintenant : {total} cultes")
    if args.commit:
        db.commit()
        print("→ écrit dans", DB)
    else:
        db.rollback()
        print("→ simulation (relancer avec --commit pour écrire)")
    db.close()


if __name__ == "__main__":
    main()
