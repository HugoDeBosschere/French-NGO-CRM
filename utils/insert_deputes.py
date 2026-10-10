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
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orglink import link_group  # noqa: E402
from elus_roster import (  # noqa: E402
    GROUPE_ATTENTE,
    app_constant,
    merge_roles,
    resoudre_groupe,
    verifier_groupes,
)

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


CHAMBRE = "Assemblée nationale"
ROLE = "Député·e"


def main():
    # Mêmes deux défauts que pour les sénateurices, voir utils/elus_roster.py :
    # un sigle inconnu faisait disparaître la personne, et une fiche existante
    # ne recevait jamais le mandat de députée.
    verifier_groupes(CHAMBRE, SIGLE_TO_GROUP)
    roles_order = app_constant("POLITICAL_ROLES")
    if ROLE not in roles_order:
        raise SystemExit(f"{ROLE!r} absent de POLITICAL_ROLES dans app.py")
    groupes_neutres = set(app_constant("POLITICAL_GROUPS")["Autre"])

    deputes = [x["depute"] for x in json.load(open(SRC, encoding="utf-8"))["deputes"]]
    db = sqlite3.connect(DB)
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")

    existing = {
        r[0]: (r[1], r[2]) for r in db.execute("SELECT name, id, role FROM persons")
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    inserted, skipped, unmapped = 0, 0, []
    promus, sans_groupe_decl = [], []
    for d in deputes:
        name = d["nom"]
        group, anomalie = resoudre_groupe(d["groupe_sigle"], SIGLE_TO_GROUP, CHAMBRE)
        if anomalie:
            unmapped.append((name, anomalie))
        elif group == GROUPE_ATTENTE[CHAMBRE]:
            sans_groupe_decl.append(name)

        if name in existing:
            pid, role_actuel = existing[name]
            fusion = merge_roles(role_actuel, ROLE, roles_order)
            if fusion == (role_actuel or ""):
                skipped += 1
                continue
            ancien_groupe = db.execute(
                "SELECT political_group FROM persons WHERE id = ?", (pid,)
            ).fetchone()[0]
            db.execute(
                "UPDATE persons SET role = ?, political_group = ?,"
                " circonscription = COALESCE(NULLIF(circonscription, ''), ?),"
                " email = COALESCE(NULLIF(email, ''), ?) WHERE id = ?",
                (
                    fusion,
                    group if not ancien_groupe or ancien_groupe in groupes_neutres
                    else ancien_groupe,
                    circonscription(d),
                    official_email(d),
                    pid,
                ),
            )
            link_group(db, pid, group, CHAMBRE)
            promus.append(f"{name} : {role_actuel or '—'} -> {fusion}")
            continue

        cur = db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?)
            """,
            (name, ROLE, group, "Inconnu",
             circonscription(d), official_email(d), now),
        )
        # The fiche's groupe politique is an organisation now, not just this
        # column: link it so the person shows their group straight away.
        link_group(db, cur.lastrowid, group, CHAMBRE)
        inserted += 1
        existing[name] = (cur.lastrowid, ROLE)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(f"Inserted: {inserted}  |  Skipped (already present): {skipped}")
    if promus:
        print(f"\nMandat de député·e ajouté à {len(promus)} fiche(s) existante(s) :")
        for ligne in promus:
            print(f"  {ligne}")
    if sans_groupe_decl:
        print(
            f"\n{len(sans_groupe_decl)} député·e(s) sans groupe déclaré, "
            f"rangé·es dans « {GROUPE_ATTENTE[CHAMBRE]} » : "
            + ", ".join(sans_groupe_decl[:10])
            + (" …" if len(sans_groupe_decl) > 10 else "")
        )
    print(f"persons table now holds: {total}")
    if unmapped:
        raise SystemExit(
            f"Sigles de groupe inconnus ({len(unmapped)}), "
            f"à ajouter à SIGLE_TO_GROUP : {unmapped!r}"
        )


if __name__ == "__main__":
    main()
