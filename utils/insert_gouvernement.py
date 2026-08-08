#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Inserts the members of the sitting government (produced by
utils/extract_gouvernement.py) into the app's `persons` table:

    python3 utils/extract_gouvernement.py     # refresh the JSON first
    python3 utils/insert_gouvernement.py

Unlike the députés/sénateurices imports, this one does NOT simply skip a person
who already exists. Ministers are usually sitting députés who were imported
earlier, so skipping would leave them looking like plain deputies forever.
Instead, for an existing person this script *adds* the government role to the
roles they already hold and fills in their portefeuille:

    "Député·e"  ->  "Ministre, Député·e"

Their `political_group` is left untouched — the annuaire carries no party
information at all, so the group already on the record (their real party, from
the AN/Sénat import) is strictly better than anything this script could set.

New people (ministers who hold no parliamentary seat) are created with
political_group "Gouvernement / Administration", since nothing better is known.

Idempotent: re-running adds no duplicate role and creates no duplicate person.
"""
import ast
import json
import os
import sqlite3
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "actual_dataset", "gouvernement.json")
DB = os.path.join(ROOT, "meetings.db")

# Group used only for members who aren't already in `persons`.
DEFAULT_GROUP = "Gouvernement / Administration"
ROLE_SEP = ", "


def app_constant(name):
    """Read a top-level list constant from app.py as plain text (no import —
    the app pulls in heavy deps). A safety check when run by hand, so a label
    renamed in app.py can't silently produce rows the form can't edit."""
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} not found in app.py")


def merge_roles(existing, new_role, order):
    """Add `new_role` to a comma-joined role list, keeping ROLES order."""
    roles = {r.strip() for r in (existing or "").split(",") if r.strip()}
    roles.add(new_role)
    known = [r for r in order if r in roles]
    # Preserve anything unknown (hand-typed before the role list changed).
    return ROLE_SEP.join(known + sorted(roles - set(order)))


def main():
    roles_order = app_constant("ROLES")
    groups = app_constant("POLITICAL_GROUPS")

    membres = [m["membre"] for m in json.load(open(SRC, encoding="utf-8"))["gouvernement"]]

    unknown = sorted({m["role"] for m in membres} - set(roles_order))
    if unknown:
        raise SystemExit(f"Roles absent from ROLES in app.py: {unknown}")
    if DEFAULT_GROUP not in groups["Autre"]:
        raise SystemExit(f"{DEFAULT_GROUP!r} missing from POLITICAL_GROUPS['Autre']")

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    existing = {
        r["name"]: r
        for r in db.execute("SELECT id, name, role, political_group FROM persons")
    }
    now = datetime.utcnow().isoformat(timespec="seconds")

    created, updated, unchanged = [], [], 0
    for m in membres:
        name = m["nom_complet"]
        row = existing.get(name)
        if row is None:
            db.execute(
                """
                INSERT INTO persons (
                    name, role, portefeuille, political_group, stance,
                    first_contacted, follow_up_date, notes, circonscription,
                    email, added_by, validated_by, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, ?)
                """,
                (name, m["role"], m["portefeuille"], DEFAULT_GROUP, "Inconnu",
                 m["email"], now),
            )
            created.append(f"{name} — {m['role']}")
            continue

        merged = merge_roles(row["role"], m["role"], roles_order)
        if merged == (row["role"] or ""):
            unchanged += 1
            continue
        db.execute(
            "UPDATE persons SET role = ?, portefeuille = ? WHERE id = ?",
            (merged, m["portefeuille"], row["id"]),
        )
        updated.append(f"{name}: {row['role'] or '—'} -> {merged}  [{row['political_group']}]")

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]

    print(f"Created {len(created)} new person(s):")
    for line in created:
        print(f"  + {line}")
    print(f"\nAdded a government role to {len(updated)} existing person(s):")
    for line in updated:
        print(f"  ~ {line}")
    print(f"\nAlready up to date: {unchanged}")
    print(f"persons table now holds: {total}")


if __name__ == "__main__":
    main()
