#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Run it by hand to insert the currently-serving senators from
actual_dataset/senateurices_actifs.json into the app's `persons` table
(e.g. to seed the DB before deployment, or to refresh the list later):

    python3 utils/insert_senateurices.py

Mapping applied per senator:
- role            -> "Sénateur·ice"
- political_group -> mapped from the Sénat group short label to the app's exact
                     POLITICAL_GROUPS label (see GROUPE_TO_GROUP)
- stance          -> "Inconnu" (unknown until someone contacts them)
- circonscription -> département / territory represented (the Sénat dataset
                     carries no email, so `email` is left NULL)
- added_by / validated_by -> NULL (imported, not entered by a moderator)

Idempotent: skips a senator whose name already exists in `persons`.
"""
import ast
import json
import os
import sqlite3
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "actual_dataset", "senateurices_actifs.json")
DB = os.path.join(ROOT, "meetings.db")

# Sénat group `libelleCourt` -> exact POLITICAL_GROUPS["Sénat"] label in app.py.
GROUPE_TO_GROUP = {
    "Les Républicains": "Les Républicains (Sénat)",
    "SER": "Socialiste, Écologiste et Républicain (Sénat)",
    "UC": "Union Centriste (Sénat)",
    "RDPI": "Rassemblement des démocrates, progressistes et indépendants (RDPI)",
    "CRCE-K": "Communiste, Républicain, Citoyen et Écologiste (CRCE-K)",
    "Les Indépendants": "Les Indépendants – République et Territoires",
    "GEST": "Écologiste – Solidarité et Territoires (Sénat)",
    "RDSE": "Rassemblement Démocratique et Social Européen (RDSE)",
    "NI": "Non-inscrit (Sénat)",
}


def app_senate_labels():
    """Read POLITICAL_GROUPS from app.py as plain text (no import — the app
    pulls in heavy deps). Purely a safety check when this script is run by hand,
    so a group label renamed in app.py can't silently produce uneditable rows."""
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "POLITICAL_GROUPS" for t in node.targets
        ):
            groups = ast.literal_eval(node.value)
            return set(groups["Sénat"])
    raise RuntimeError("POLITICAL_GROUPS not found in app.py")


def main():
    valid = app_senate_labels()
    bad = {k: v for k, v in GROUPE_TO_GROUP.items() if v not in valid}
    if bad:
        raise SystemExit(
            "Mapping targets not present in POLITICAL_GROUPS['Sénat']: " + repr(bad)
        )

    senateurices = json.load(open(SRC, encoding="utf-8"))
    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys = ON")

    existing = {r[0] for r in db.execute("SELECT name FROM persons")}
    now = datetime.utcnow().isoformat(timespec="seconds")

    inserted, skipped, unmapped = 0, 0, []
    for s in senateurices:
        name = f"{s['prenom']} {s['nom']}".strip()
        if name in existing:
            skipped += 1
            continue
        short = (s.get("groupe") or {}).get("libelleCourt")
        group = GROUPE_TO_GROUP.get(short)
        if group is None:
            unmapped.append((name, short))
            continue
        circo = (s.get("circonscription") or {}).get("libelle")
        db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted,
                follow_up_date, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?)
            """,
            (name, "Sénateur·ice", group, "Inconnu", circo, now),
        )
        inserted += 1
        existing.add(name)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(f"Inserted: {inserted}  |  Skipped (already present): {skipped}")
    if unmapped:
        print(f"UNMAPPED groups ({len(unmapped)}):", unmapped)
    print(f"persons table now holds: {total}")


if __name__ == "__main__":
    main()
