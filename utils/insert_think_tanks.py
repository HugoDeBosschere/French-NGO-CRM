#!/usr/bin/env python3
"""Seed French think tanks into the CRM, from data baked in here.

Demande de Jeanne : suivre les think tanks « et assimilés », parce
qu'ils conseillent les politiques. Same shape as utils/insert_cultes.py: a short,
hand-picked list of the organisations PauseIA would actually write to — the ones
that publish on le numérique, l'IA, la souveraineté technologique or the
institutions that will regulate it — so that a chercheur·euse can be filed
under something real from the first day.

Why a list and not an import. There is no open dataset of French think tanks:
the HATVP répertoire only holds those registered as représentants d'intérêts,
which most are not. No people are created here either: researchers are added
by hand as contact is made, with a source.

Idempotent, and matched by name among think tanks only: a row that already
exists as some other org_type (an ONG, say) is left alone, and a rerun adds
nothing. Every row is stance « Inconnu » and carries a note saying it is to be
checked: the links were written from public knowledge on 19/11/2026.

    python3 utils/insert_think_tanks.py            # dry run
    python3 utils/insert_think_tanks.py --commit   # write

`--added-by` names the utilisateurice recorded in « Qui a ajouté » and
« Validé par »; they are created if absent.
"""

import argparse
import ast
import os
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "meetings.db")
ORG_TYPE = "Think tank"

# Provenance of every row below, unless --added-by says otherwise.
DEFAULT_ADDED_BY = "Antoine"

NOTE = ("Pré-rempli le 19/11/2026 (connaissances publiques, lien à vérifier) — "
        "{domaine}. À compléter au fil des contacts.")

