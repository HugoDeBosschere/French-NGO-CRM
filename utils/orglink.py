"""Link a bulk-imported politique to their groupe politique organisation.

Since the journalist CRM was merged in, a person's organisation lives in
`person_organisations` and `persons.political_group` is only its mirror (see
_sync_group_mirror in app.py). The importers here know the group's *name*, so
they call `link_group` right after inserting, and the fiche shows its groupe
politique immediately rather than only after the app is next restarted.

app.py's `_seed_organisations_from_groups` reconciles anything that slips
through on startup, so this is belt and braces — but the belt matters: these
scripts run on systemd timers, days apart from any restart.
"""

from datetime import datetime, timezone

# Kept in step with app.py's POLITICAL_GROUPS. A group absent from it — typed by
# hand, or newly formed — simply gets no chambre, exactly as in the app.
CHAMBERS = {
    "Assemblée nationale", "Sénat", "Parlement européen", "Autre",
}


def link_group(db, person_id, group_name, chambre=None):
    """Attach `person_id` to the groupe politique called `group_name`.

    Creates the organisation if this is the first person in it. Returns its id.
    Safe to call twice: the link is INSERT OR IGNORE and the lookup is by name.
    """
    if not (group_name or "").strip():
        return None
    group_name = group_name.strip()
    row = db.execute(
        "SELECT id FROM organisations WHERE org_type = 'Groupe politique' AND name = ?",
        (group_name,),
    ).fetchone()
    if row is None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        org_id = db.execute(
            """
            INSERT INTO organisations (name, org_type, chambre, stance, created_at)
            VALUES (?, 'Groupe politique', ?, 'Inconnu', ?)
            """,
            (group_name, chambre if chambre in CHAMBERS else None, now),
        ).lastrowid
    else:
        org_id = row[0]
    db.execute(
        "INSERT OR IGNORE INTO person_organisations (person_id, organisation_id) "
        "VALUES (?, ?)",
        (person_id, org_id),
    )
    return org_id
