#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Run it by hand to insert the sitting French MEPs from
actual_dataset/eurodeputes_fr.json into the app's `persons` table:

    python3 utils/extract_eurodeputes.py     # refresh the dataset first
    python3 utils/insert_eurodeputes.py

Mapping applied per MEP:
- role            -> "Député·e européen·ne"
- political_group -> mapped from the EP group code to the app's exact
                     POLITICAL_GROUPS label (see GROUPE_TO_GROUP)
- stance          -> "Inconnu" (unknown until someone contacts them)
- email           -> as published by the Parliament (not derived, unlike the
                     Sénat addresses in insert_senateurices.py)
- circonscription -> NULL: French MEPs are elected on a single national list,
                     so there is no territory to record, the same way the
                     government rows carry no circonscription
- added_by / validated_by -> NULL (imported, not entered by a moderator)

Safe to re-run after a European election: a member already present is skipped
(their email is still backfilled if missing), and one who has left the
Parliament is reported but never deleted — they may have meetings recorded
against them, and that history is worth keeping.
"""
import ast
import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "actual_dataset", "eurodeputes_fr.json")
DB = os.path.join(ROOT, "meetings.db")

ROLE = "Député·e européen·ne"

# EP group code (`api:political-group` in the open-data API) -> exact
# POLITICAL_GROUPS["Parlement européen"] label in app.py. Groups are formed
# anew after each election, so an unknown code stops the run rather than
# quietly filing someone under the wrong banner.
GROUPE_TO_GROUP = {
    "PPE": "Parti populaire européen (PPE)",
    "S&D": "Alliance progressiste des Socialistes et Démocrates (S&D)",
    "Renew": "Renew Europe",
    "Verts/ALE": "Verts/ALE",
    "ECR": "Conservateurs et Réformistes européens (CRE)",
    "The Left": "The Left (GUE/NGL)",
    "PfE": "Patriotes pour l'Europe",
    "ESN": "Europe des Nations Souveraines (ESN)",
    "NI": "Non-inscrit (Parlement européen)",
}


def app_labels(chamber):
    """Read POLITICAL_GROUPS / ROLES from app.py as plain text (no import — the
    app pulls in heavy deps). A safety check when this is run by hand, so a
    label renamed in app.py can't silently produce uneditable rows."""
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    groups = roles = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if getattr(target, "id", None) == "POLITICAL_GROUPS":
                groups = ast.literal_eval(node.value)
            elif getattr(target, "id", None) == "ROLES":
                roles = ast.literal_eval(node.value)
    if groups is None or roles is None:
        raise RuntimeError("POLITICAL_GROUPS or ROLES not found in app.py")
    return set(groups[chamber]), set(roles)


def main():
    valid_groups, valid_roles = app_labels("Parlement européen")
    if ROLE not in valid_roles:
        raise SystemExit(f"{ROLE!r} is missing from ROLES in app.py")
    bad = {k: v for k, v in GROUPE_TO_GROUP.items() if v not in valid_groups}
    if bad:
        raise SystemExit(
            "Mapping targets not present in POLITICAL_GROUPS['Parlement "
            "européen']: " + repr(bad)
        )

    meps = json.load(open(SRC, encoding="utf-8"))

    unknown = sorted({m["groupe"] for m in meps if m["groupe"] not in GROUPE_TO_GROUP})
    if unknown:
        raise SystemExit(
            "Unknown EP political group(s) — add them to app.py and to "
            "GROUPE_TO_GROUP: " + repr(unknown)
        )

    clashes = {}
    for m in meps:
        clashes.setdefault(m["email"], []).append(m["nom_complet"])
    clashes = {e: n for e, n in clashes.items() if e and len(n) > 1}
    if clashes:
        raise SystemExit("Two MEPs share an address: " + repr(clashes))

    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys = ON")

    existing = {r[0] for r in db.execute("SELECT name FROM persons")}
    seen = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    inserted = skipped = backfilled = 0
    for m in meps:
        name = m["nom_complet"]
        seen.add(name)
        if name in existing:
            skipped += 1
            if m["email"]:
                backfilled += db.execute(
                    "UPDATE persons SET email = ? WHERE name = ? AND role = ? "
                    "AND (email IS NULL OR email = '')",
                    (m["email"], name, ROLE),
                ).rowcount
            continue
        db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted,
                follow_up_date, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, ?)
            """,
            (name, ROLE, GROUPE_TO_GROUP[m["groupe"]], "Inconnu", m["email"], now),
        )
        inserted += 1
        existing.add(name)

    db.commit()

    # People filed as MEPs who are no longer in the Parliament's current list:
    # report, never delete — their meeting history stays.
    gone = [
        r[0]
        for r in db.execute("SELECT name FROM persons WHERE role = ?", (ROLE,))
        if r[0] not in seen
    ]
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(
        f"Inserted: {inserted}  |  Skipped (already present): {skipped}"
        f"  |  Emails backfilled: {backfilled}"
    )
    if gone:
        print(
            f"Plus au Parlement ({len(gone)}) — conservé·es, à vérifier à la "
            f"main: {', '.join(gone)}"
        )
    print(f"persons table now holds: {total}")


if __name__ == "__main__":
    main()
