#!/usr/bin/env python3
"""Fusionner les fiches en double d'une même personne, sans rien perdre.

Trois fiches « Yannick Jadot » liées au même courriel, « Anne-Flore Gaspard »
deux fois et « Anne-Flaure Gaspard » une troisième : les scripts d'insertion
apparient par (nom normalisé, type de contact), si bien qu'une même personne
arrivée par deux chemins — l'import presse, l'open data parlementaire,
CiviCRM — obtient deux fiches. L'affichage ne fait que le montrer.

LA GARANTIE : aucun échange n'est perdu. Chaque courriel, rencontre,
intervention, contenu, organisation et adresse apprise de la fiche absorbée
est rattaché à la fiche conservée AVANT toute suppression, et le script
recompte tout avant/après. Si un seul lien manquait à l'appel, il annule.

Les tables à reprendre ne sont pas écrites en dur : elles sont découvertes
dans le schéma (PRAGMA foreign_key_list). Une table ajoutée demain sera
reprise sans que personne ait à y penser — c'est exactement le genre d'oubli
qui ferait disparaître des échanges en silence.

Usage :
    python3 utils/fusionner_doublons.py --db meetings.db --dry-run
    python3 utils/fusionner_doublons.py --db meetings.db
    python3 utils/fusionner_doublons.py --db meetings.db --approchants  # rapport
"""
import argparse
import difflib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from civicrm import norm_name  # noqa: E402
from elus_roster import merge_roles  # noqa: E402

DEFAULT_DB = os.environ.get("IMAP_DB_PATH", "meetings.db")

# Ce qu'on recompte avant et après. On compte les ÉCHANGES encore rattachés à
# quelqu'un, pas les lignes de liaison : fusionner deux fiches toutes deux
# liées au même courriel fait légitimement tomber deux lignes à une. Ce qui ne
# doit jamais changer, c'est le nombre de courriels, rencontres, interventions
# et contenus qui ont encore au moins une personne — sinon un échange vient de
# devenir orphelin, et c'est le seul résultat inacceptable.
LIENS_VITAUX = {
    "mail_persons": "mail_id",
    "meeting_persons": "meeting_id",
    "intervention_persons": "intervention_id",
    "content_persons": "content_id",
}


def log(message):
    print(message, flush=True)


