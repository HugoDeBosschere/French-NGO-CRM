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
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orglink import link_group  # noqa: E402
# Une seule définition de « cette URL vient-elle d'une source
# officielle ? », partagée avec la synchro des député·es et des
# sénateur·ices : deux copies divergeraient.
from sync_emails_from_elus import pose_par_le_script  # noqa: E402
from civicrm import norm_name  # noqa: E402
from elus_roster import (  # noqa: E402
    GROUPE_ATTENTE,
    app_constant,
    merge_roles,
    resoudre_groupe,
)

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
    """Read POLITICAL_GROUPS / POLITICAL_ROLES from app.py as plain text (no
    import — the
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
            # POLITICAL_ROLES, not ROLES: the latter is a concatenation of
            # the political and journalist lists since the merge, which
            # literal_eval cannot evaluate. A MEP's role is political.
            elif getattr(target, "id", None) == "POLITICAL_ROLES":
                roles = ast.literal_eval(node.value)
    if groups is None or roles is None:
        raise RuntimeError(
            "POLITICAL_GROUPS or POLITICAL_ROLES not found in app.py")
    return set(groups[chamber]), set(roles)



# Les colonnes de portrait et de fiche officielle n'existent que depuis la
# migration d'octobre 2026 : une base plus ancienne, ou un test, peut ne pas
# les avoir, et le script ne doit pas tomber pour autant.
def _colonnes_medias(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(persons)")}
    return {"photo_url", "fiche_url"} <= cols


def poser_medias(db, person_id, photo, fiche):
    """Portrait et fiche officielle, remplis s'ils manquent, rafraîchis s'ils
    viennent déjà du Parlement européen.

    Même règle que sync_emails_from_elus.py pour les député·es et les
    sénateur·ices : on ne remplace que ce que nos propres scripts ont posé, et
    jamais une valeur saisie à la main. L'adresse du portrait change quand la
    photo officielle change.
    """
    if not _colonnes_medias(db):
        return 0
    actuel = db.execute(
        "SELECT photo_url, fiche_url FROM persons WHERE id = ?",
        (person_id,)).fetchone() or (None, None)
    neufs = []
    for colonne, valeur, ancien in (("photo_url", photo, actuel[0]),
                                    ("fiche_url", fiche, actuel[1])):
        if not valeur:
            continue
        ancien = (ancien or "").strip()
        # `pose_par_le_script` plutôt qu'un test maison : il compare l'HÔTE et
        # non la fin de la chaîne. Un « in » laissait passer
        # evil-europarl.europa.eu.pirate.fr, attrapé par un test.
        if not ancien or (pose_par_le_script(ancien) and ancien != valeur):
            neufs.append((colonne, valeur))
    if not neufs:
        return 0
    db.execute("UPDATE persons SET " + ", ".join(f"{c} = ?" for c, _v in neufs)
               + " WHERE id = ?", [v for _c, v in neufs] + [person_id])
    return 1

def main():
    valid_groups, valid_roles = app_labels("Parlement européen")
    if ROLE not in valid_roles:
        raise SystemExit(f"{ROLE!r} is missing from POLITICAL_ROLES in app.py")
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
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")

    # Même correctif que pour les deux autres chambres (utils/elus_roster.py) :
    # on lit le rôle, pour voir qu'une fiche existante n'a pas encore le mandat.
    # Uniquement les fiches « Politique » : sinon le rapprochement par le seul
    # nom donne le mandat à un homonyme d'un autre type (voir insert_deputes.py,
    # « Laurent Alexandre »). Le type par défaut des fiches importées étant
    # 'Politique', l'idempotence des imports précédents est préservée.
    existing = {
        r[0]: (r[1], r[2]) for r in db.execute(
            "SELECT name, id, role FROM persons WHERE contact_type = 'Politique'")
    }
    # Un second index, insensible aux accents et à la ponctuation. L'API du
    # Parlement écrit « Leila Chaibi » là où le CRM a « Leïla Chaibi » : la
    # comparaison stricte en faisait deux personnes, créait une fiche en
    # double et annonçait l'originale comme partie du Parlement. Constaté sur
    # la base de production. Même normalisation que le script de fusion.
    par_nom_normalise = {}
    for nom, (pid, role) in existing.items():
        par_nom_normalise.setdefault(norm_name(nom), (nom, pid, role))
    roles_order = app_constant("POLITICAL_ROLES")
    groupes_neutres = set(app_constant("POLITICAL_GROUPS")["Autre"])
    seen = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    inserted = skipped = backfilled = medias = 0
    promus, unmapped, sans_groupe_decl = [], [], []
    for m in meps:
        name = m["nom_complet"]
        seen.add(name)
        # Un groupe inconnu ne doit plus planter l'import au milieu : la
        # personne entre avec le groupe d'attente, et l'anomalie est signalée
        # à la fin, une fois tout le monde enregistré.
        group, anomalie = resoudre_groupe(
            m["groupe"], GROUPE_TO_GROUP, "Parlement européen"
        )
        if anomalie:
            unmapped.append((name, anomalie))
        elif group == GROUPE_ATTENTE["Parlement européen"]:
            sans_groupe_decl.append(name)

        proche = par_nom_normalise.get(norm_name(name))
        if name not in existing and proche:
            # Même personne, orthographe différente : on garde la fiche et son
            # historique, et on note le nom du CRM comme « vu », sinon il
            # serait annoncé comme ayant quitté le Parlement.
            name = proche[0]
            seen.add(name)
        if name in existing:
            pid, role_actuel = existing[name]
            # Avant toute autre considération : le portrait et la fiche ne
            # dépendent ni du rôle ni du groupe. Posés plus bas, ils ne
            # touchaient que les fiches dont le mandat changeait — mesuré sur
            # la base de production : 1 portrait pour 81 eurodéputé·es, les 80
            # autres étant « déjà présentes » et sortant par `continue`.
            medias += poser_medias(db, pid, m.get("photo"), m.get("url"))
            fusion = merge_roles(role_actuel, ROLE, roles_order)
            if fusion != (role_actuel or ""):
                ancien_groupe = db.execute(
                    "SELECT political_group FROM persons WHERE id = ?", (pid,)
                ).fetchone()[0]
                db.execute(
                    "UPDATE persons SET role = ?, political_group = ?,"
                    " email = COALESCE(NULLIF(email, ''), ?) WHERE id = ?",
                    (
                        fusion,
                        group if not ancien_groupe or ancien_groupe in groupes_neutres
                        else ancien_groupe,
                        m["email"],
                        pid,
                    ),
                )
                link_group(db, pid, group, "Parlement européen")
                promus.append(f"{name} : {role_actuel or '—'} -> {fusion}")
            else:
                skipped += 1
            if m["email"]:
                backfilled += db.execute(
                    "UPDATE persons SET email = ? WHERE name = ? AND role LIKE ? "
                    "AND (email IS NULL OR email = '')",
                    (m["email"], name, f"%{ROLE}%"),
                ).rowcount
            continue
        cur = db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, ?)
            """,
            (name, ROLE, group, "Inconnu", m["email"], now),
        )
        link_group(db, cur.lastrowid, group, "Parlement européen")
        medias += poser_medias(db, cur.lastrowid, m.get("photo"), m.get("url"))
        inserted += 1
        existing[name] = (cur.lastrowid, ROLE)
        par_nom_normalise.setdefault(norm_name(name), (name, cur.lastrowid, ROLE))

    db.commit()

    # Celles et ceux que CE script a importés et qui ne sont plus dans la liste :
    # signalés, jamais supprimés, leur historique de rencontres reste.
    #
    # Le filtre sur `fiche_url` n'est pas cosmétique. La source ne contient que
    # les 81 élu·es FRANÇAIS·ES : un·e eurodéputé·e d'un autre pays, ajouté·e à
    # la main après une rencontre, n'y figure pas et était donc annoncé·e
    # « plus au Parlement » à chaque passage. Estelle Ceulemans, élue belge en
    # exercice depuis juillet 2024, l'a été tous les lundis pendant des
    # semaines. Or ce script ne sait pas qui a quitté le Parlement, il sait
    # seulement qui manque à la liste française.
    #
    # Le discriminant est la SAISIE HUMAINE, pas le lien officiel. On avait
    # d'abord filtré sur `fiche_url` : mauvaise idée, parce qu'on veut poser le
    # lien officiel et le portrait des élu·es d'autres pays aussi, et le jour où
    # on le fait la fiche repasse le filtre et l'alerte revient. C'est arrivé
    # dans l'heure qui a suivi le premier correctif.
    #
    # `added_by` / `validated_by` disent qu'un humain a créé ou validé la fiche,
    # donc qu'elle ne vient pas d'ici : c'est déjà la convention retenue par la
    # réconciliation `in_office` de sync_officials.py, pour la même raison.
    #
    # Limite assumée : un·e eurodéputé·e français·e dont la fiche a été validée
    # à la main et qui quitte ensuite le Parlement ne sera pas signalé·e. Mieux
    # vaut un départ manqué, que la liste des élu·es en exercice finit par
    # rattraper, qu'une fausse alerte hebdomadaire que plus personne ne lit.
    gone = [
        nom
        for nom, fiche, ajout, valid in db.execute(
            "SELECT name, fiche_url, added_by, validated_by FROM persons "
            "WHERE role = ?", (ROLE,))
        if nom not in seen and pose_par_le_script(fiche)
        and ajout is None and valid is None
    ]
    total = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(
        f"Inserted: {inserted}  |  Skipped (already present): {skipped}"
        f"  |  Emails backfilled: {backfilled}"
        f"  |  Portraits / fiches: {medias}"
    )
    if gone:
        print(
            f"Absent·es de la liste française ({len(gone)}) — conservé·es, à vérifier à la "
            f"main: {', '.join(gone)}"
        )
    if promus:
        print(f"\nMandat de député·e européen·ne ajouté à {len(promus)} fiche(s) :")
        for ligne in promus:
            print(f"  {ligne}")
    if sans_groupe_decl:
        print(
            f"\n{len(sans_groupe_decl)} eurodéputé·e(s) sans groupe déclaré, "
            f"rangé·es dans « {GROUPE_ATTENTE['Parlement européen']} » : "
            + ", ".join(sans_groupe_decl[:10])
            + (" …" if len(sans_groupe_decl) > 10 else "")
        )
    print(f"persons table now holds: {total}")
    if unmapped:
        raise SystemExit(
            f"Groupes inconnus ({len(unmapped)}), "
            f"à ajouter à GROUPE_TO_GROUP : {unmapped!r}"
        )


if __name__ == "__main__":
    main()
