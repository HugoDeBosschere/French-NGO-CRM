#!/usr/bin/env python3
"""Pourquoi les courriels d'un membre n'arrivent-ils pas dans le CRM ?

Un membre signale que ses échanges avec des élu·es ne sont pas enregistrés. La
chaîne compte cinq maillons, et il n'y a aucun moyen de deviner lequel a cédé :

    1. une règle Workspace copie le courriel dans la boîte d'audit ;
    2. l'import tourne chaque jour et lit cette boîte ;
    3. une des deux adresses doit être reconnue comme celle d'un membre ;
    4. l'autre doit être rattachée à une fiche du CRM ;
    5. sinon l'adresse part en file « à rattacher », et attend un humain.

Ce script interroge les cinq, dans cet ordre, et s'arrête de conclure dès qu'il
ne sait plus. Il ne MODIFIE RIEN : ni la base, ni la boîte, ni les courriels.

    docker exec -i website-meeting-app python3 utils/diagnostic_membre.py elie@pauseia.fr

Avec --imap, il va aussi lire la boîte d'audit et dire, courriel par courriel,
ce que l'import en ferait aujourd'hui — c'est le seul maillon que la base ne
peut pas raconter :

    docker exec -i website-meeting-app python3 utils/diagnostic_membre.py elie@pauseia.fr --imap
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import maildomains  # noqa: E402
from import_member_mails import (  # noqa: E402
    DEFAULT_DB, GROUP_ADDRESSES, MEMBER_DOMAIN, addr_pairs,
    build_name_pattern_index, classify, connect_imap, ensure_member_tables,
    is_member, is_official, load_email_index_with_aliases, parle_delus,
)
from import_campaign_mails import decoded  # noqa: E402


def titre(texte):
    print(f"\n=== {texte} ===")


def maillon_3(adresse):
    """L'adresse est-elle reconnue comme celle d'un membre ?"""
    titre("3. L'adresse est-elle reconnue comme celle d'un membre ?")
    if is_member(adresse):
        print(f"  OUI — {adresse} est bien vue comme un membre.")
        return True
    if not adresse.endswith("@" + MEMBER_DOMAIN):
        print(f"  NON — l'adresse n'est pas en @{MEMBER_DOMAIN}.")
        print("  C'est la cause : la chaîne ne suit que les adresses de")
        print(f"  l'association. Un membre qui écrit depuis son adresse")
        print("  personnelle n'est pas suivi, par conception.")
    else:
        print(f"  NON — {adresse} figure dans GROUP_ADDRESSES, la liste des")
        print("  adresses de service (campagne@, contact@…), traitées comme des")
        print("  boîtes partagées et non comme des personnes.")
        print("  À corriger dans utils/import_member_mails.py si c'est une erreur.")
    return False


def maillon_2(db):
    """L'import tourne-t-il, et quand pour la dernière fois ?"""
    titre("2. L'import tourne-t-il ?")
    try:
        lignes = db.execute(
            "SELECT started_at, status, imported, inspected, detail "
            "FROM import_runs WHERE script LIKE '%member%' "
            "ORDER BY started_at DESC LIMIT 3"
        ).fetchall()
    except sqlite3.OperationalError:
        print("  Table import_runs absente : l'import n'a jamais tourné ici.")
        return
    if not lignes:
        print("  Aucun passage enregistré. C'est la cause la plus probable :")
        print("  le service n'est pas installé, ou jamais lancé.")
        return
    for ligne in lignes:
        print(f"  {ligne['started_at']}  {ligne['status']:<8} "
              f"{ligne['imported']} importé(s) sur {ligne['inspected']} examiné(s)"
              f"  {ligne['detail'] or ''}")
    if lignes[0]["status"] != "ok":
        print("  Le dernier passage a ÉCHOUÉ : voir le détail ci-dessus.")