def tables_liees(db):
    """Toute table portant une clé étrangère vers persons -> sa colonne.

    Découvert plutôt que listé : person_emails, to_contact et civicrm_pending
    s'ajoutent d'eux-mêmes, et la prochaine aussi.
    """
    trouvees = {}
    for (nom,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"):
        if nom == "persons":
            continue
        for fk in db.execute(f"PRAGMA foreign_key_list({nom})"):
            if fk[2] == "persons":
                trouvees[nom] = fk[3]
    return trouvees


def compter_liens(db):
    totaux = {}
    for table, parent in LIENS_VITAUX.items():
        try:
            totaux[table] = db.execute(
                f"SELECT COUNT(DISTINCT {parent}) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            pass
    return totaux


def groupes_de_doublons(db):
    """[(nom normalisé, [fiches])] pour les noms portés par plusieurs fiches."""
    par_nom = {}
    for ligne in db.execute("SELECT * FROM persons ORDER BY id"):
        nom = (ligne["name"] or "").strip()
        if not nom:
            continue          # une fiche sans nom n'est pas un doublon : voir --rapport
        par_nom.setdefault(norm_name(nom), []).append(ligne)
    return [(cle, gens) for cle, gens in par_nom.items() if len(gens) > 1]


def densite(fiche):
    """Combien de champs renseignés : à défaut d'autorité, la fiche la plus
    complète est la meilleure base."""
    return sum(1 for cle in fiche.keys()
               if cle != "id" and str(fiche[cle] or "").strip())


def choisir_survivante(gens, officiels):
    """La fiche qu'on garde.

    D'abord celle que la source officielle connaît — elle porte le portrait et
    la fiche parlementaire posés par sync_emails_from_elus, donc elle est
    alignée sur l'open data. À égalité, la plus complète ; à égalité encore, la
    plus ancienne, qui porte l'historique le plus long.
    """
    def rang(fiche):
        officielle = norm_name(fiche["name"]) in officiels and bool(
            (fiche["fiche_url"] or "").strip()
            if "fiche_url" in fiche.keys() else False)
        return (0 if officielle else 1, -densite(fiche), fiche["id"])
    return sorted(gens, key=rang)[0]


def champs_fusionnes(survivante, autres):
    """Ce qu'on récupère des fiches absorbées : tout champ vide chez la
    survivante et renseigné ailleurs. On ne remplace jamais une valeur
    existante — la survivante a été choisie parce qu'elle fait autorité."""
    neufs = {}
    for cle in survivante.keys():
        if cle in ("id", "name", "created_at"):
            continue
        if str(survivante[cle] or "").strip():
            continue
        for autre in autres:
            valeur = autre[cle] if cle in autre.keys() else None
            if str(valeur or "").strip():
                neufs[cle] = valeur
                break
    # Les fonctions se cumulent au lieu de s'écraser, comme dans les scripts
    # d'insertion : « Ministre, Député·e » reste vrai des deux côtés.
    roles = survivante["role"] or ""
    for autre in autres:
        if (autre["role"] or "").strip():
            roles = merge_roles(roles, autre["role"], [])
    if roles != (survivante["role"] or ""):
        neufs["role"] = roles
    return neufs


def fusionner_groupe(db, gens, officiels, dry_run):
    survivante = choisir_survivante(gens, officiels)
    autres = [g for g in gens if g["id"] != survivante["id"]]
    perdants = [g["id"] for g in autres]
    marques = ",".join("?" * len(perdants))

    deplaces = {}
    for table, colonne in sorted(tables_liees(db).items()):
        combien = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {colonne} IN ({marques})",
            perdants).fetchone()[0]
        if combien:
            deplaces[table] = combien
        if dry_run:
            continue
        # OR IGNORE : la clé primaire composite (mail_id, person_id) refuse un
        # lien qui existe déjà pour la survivante. La ligne est alors laissée
        # en place, puis supprimée juste après — ne pas l'ignorer ferait
        # échouer toute la fusion sur un doublon de lien parfaitement normal.
        db.execute(
            f"UPDATE OR IGNORE {table} SET {colonne} = ? "
            f"WHERE {colonne} IN ({marques})", [survivante["id"], *perdants])
        db.execute(
            f"DELETE FROM {table} WHERE {colonne} IN ({marques})", perdants)

    # thread_persons garde des identifiants en CSV, hors clé étrangère : sans
    # cette reprise, un courriel de réponse continuerait d'être attribué à une
    # fiche supprimée, et finirait donc dans la file « à rattacher ».
    try:
        for message_id, ids in db.execute(
                "SELECT message_id, person_ids FROM thread_persons").fetchall():
            liste = [i.strip() for i in (ids or "").split(",") if i.strip()]
            neuf = []
            for i in liste:
                remplacant = str(survivante["id"]) if int(i) in perdants else i
                if remplacant not in neuf:
                    neuf.append(remplacant)
            if neuf != liste and not dry_run:
                db.execute(
                    "UPDATE thread_persons SET person_ids = ? WHERE message_id = ?",
                    (",".join(neuf), message_id))
    except (sqlite3.Error, ValueError):
        pass

    neufs = champs_fusionnes(survivante, autres)
    if neufs and not dry_run:
        db.execute(
            "UPDATE persons SET " + ", ".join(f"{c} = ?" for c in neufs)
            + " WHERE id = ?", [*neufs.values(), survivante["id"]])
    if not dry_run:
        db.execute(f"DELETE FROM persons WHERE id IN ({marques})", perdants)

    return survivante, autres, deplaces, neufs


def noms_approchants(db, seuil=0.90):
    """Les noms très proches sans être identiques — « Flore » / « Flaure ».

    Signalés, jamais fusionnés : deux élu·es peuvent porter des noms voisins,
    et fusionner deux personnes distinctes est la seule erreur irréparable ici.

    Comparer 1 800 noms deux à deux fait 1,6 million de comparaisons — mesuré
    à 30 secondes. On ne compare donc que les noms partageant leurs cinq
    premiers caractères : une faute de frappe les préserve presque toujours.
    Presque : une coquille sur le prénom échappe à ce filet, et c'est le prix
    d'un rapport qui s'obtient en une seconde plutôt qu'en une demi-minute.
    """
    noms = sorted({norm_name(r[0]) for r in db.execute(
        "SELECT name FROM persons WHERE TRIM(COALESCE(name, '')) <> ''")})
    paquets = {}
    for nom in noms:
        paquets.setdefault(nom[:5], []).append(nom)
    paires = []
    for lot in paquets.values():
        for i, a in enumerate(lot):
            for b in lot[i + 1:]:
                if abs(len(a) - len(b)) > 3:
                    continue
                score = difflib.SequenceMatcher(None, a, b).ratio()
                if score >= seuil:
                    paires.append((score, a, b))
    paires.sort(reverse=True)
    return [(a, b) for _s, a, b in paires]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true",
                    help="dire ce qui serait fusionné, sans rien écrire")
    ap.add_argument("--approchants", action="store_true",
                    help="signaler aussi les noms très proches (jamais fusionnés)")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")

    officiels = {norm_name(r[0]) for r in db.execute(
        "SELECT name FROM persons WHERE TRIM(COALESCE(fiche_url, '')) <> ''")}

    avant = compter_liens(db)
    groupes = groupes_de_doublons(db)
    if not groupes:
        log("Aucun doublon de nom.")
    fusions = 0
    for _cle, gens in groupes:
        survivante, autres, deplaces, neufs = fusionner_groupe(
            db, gens, officiels, args.dry_run)
        fusions += len(autres)
        prefixe = "[à blanc] " if args.dry_run else ""
        log(f"{prefixe}{survivante['name']} : on garde la fiche "
            f"#{survivante['id']}, on absorbe "
            f"{', '.join('#' + str(a['id']) for a in autres)}")
        for table, combien in sorted(deplaces.items()):
            log(f"    {table} : {combien} lien(s) repris")
        if neufs:
            log(f"    champs récupérés : {', '.join(sorted(neufs))}")

    sans_nom = db.execute(
        "SELECT COUNT(*) FROM persons WHERE TRIM(COALESCE(name, '')) = ''"
    ).fetchone()[0]
    if sans_nom:
        log(f"\n{sans_nom} fiche(s) sans nom — à regarder à la main, elles ne "
            f"peuvent pas être appariées.")

    if args.approchants:
        paires = noms_approchants(db)
        if paires:
            log(f"\n{len(paires)} nom(s) très proches, NON fusionnés "
                f"(une faute de frappe, ou deux personnes différentes — "
                f"c'est à un humain de trancher) :")
            for a, b in paires[:40]:
                log(f"  « {a} »  ≈  « {b} »")
            if len(paires) > 40:
                log(f"  … et {len(paires) - 40} autres, les plus ressemblantes "
                    f"d'abord.")

    apres = compter_liens(db)
    perdus = {t: avant[t] - apres.get(t, 0) for t in avant
              if apres.get(t, 0) < avant[t]}
    if perdus and not args.dry_run:
        db.rollback()
        sys.exit(f"ANNULÉ : des liens ont disparu {perdus}. Rien n'a été "
                 f"écrit. C'est exactement ce que cette vérification existe "
                 f"pour empêcher.")

    if args.dry_run:
        db.rollback()
        log(f"\n[à blanc] {fusions} fiche(s) seraient absorbées. Rien écrit.")
    else:
        db.commit()
        log(f"\n{fusions} fiche(s) absorbée(s). "
            f"Liens conservés : {apres}.")
    db.close()


if __name__ == "__main__":
    main()
