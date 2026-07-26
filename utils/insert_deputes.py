#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Insert the currently-sitting deputies from actual_dataset/deputes_officiel.json
into the app's `persons` table:

    python3 utils/insert_deputes.py

Mapping applied per deputy:
- role            -> "Député·e"
- political_group -> mapped from the group sigle to the app's exact
                     POLITICAL_GROUPS label (see SIGLE_TO_GROUP)
- stance          -> "Inconnu" (unknown until someone contacts them)
- circonscription -> département + circo number
- email           -> official @assemblee-nationale.fr address, when present
- added_by / validated_by -> NULL (imported, not entered by a moderator)

Idempotent: skips a deputy whose name already exists in `persons`.
"""
import json
import os
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "actual_dataset", "deputes_officiel.json")
DB = os.path.join(ROOT, "meetings.db")

# Group sigle (from the AN dump) -> exact label used by POLITICAL_GROUPS in app.py
SIGLE_TO_GROUP = {
    "RN": "Rassemblement National (RN)",
    "EPR": "Ensemble pour la République (EPR)",
    "LFI-NFP": "La France Insoumise (LFI-NFP)",
    "SOC": "Socialistes et apparentés",
    "DR": "Droite Républicaine (DR)",
    "EcoS": "Écologiste et Social",
    "Dem": "Les Démocrates (MoDem)",
    "HOR": "Horizons & Indépendants",
    "GDR": "Gauche Démocrate et Républicaine (GDR)",
    "LIOT": "Libertés, Indépendants, Outre-mer et Territoires (LIOT)",
    "UDR": "Union des droites pour la République (UDR)",
    "NI": "Non-inscrit",
}


def circonscription(d):
    if not d.get("nom_circo"):
        return None
    circo = d["nom_circo"]
    if d.get("num_deptmt") and d.get("num_circo") is not None:
        circo += f" ({d['num_deptmt']}-{d['num_circo']})"
    return circo


def official_email(d):
    return next(
        (e["email"] for e in d.get("emails", [])
         if e.get("email", "").endswith("@assemblee-nationale.fr")),
        None,
    )


def main():
    deputes = [x["depute"] for x in json.load(open(SRC, encoding="utf-8"))["deputes"]]
    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys = ON")

    existing = {r[0] for r in db.execute("SELECT name FROM persons")}
    now = datetime.utcnow().isoformat(timespec="seconds")

    inserted, skipped, unmapped = 0, 0, []
    for d in deputes:
        name = d["nom"]
        if name in existing:
            skipped += 1
            continue
        group = SIGLE_TO_GROUP.get(d["groupe_sigle"])
        if group is None:
            unmapped.append((name, d["groupe_sigle"]))
            continue
        db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted,
                follow_up_date, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, NULL, ?)
            """,
            (name, "Député·e", group, "Inconnu",
             circonscription(d), official_email(d), now),
        )
        inserted += 1
        existing.add(name)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(f"Inserted: {inserted}  |  Skipped (already present): {skipped}")
    if unmapped:
        print(f"UNMAPPED sigles ({len(unmapped)}):", unmapped)
    print(f"persons table now holds: {total}")


if __name__ == "__main__":
    main()
