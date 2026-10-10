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
- circonscription -> département / territory represented
- email           -> derived from the Sénat address convention, see senat_email()
- added_by / validated_by -> NULL (imported, not entered by a moderator)

Idempotent : une sénatrice déjà fichée n'est jamais dupliquée. Si sa fiche porte
un autre rôle (élue locale devenue sénatrice au renouvellement), le rôle
« Sénateur·ice » lui est AJOUTÉ plutôt qu'ignoré, et son e-mail comblé s'il
manquait. Voir utils/elus_roster.py pour le détail des deux défauts corrigés.
"""
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orglink import link_group  # noqa: E402
from elus_roster import (  # noqa: E402
    GROUPE_ATTENTE,
    app_constant,
    merge_roles,
    resoudre_groupe,
    verifier_groupes,
)

SRC = os.path.join(ROOT, "actual_dataset", "senateurices_actifs.json")
DB = os.path.join(ROOT, "meetings.db")
ROLE = "Sénateur·ice"
GROUPE_ATTENTE_SENAT = GROUPE_ATTENTE["Sénat"]

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


# Homonyms: when two senators derive the same address, the Sénat leaves it to
# the one in office first and spells out the newcomer's full first name
# (Pascal Martin keeps p.martin, Pauline Martin gets pauline.martin). Confirmed
# on senat.fr. Keyed by "Prénom Nom" — extend when the collision check below
# reports a new pair.
EMAIL_OVERRIDES = {
    "Pauline Martin": "pauline.martin@senat.fr",
}


def _strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    ).lower()


def senat_email(prenom, nom):
    """Sénat convention: initial of every part of the first name, a dot, then
    the family name — unaccented, lowercased, inner spaces and apostrophes
    turned into hyphens (particles kept).

        François Patriat        -> f.patriat@senat.fr
        Marie-Claire Carrère-Gée -> mc.carrere-gee@senat.fr
        Louis-Jean de Nicolaÿ   -> lj.de-nicolay@senat.fr
        Thani Mohamed Soilihi   -> t.mohamed-soilihi@senat.fr

    Checked against the addresses published on senat.fr for a 45-senator
    sample: 43 exact matches, 0 mismatches, 2 senators publishing no address
    at all. The convention is mechanical, so the address is generated for
    everyone — but a few senators publish none on their senat.fr page
    (Marie-Pierre de La Gontrie, Évelyne Perrot, Thierry Meignen among them),
    and for those the generated address may simply bounce.

    Homonyms are read from EMAIL_OVERRIDES rather than derived.
    """
    override = EMAIL_OVERRIDES.get(f"{prenom} {nom}".strip())
    if override:
        return override
    initials = "".join(p[0] for p in re.split(r"[-\s']+", _strip_accents(prenom)) if p)
    family = re.sub(r"[\s']+", "-", _strip_accents(nom).strip())
    if not initials or not family:
        return None
    return f"{initials}.{family}@senat.fr"


def main():
    verifier_groupes("Sénat", GROUPE_TO_GROUP)
    roles_order = app_constant("POLITICAL_ROLES")
    # Groupes « fourre-tout » : sur une fiche qui en porte un, le groupe
    # sénatorial est une information meilleure, on la pose. Un vrai groupe
    # d'une autre chambre, en revanche, n'est jamais écrasé.
    groupes_neutres = set(app_constant("POLITICAL_GROUPS")["Autre"])
    if ROLE not in roles_order:
        raise SystemExit(f"{ROLE!r} absent de POLITICAL_ROLES dans app.py")

    senateurices = json.load(open(SRC, encoding="utf-8"))

    # Two senators sharing an address would send mail to the wrong person.
    by_email = {}
    for s in senateurices:
        by_email.setdefault(senat_email(s["prenom"], s["nom"]), []).append(
            f"{s['prenom']} {s['nom']}"
        )
    clashes = {e: n for e, n in by_email.items() if len(n) > 1}
    if clashes:
        raise SystemExit(
            "Address collision — check senat.fr and add the newcomer to "
            "EMAIL_OVERRIDES: " + repr(clashes)
        )

    db = sqlite3.connect(DB)
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")

    # id + rôle, et non le seul nom : c'est ce qui permet de voir qu'une fiche
    # existante ne porte pas encore le mandat sénatorial.
    existing = {
        r[0]: (r[1], r[2]) for r in db.execute("SELECT name, id, role FROM persons")
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    inserted, skipped, backfilled, unmapped = 0, 0, 0, []
    promus, sans_groupe_decl = [], []
    for s in senateurices:
        name = f"{s['prenom']} {s['nom']}".strip()
        email = senat_email(s["prenom"], s["nom"])
        short = (s.get("groupe") or {}).get("libelleCourt")
        group, anomalie = resoudre_groupe(short, GROUPE_TO_GROUP, "Sénat")
        if anomalie:
            unmapped.append((name, anomalie))
        elif group == GROUPE_ATTENTE_SENAT:
            sans_groupe_decl.append(name)
        circo = (s.get("circonscription") or {}).get("libelle")

        if name in existing:
            pid, role_actuel = existing[name]
            fusion = merge_roles(role_actuel, ROLE, roles_order)
            if fusion != (role_actuel or ""):
                # Élu·e déjà fiché·e sous un autre mandat : on AJOUTE le mandat
                # sénatorial au lieu de l'ignorer, et on complète ce qui manque
                # sans jamais écraser une valeur saisie à la main.
                ancien_groupe = db.execute(
                    "SELECT political_group FROM persons WHERE id = ?", (pid,)
                ).fetchone()[0]
                groupe_final = (
                    group
                    if not ancien_groupe or ancien_groupe in groupes_neutres
                    else ancien_groupe
                )
                db.execute(
                    "UPDATE persons SET role = ?, political_group = ?,"
                    " circonscription = COALESCE(NULLIF(circonscription, ''), ?),"
                    " email = COALESCE(NULLIF(email, ''), ?) WHERE id = ?",
                    (fusion, groupe_final, circo, email, pid),
                )
                link_group(db, pid, group, "Sénat")
                promus.append(f"{name} : {role_actuel or '—'} -> {fusion}")
            else:
                skipped += 1
            if email:
                backfilled += db.execute(
                    "UPDATE persons SET email = ? "
                    "WHERE name = ? AND role LIKE ? "
                    "AND (email IS NULL OR email = '')",
                    (email, name, f"%{ROLE}%"),
                ).rowcount
            continue
        cur = db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?)
            """,
            (name, ROLE, group, "Inconnu", circo, email, now),
        )
        # The fiche's groupe politique is an organisation now, not just this
        # column: link it so the person shows their group straight away.
        link_group(db, cur.lastrowid, group, "Sénat")
        inserted += 1
        existing[name] = (cur.lastrowid, ROLE)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(
        f"Inserted: {inserted}  |  Skipped (already present): {skipped}"
        f"  |  Emails backfilled: {backfilled}"
    )
    if promus:
        print(f"\nMandat sénatorial ajouté à {len(promus)} fiche(s) existante(s) :")
        for ligne in promus:
            print(f"  {ligne}")
    if sans_groupe_decl:
        # Normal après un renouvellement : ce n'est pas une anomalie, mais ça
        # doit se voir, sinon personne ne pense à repasser quand les groupes
        # sont constitués.
        print(
            f"\n{len(sans_groupe_decl)} sénateur·ice(s) sans groupe déclaré, "
            f"rangé·es dans « {GROUPE_ATTENTE_SENAT} » : "
            + ", ".join(sans_groupe_decl[:10])
            + (" …" if len(sans_groupe_decl) > 10 else "")
        )
    print(f"persons table now holds: {total}")
    if unmapped:
        # Un libellé inconnu n'est pas un élu sans groupe : c'est la source qui
        # a changé de vocabulaire. On échoue, pour que l'alerte parte et que la
        # table GROUPE_TO_GROUP soit complétée.
        raise SystemExit(
            f"Libellés de groupe inconnus ({len(unmapped)}), "
            f"à ajouter à GROUPE_TO_GROUP : {unmapped!r}"
        )


if __name__ == "__main__":
    main()