def maillon_4(db, adresse):
    """Ce membre est-il connu, et combien de courriels lui sont attribués ?"""
    titre("4. Ce que la base sait déjà de ce membre")
    ensure_member_tables(db)
    membre = db.execute(
        "SELECT id, email, name FROM members WHERE LOWER(email) = ?",
        (adresse,),
    ).fetchone()
    if membre is None:
        print("  Inconnu de la table `members`.")
        print("  Un membre y apparaît au PREMIER courriel importé : son absence")
        print("  dit qu'aucun de ses échanges n'est jamais entré, pas qu'il y a")
        print("  un réglage à faire pour lui.")
        return None
    print(f"  Fiche membre #{membre['id']} — {membre["name"] or '(sans nom)'}")
    compte = db.execute(
        "SELECT COUNT(*) FROM mail_members WHERE member_id = ?", (membre["id"],)
    ).fetchone()[0]
    print(f"  Courriels enregistrés et attribués : {compte}")
    dernier = db.execute(
        "SELECT ma.mail_date, ma.subject FROM mails ma "
        "JOIN mail_members mm ON mm.mail_id = ma.id "
        "WHERE mm.member_id = ? ORDER BY ma.mail_date DESC LIMIT 1",
        (membre["id"],),
    ).fetchone()
    if dernier:
        print(f"  Le plus récent : {dernier['mail_date']} — "
              f"« {dernier['subject'] or ''} »")
        print("  Si ses échanges récents manquent mais que d'anciens sont là,")
        print("  la chaîne fonctionne et c'est l'un des maillons d'après qui")
        print("  bloque pour ces courriels-là, pas un réglage le concernant.")
    # `pending_mails` ne porte pas de colonne membre : la file de modération
    # n'est donc pas interrogeable par membre, on donne le total global.
    attente = db.execute("SELECT COUNT(*) FROM pending_mails").fetchone()[0]
    if attente:
        print(f"  (File de modération, tous membres confondus : {attente} —")
        print("   un courriel capté mais non publié y attend un humain.)")
    return membre["id"]


def maillon_5(db, adresse):
    """Des adresses issues de ses courriels attendent-elles un rattachement ?"""
    titre("5. Adresses laissées en file « à rattacher »")
    try:
        lignes = db.execute(
            "SELECT email, display, last_subject, last_direction, last_seen "
            "FROM civicrm_pending WHERE status = 'pending' "
            "AND LOWER(COALESCE(last_member, '')) = ? "
            "ORDER BY last_seen DESC LIMIT 15", (adresse,)
        ).fetchall()
    except sqlite3.OperationalError:
        print("  File absente de cette base.")
        return
    if not lignes:
        print("  Aucune. Ses courriels ne sont donc pas bloqués là.")
        return
    print(f"  {len(lignes)} adresse(s) en attente, venues de SES courriels :")
    for l in lignes:
        print(f"    {l['email']:<40} {l['display'] or ''}  "
              f"« {l['last_subject'] or ''} »")
    print("  C'est la cause : l'élu·e n'a pas de fiche, ou son adresse n'est pas")
    print("  celle de la fiche. Allez sur /echanges/a-rattacher et tranchez :")
    print("  une fois l'adresse rattachée, les courriels suivants entrent seuls.")


def maillon_1(adresse, limite):
    """La boîte d'audit contient-elle ses courriels, et qu'en ferait l'import ?"""
    titre("1. La boîte d'audit reçoit-elle ses courriels ?")
    try:
        imap = connect_imap()
    except Exception as exc:                          # noqa: BLE001
        print(f"  Connexion IMAP impossible : {type(exc).__name__}: {exc}")
        print("  Sans elle, impossible de dire si la règle Workspace copie bien")
        print("  ses courriels. Vérifiez les variables MEMBER_IMAP_*.")
        return []
    try:
        statut, donnees = imap.search(None, "OR", f'FROM "{adresse}"',
                                      f'TO "{adresse}"')
        uids = donnees[0].split() if statut == "OK" else []
        print(f"  {len(uids)} courriel(s) le concernant dans la boîte.")
        if not uids:
            print("  C'est la cause : la règle Workspace ne copie pas ses")
            print("  courriels. Rien, en aval, ne peut les enregistrer.")
            print("  À vérifier dans la console Google Workspace :")
            print("  Applications > Gmail > Conformité du contenu.")
        return uids[-limite:]
    finally:
        try:
            imap.logout()
        except Exception:                             # noqa: BLE001
            pass


