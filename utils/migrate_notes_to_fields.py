#!/usr/bin/env python3
"""One-off migration — NOT part of the web app.

Earlier imports stored a person's circonscription and email inside the free-text
`notes` column, e.g.:

    "Circonscription : Ariège (09-2) · audrey.abadie@assemblee-nationale.fr"
    "Circonscription : Côte-d'Or"

This moves that data into the dedicated `circonscription` and `email` columns
(added later) and clears it from `notes`. Rows are updated in place, so person
ids — and any meeting/mail links — are preserved.

Idempotent: only touches rows whose notes still start with "Circonscription :".

    python3 utils/migrate_notes_to_fields.py
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "meetings.db")


def parse_notes(notes):
    """Return (circonscription, email, remaining_notes) parsed from `notes`."""
    circo = email = None
    rest = []
    for part in notes.split(" · "):
        part = part.strip()
        if part.startswith("Circonscription :"):
            circo = part[len("Circonscription :"):].strip() or None
        elif "@" in part and " " not in part:
            email = part
        elif part:
            rest.append(part)
    return circo, email, (" · ".join(rest) or None)


def main():
    db = sqlite3.connect(DB)
    cols = [r[1] for r in db.execute("PRAGMA table_info(persons)")]
    if "circonscription" not in cols:
        db.execute("ALTER TABLE persons ADD COLUMN circonscription TEXT")
    if "email" not in cols:
        db.execute("ALTER TABLE persons ADD COLUMN email TEXT")

    rows = db.execute(
        "SELECT id, notes FROM persons WHERE notes LIKE 'Circonscription :%'"
    ).fetchall()

    for pid, notes in rows:
        circo, email, rest = parse_notes(notes)
        db.execute(
            "UPDATE persons SET circonscription = ?, email = ?, notes = ? WHERE id = ?",
            (circo, email, rest, pid),
        )

    db.commit()
    with_circo = db.execute(
        "SELECT COUNT(*) FROM persons WHERE circonscription IS NOT NULL"
    ).fetchone()[0]
    with_email = db.execute(
        "SELECT COUNT(*) FROM persons WHERE email IS NOT NULL"
    ).fetchone()[0]
    print(f"Rows migrated: {len(rows)}")
    print(f"persons with circonscription: {with_circo}  |  with email: {with_email}")


if __name__ == "__main__":
    main()