# (name, link, domaine). The domaine is what the think tank works on, in a few
# words: the reason it is on this list.
THINK_TANKS = [
    # --- Généralistes, très lus par les décideurs ---
    ("Institut Montaigne", "https://www.institutmontaigne.org",
     "généraliste, publie sur le numérique et l'IA"),
    ("Terra Nova", "https://tnova.fr",
     "généraliste, progressiste"),
    ("Fondation Jean-Jaurès", "https://www.jean-jaures.org",
     "généraliste, études d'opinion"),
    ("Fondation pour l'innovation politique (Fondapol)", "https://www.fondapol.org",
     "généraliste, libéral"),
    ("GénérationLibre", "https://www.generationlibre.eu",
     "libéral, libertés numériques"),
    ("Institut Rousseau", "https://institut-rousseau.fr",
     "généraliste, économie et écologie"),
    ("Institut Sapiens", "https://www.institutsapiens.fr",
     "économie et technologies"),
    ("Institut Thomas More", "https://institut-thomas-more.org",
     "généraliste, conservateur"),
    ("Fondation Res Publica", "https://fondation-res-publica.org",
     "souveraineté, République"),
    ("Institut de l'entreprise", "https://www.institut-entreprise.fr",
     "entreprises et économie"),
    ("La Fabrique de l'industrie", "https://www.la-fabrique.fr",
     "industrie, innovation"),
    # --- Relations internationales, défense, Europe ---
    ("Institut français des relations internationales (Ifri)", "https://www.ifri.org",
     "relations internationales, géopolitique des technologies"),
    ("Institut de relations internationales et stratégiques (IRIS)",
     "https://www.iris-france.org",
     "relations internationales, défense"),
    ("Fondation pour la recherche stratégique (FRS)", "https://www.frstrategie.org",
     "défense, sécurité, armes autonomes"),
    ("Institut Jacques Delors", "https://institutdelors.eu",
     "Union européenne, régulation"),
    ("Fondation Robert Schuman", "https://www.robert-schuman.eu",
     "Union européenne"),
    # --- Numérique, information, IA ---
    ("Renaissance numérique", "https://www.renaissancenumerique.org",
     "numérique et société, régulation de l'IA"),
    ("Fondation Descartes", "https://www.fondationdescartes.org",
     "information et désinformation"),
    ("The Shift Project", "https://theshiftproject.org",
     "climat, empreinte du numérique"),
    ("Centre pour la sécurité de l'IA (CeSIA)", "https://cesia.org",
     "sécurité de l'IA, risques extrêmes"),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(name):
    """Case- and accent-insensitive match key, so « Ifri » finds « IFRI »."""
    folded = unicodedata.normalize("NFKD", (name or "").strip().casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def moderator_id(db, name):
    """The utilisateurice id for `name`, created if they don't exist yet."""
    for mid, existing in db.execute("SELECT id, name FROM moderators"):
        if key(existing) == key(name):
            return mid
    return db.execute("INSERT INTO moderators (name) VALUES (?)", (name,)).lastrowid


def app_org_types():
    """ORG_TYPES as app.py defines it, read without importing the app.

    Same guard as insert_cultes.py: if « Think tank » is renamed in app.py, this
    must fail loudly rather than write rows the form cannot offer.
    """
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "ORG_TYPES" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit("ORG_TYPES introuvable dans app.py")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it, only report")
    parser.add_argument("--added-by", default=DEFAULT_ADDED_BY,
                        help=f"utilisateurice recorded as having added the rows "
                             f"(default: {DEFAULT_ADDED_BY})")
    parser.add_argument("--db", default=os.environ.get("IMAP_DB_PATH", DB),
                        help="base SQLite (défaut : meetings.db, ou IMAP_DB_PATH)")
    args = parser.parse_args(argv)

    if ORG_TYPE not in app_org_types():
        raise SystemExit(f"« {ORG_TYPE} » absent de ORG_TYPES dans app.py")

    if not os.path.exists(args.db):
        sys.exit(f"Base introuvable : {args.db}")
    db = sqlite3.connect(args.db)
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    who = moderator_id(db, args.added_by)
    stamp = now()

    # Existing think tanks only. An ONG sharing a name is a different row and
    # must not be duplicated or touched: it is reported, a human decides.
    existing = {
        key(r["name"]): r["id"]
        for r in db.execute(
            "SELECT id, name FROM organisations WHERE org_type = ?", (ORG_TYPE,))
    }
    other_types = {
        key(r["name"]): r["org_type"]
        for r in db.execute(
            "SELECT name, org_type FROM organisations WHERE org_type != ?", (ORG_TYPE,))
    }

    added, skipped, elsewhere = [], [], []
    for name, link, domaine in THINK_TANKS:
        if key(name) in existing:
            skipped.append(name)
            continue
        if key(name) in other_types:
            elsewhere.append(f"{name} (déjà en « {other_types[key(name)]} »)")
            continue
        db.execute(
            """
            INSERT INTO organisations (name, org_type, stance, link, notes,
                added_by, validated_by, created_at)
            VALUES (?, ?, 'Inconnu', ?, ?, ?, ?, ?)
            """,
            (name, ORG_TYPE, link, NOTE.format(domaine=domaine), who, who, stamp),
        )
        added.append(name)

    print(f"think tanks ajoutés : {len(added)} (sur {len(THINK_TANKS)})")
    for line in added:
        print(f"  + {line}")
    if skipped:
        print(f"déjà présents       : {len(skipped)}")
        for line in skipped:
            print(f"  = {line}")
    if elsewhere:
        print(f"sous un autre type  : {len(elsewhere)} (laissés tels quels, à trancher)")
        for line in elsewhere:
            print(f"  ? {line}")

    total = db.execute(
        "SELECT COUNT(*) FROM organisations WHERE org_type = ?", (ORG_TYPE,)
    ).fetchone()[0]
    print(f"\nla base contient maintenant : {total} think tanks")
    if args.commit:
        db.commit()
        print("→ écrit dans", args.db)
    else:
        db.rollback()
        print("→ simulation (relancer avec --commit pour écrire)")
    db.close()


if __name__ == "__main__":
    main()