def verdict_par_courriel(db, imap, uids):
    """Ce que l'import ferait, aujourd'hui, de chacun de ces courriels."""
    import email as emaillib
    titre("1b. Ce que l'import ferait de chacun")
    email_index = load_email_index_with_aliases(db)
    name_patterns = build_name_pattern_index(db)
    for uid in uids:
        statut, donnees = imap.fetch(uid, "(RFC822)")
        if statut != "OK" or not donnees or not donnees[0]:
            print(f"  [{uid.decode()}] illisible")
            continue
        msg = emaillib.message_from_bytes(donnees[0][1])
        sujet = decoded(msg.get("Subject")) or "(sans objet)"
        deja = db.execute(
            "SELECT 1 FROM imported_mails WHERE message_id = ?",
            ((msg.get("Message-ID") or "").strip(),)
        ).fetchone()
        direction, matches, membre, _learn, faible = classify(
            msg, db, email_index, name_patterns)
        print(f"\n  « {sujet[:70]} »")
        if deja:
            print("    déjà importé (dédoublonné par Message-ID)")
            continue
        if direction:
            print(f"    SERAIT IMPORTÉ — {direction}, "
                  f"{len(matches)} personne(s) reconnue(s)"
                  + (", confiance faible : passerait en modération" if faible else ""))
            continue
        # Pas de direction : on dit lequel des deux côtés manque.
        autres = [a for _d, a in addr_pairs(msg, "From", "To", "Cc")
                  if not is_member(a)]
        membres = [a for _d, a in addr_pairs(msg, "From", "To", "Cc")
                   if is_member(a)]
        if not membres:
            print("    PAS IMPORTÉ — aucun membre PauseIA identifié dans les")
            print("    en-têtes. Si le membre était en Cci, c'est normal : un")
            print("    Cci n'apparaît pas dans le courriel reçu.")
            continue
        print("    PAS IMPORTÉ — aucune fiche ne porte l'adresse de l'autre côté :")
        for a in autres[:5]:
            marques = []
            if is_official(a):
                marques.append("domaine suivi")
            if maildomains.in_scope(a):
                marques.append("dans le périmètre")
            print(f"      {a}" + (f"   ({', '.join(marques)})" if marques else ""))
        if autres and not any(maildomains.in_scope(a) for a in autres):
            print("    Hors périmètre : n'entrerait que si le corps parle")
            print(f"    d'élu·es — ici, {'oui' if parle_delus(msg) else 'non'}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("adresse", help="l'adresse du membre, p. ex. elie@pauseia.fr")
    ap.add_argument("--imap", action="store_true",
                    help="lire aussi la boîte d'audit (maillon 1)")
    ap.add_argument("--max", type=int, default=10,
                    help="nombre de courriels détaillés avec --imap")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    adresse = args.adresse.strip().lower()
    print(f"Diagnostic pour {adresse}")
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    maildomains.refresh(db)
    maildomains.refresh_scope(db)

    if not maillon_3(adresse):
        return
    maillon_2(db)
    maillon_4(db, adresse)
    maillon_5(db, adresse)
    if args.imap:
        uids = maillon_1(adresse, args.max)
        if uids:
            imap = connect_imap()
            try:
                verdict_par_courriel(db, imap, uids)
            finally:
                try:
                    imap.logout()
                except Exception:                     # noqa: BLE001
                    pass
    else:
        print("\n(Relancez avec --imap pour interroger la boîte d'audit, "
              "le seul maillon que la base ne connaît pas.)")


if __name__ == "__main__":
    main()
