#!/usr/bin/env python3
"""One-off import of the journalist CRM (Website_media) into this database.

The two apps were merged into this one: `journalists` becomes the
« Journaliste » half of `persons`, `media` becomes the « Média » half of
`organisations`, and the contenus and interventions recorded against a media
follow. Political groups are already organisations by the time this runs — see
_seed_organisations_from_groups in app.py.

Idempotent by name: a média or a journaliste whose name is already present is
skipped rather than duplicated, so a rerun after a partial import finishes the
job instead of doubling it. Names are compared casefolded and
whitespace-collapsed, the same rule the moderation matcher uses.

    uv run python utils/import_media_crm.py path/to/media/meetings.db [--commit]

Without --commit nothing is written: the script reports what it would do.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "meetings.db"

# The journalist CRM had no notion of a chambre, and its médias carry an
# orientation; the columns line up one-to-one apart from that.
MEDIA_COLUMNS = ("name", "media_type", "orientation", "stance", "link", "notes")
JOURNALIST_COLUMNS = ("name", "role", "stance", "first_contacted", "email",
                      "phone", "social_links", "notes")


def key(name):
    """Comparison key for a name: casefolded, whitespace-collapsed."""
    return " ".join((name or "").split()).casefold()


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def moderator_map(dst, src):
    """Map source moderator ids onto this database's, matching on name.

    A name that exists here is reused — the two apps were run by the same
    handful of people, and duplicating them would split every provenance field.
    One that does not is created, so « Saisi par » keeps pointing at a real
    person rather than going NULL.
    """
    here = {key(r[1]): r[0] for r in dst.execute("SELECT id, name FROM moderators")}
    mapping = {}
    for mid, name in src.execute("SELECT id, name FROM moderators"):
        k = key(name)
        if k not in here:
            here[k] = dst.execute(
                "INSERT INTO moderators (name) VALUES (?)", (name,)
            ).lastrowid
        mapping[mid] = here[k]
    return mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="the journalist CRM's meetings.db")
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it, only report")
    args = parser.parse_args()

    src_path = Path(args.source)
    if not src_path.exists():
        sys.exit(f"Source introuvable : {src_path}")

    dst = sqlite3.connect(DB_PATH)
    dst.row_factory = sqlite3.Row
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    mods = moderator_map(dst, src)

    def mod(value):
        return mods.get(value)

    report = []

    # --- Médias -> organisations ------------------------------------------- #
    existing_orgs = {
        key(r["name"]): r["id"]
        for r in dst.execute("SELECT id, name FROM organisations "
                             "WHERE org_type = 'Média'")
    }
    media_ids = {}   # source media id -> organisation id here
    added_orgs = 0
    for row in src.execute("SELECT * FROM media ORDER BY id"):
        k = key(row["name"])
        if k in existing_orgs:
            media_ids[row["id"]] = existing_orgs[k]
            continue
        cur = dst.execute(
            """
            INSERT INTO organisations (name, org_type, media_type, orientation,
                stance, link, notes, added_by, validated_by, created_at)
            VALUES (?, 'Média', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["name"], row["media_type"], row["orientation"], row["stance"],
             row["link"], row["notes"], mod(row["added_by"]),
             mod(row["validated_by"]), row["created_at"] or now()),
        )
        media_ids[row["id"]] = existing_orgs[k] = cur.lastrowid
        added_orgs += 1
    report.append(f"organisations « Média » ajoutées : {added_orgs}")

    # --- Journalistes -> persons ------------------------------------------- #
    # Matched among journalistes only. A journaliste and a politique can
    # genuinely share a name — « Laurent Alexandre » is both an LFI député and
    # a chroniqueur — and merging them into one fiche would be wrong. Scoping
    # the key by contact_type keeps them apart and keeps a rerun idempotent.
    existing_people = {
        key(r["name"]): r["id"] for r in dst.execute(
            "SELECT id, name FROM persons WHERE contact_type = 'Journaliste'")
    }
    person_ids = {}
    added_people = 0
    for row in src.execute("SELECT * FROM journalists ORDER BY id"):
        k = key(row["name"])
        if k in existing_people:
            # Already imported: reuse that fiche rather than create a second.
            person_ids[row["id"]] = existing_people[k]
            continue
        cur = dst.execute(
            """
            INSERT INTO persons (name, contact_type, role, stance, first_contacted,
                email, phone, social_links, notes, added_by, validated_by, created_at)
            VALUES (?, 'Journaliste', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["name"], row["role"], row["stance"], row["first_contacted"],
             row["email"], row["phone"], row["social_links"], row["notes"],
             mod(row["added_by"]), mod(row["validated_by"]),
             row["created_at"] or now()),
        )
        person_ids[row["id"]] = existing_people[k] = cur.lastrowid
        added_people += 1
    report.append(f"personnes « Journaliste » ajoutées : {added_people}")

    # --- journalist_media -> person_organisations -------------------------- #
    links = 0
    for row in src.execute("SELECT * FROM journalist_media"):
        pid, oid = person_ids.get(row["journalist_id"]), media_ids.get(row["media_id"])
        if pid and oid:
            dst.execute(
                "INSERT OR IGNORE INTO person_organisations (person_id, organisation_id) "
                "VALUES (?, ?)", (pid, oid))
            links += 1
    report.append(f"liens personne ↔ organisation : {links}")

    # --- Interventions and contenus ---------------------------------------- #
    def copy_records(table, join_table, date_col, columns, mod_join=None):
        """Copy one record table and its journalist (and moderator) links."""
        copied = 0
        for row in src.execute(f"SELECT * FROM {table} ORDER BY id"):
            oid = media_ids.get(row["media_id"])
            if oid is None:
                continue
            # Same link and same date means the same record: reruns skip it.
            if dst.execute(
                f"SELECT 1 FROM {table} WHERE link = ? AND {date_col} = ?",
                (row["link"], row[date_col]),
            ).fetchone():
                continue
            placeholders = ", ".join("?" for _ in columns)
            cur = dst.execute(
                f"""
                INSERT INTO {table} (organisation_id, {", ".join(columns)},
                    recorded_by, validated_by, created_at)
                VALUES (?, {placeholders}, ?, ?, ?)
                """,
                (oid, *[row[c] for c in columns], mod(row["recorded_by"]),
                 mod(row["validated_by"]), row["created_at"] or now()),
            )
            new_id = cur.lastrowid
            fk = f"{table[:-1]}_id" if table.endswith("s") else f"{table}_id"
            for link in src.execute(
                f"SELECT * FROM {join_table} WHERE {fk} = ?", (row["id"],)
            ):
                pid = person_ids.get(link["journalist_id"])
                if pid:
                    dst.execute(
                        f"INSERT OR IGNORE INTO {table[:-1]}_persons "
                        f"({fk}, person_id) VALUES (?, ?)", (new_id, pid))
            if mod_join:
                for link in src.execute(
                    f"SELECT * FROM {mod_join} WHERE {fk} = ?", (row["id"],)
                ):
                    mid = mod(link["moderator_id"])
                    if mid:
                        dst.execute(
                            f"INSERT OR IGNORE INTO {table[:-1]}_moderators "
                            f"({fk}, moderator_id) VALUES (?, ?)", (new_id, mid))
            copied += 1
        return copied

    n = copy_records(
        "interventions", "intervention_journalists", "intervention_date",
        ("intervention_date", "intervention_type", "link", "summary"),
        mod_join="intervention_moderators")
    report.append(f"interventions copiées : {n}")

    n = copy_records(
        "contents", "content_journalists", "published_on",
        ("content_type", "link", "published_on", "summary"))
    report.append(f"contenus copiés : {n}")

    # A journaliste has no groupe politique, so their mirror column must stay
    # NULL; the inserts above never set it, and this asserts it.
    stray = dst.execute(
        "SELECT COUNT(*) FROM persons WHERE contact_type = 'Journaliste' "
        "AND political_group IS NOT NULL"
    ).fetchone()[0]
    report.append(f"journalistes avec un groupe politique résiduel : {stray}")

    print("\n".join(report))
    if args.commit:
        dst.commit()
        print("\n→ écrit dans", DB_PATH)
    else:
        dst.rollback()
        print("\n→ simulation (relancer avec --commit pour écrire)")
    dst.close()
    src.close()


if __name__ == "__main__":
    main()
