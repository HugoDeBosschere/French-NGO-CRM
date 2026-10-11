#!/usr/bin/env python3
"""Rattrapage unique : retrouver le corps des courriels citoyens déjà importés.

Jusqu'en octobre 2026, le pipeline citoyen gardait l'objet et le nom de
l'élu·e, pas le message. Les courriels déjà en base n'ont donc pas de corps, et
aucun import futur ne le leur donnera : la déduplication se fait sur le
Message-ID, et le leur est déjà noté dans `imported_mails`. Un `--backfill`
ordinaire les sauterait — précisément ceux qu'on veut enrichir.

Ce script relit la boîte de campagne et rapproche chaque message de sa ligne
en base. Le lien direct n'existe pas : `imported_mails` ne garde pas
l'identifiant du courriel créé. On rapproche donc par ce que les deux côtés
ont en commun : la date, l'objet, et l'ensemble des élu·es destinataires.

Les ambiguïtés sont assumées plutôt que devinées. Pour une campagne, date et
objet sont souvent identiques d'un citoyen à l'autre — c'est le principe d'une
campagne. Quand plusieurs messages et plusieurs lignes partagent la même clé :

- si les lignes sont toutes intactes (aucune pièce jointe, aucune validation,
  résumé encore égal à l'étiquette générée), elles sont interchangeables : la
  base obtient le même contenu quel que soit l'ordre, et on attribue ;
- sinon on passe, et on le dit. Attribuer les mots d'un citoyen au mauvais
  enregistrement serait pire que de ne rien faire.

Usage :
    python3 utils/backfill_campagne_corps.py --dry-run
    python3 utils/backfill_campagne_corps.py
"""
import argparse
import email
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_campaign_mails import (  # noqa: E402
    DEFAULT_DB, connect_imap, decoded, etape_echange, extract_body, fetch_uids,
    load_email_index, log, mail_date_iso, match_recipients,
)

ETIQUETTE = "Mail d'un citoyen"


def cle(mail_date, subject, person_ids):
    return (mail_date, (subject or "").strip(), tuple(sorted(person_ids)))


def lignes_sans_corps(db, refaire=False):
    """{clé: [lignes]} — les courriels citoyens dont le corps manque.

    Avec `refaire`, ceux qui en ont un déjà sont repris aussi. Les 285 premiers
    corps ont été extraits par une version qui remplaçait chaque balise HTML
    par une espace et ne décodait pas les entités : ceux qui n'avaient qu'une
    partie HTML sont arrivés d'un seul tenant, avec « &nbsp; » en clair. La
    source est la même, donc les repasser ne peut qu'améliorer — et le corps
    n'est modifiable nulle part dans l'interface, donc on n'écrase aucun
    travail humain.
    """
    condition = "" if refaire else "b.mail_id IS NULL AND "
    par_cle = {}
    for ligne in db.execute(
        f"""
        SELECT x.id, x.mail_date, x.subject, x.summary, x.direction,
               x.document_stored_name, x.validated_by, x.received_by
          FROM mails x
          LEFT JOIN mail_bodies b ON b.mail_id = x.id
         WHERE {condition}x.summary LIKE ?
         ORDER BY x.id
        """,
        (ETIQUETTE + "%",),
    ):
        ids = [r[0] for r in db.execute(
            "SELECT person_id FROM mail_persons WHERE mail_id = ?",
            (ligne["id"],))]
        par_cle.setdefault(cle(ligne["mail_date"], ligne["subject"], ids),
                           []).append(ligne)
    return par_cle


def intacte(ligne):
    """Une ligne que personne n'a touchée depuis l'import.

    Deux lignes intactes de même date, même objet et mêmes destinataires sont
    indiscernables : leur attribuer l'un ou l'autre corps donne la même base.
    Une ligne annotée, validée ou porteuse d'une pièce jointe, elle, est un
    enregistrement particulier — on n'y touche qu'à coup sûr.
    """
    return (not (ligne["document_stored_name"] or "").strip()
            and ligne["validated_by"] is None
            and ligne["received_by"] is None
            and (ligne["summary"] or "").startswith(ETIQUETTE))


def apparier(attente, messages):
    """Rapprocher lignes et messages. Retourne (paires, ambigus, sans_message).

    Séparé de la lecture IMAP pour être vérifiable : c'est ici que se joue le
    risque — attribuer les mots d'un citoyen au mauvais enregistrement — et
    c'est donc ici que les tests portent.
    """
    paires, ambigus, sans_message = [], [], 0
    for k, lignes in attente.items():
        lot = messages.get(k)
        if not lot:
            sans_message += len(lignes)
            continue
        if (len(lignes) > 1 or len(lot) > 1) and not all(
                intacte(ligne) for ligne in lignes):
            ambigus.append((k, lignes, lot))
            continue
        paires.extend(zip(lignes, lot))
    return paires, ambigus, sans_message


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("IMAP_DB_PATH", DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refaire", action="store_true",
                    help="reprendre aussi les corps déjà enregistrés, pour "
                         "profiter d'une extraction HTML corrigée")
    ap.add_argument("--sens", action="store_true",
                    help="corriger aussi « Envoyé / Reçu » d'après les "
                         "destinataires (lignes intactes seulement)")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")

    attente = lignes_sans_corps(db, args.refaire)
    total_lignes = sum(len(v) for v in attente.values())
    if not total_lignes:
        log("Aucun courriel citoyen à traiter. Rien à faire.")
        return
    quoi = "à (re)lire" if args.refaire else "sans corps"
    log(f"{total_lignes} courriel(s) citoyen(s) {quoi}, "
        f"{len(attente)} clé(s) distincte(s).")

    email_index = load_email_index(db)
    mailbox = os.environ.get("MEMBER_IMAP_MAILBOX",
                             os.environ.get("IMAP_MAILBOX", "INBOX"))
    notre_adresse = os.environ.get("IMAP_USER")

    # Les messages de la boîte, groupés par la même clé.
    messages = {}
    conn = connect_imap()
    lus = 0
    try:
        uids = fetch_uids(conn, mailbox, 0, True)
        log(f"Boîte {mailbox!r} : {len(uids)} message(s) à relire.")
        for uid in uids:
            statut, data = conn.uid("fetch", str(uid), "(RFC822)")
            if statut != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            matches = match_recipients(msg, db, email_index)
            if not matches:
                continue
            corps = extract_body(msg)
            if not corps:
                continue
            k = cle(mail_date_iso(msg),
                    decoded(msg.get("Subject")) or "(sans objet)",
                    [pid for pid, _nom in matches])
            messages.setdefault(k, []).append((corps, msg, matches))
            lus += 1
    finally:
        conn.logout()
    log(f"{lus} message(s) exploitable(s) relu(s).")

    paires, ambigus_lots, sans_message = apparier(attente, messages)
    for k, lignes, lot in ambigus_lots:
        log(f"  [ambigu] {k[0]} « {k[1][:60]} » : {len(lignes)} ligne(s) pour "
            f"{len(lot)} message(s), dont au moins une annotée — laissée "
            f"telle quelle.")
    ambigus = sum(len(lignes) for _k, lignes, _lot in ambigus_lots)

    remplis = sens_corriges = 0
    for ligne, (corps, msg, matches) in paires:
        if not args.dry_run:
            db.execute(
                "INSERT OR REPLACE INTO mail_bodies (mail_id, body) "
                "VALUES (?, ?)", (ligne["id"], corps))
        remplis += 1
        if not args.sens:
            continue
        adresses = set()
        for pid, _nom in matches:
            for (adresse,) in db.execute(
                    "SELECT email FROM persons WHERE id = ? "
                    "AND email IS NOT NULL AND email != ''", (pid,)):
                adresses.add(adresse.strip().lower())
        etape = etape_echange(ligne["subject"], msg, adresses, notre_adresse)
        sens = "sent" if etape == "envoi" else "received"
        if sens != ligne["direction"] and intacte(ligne):
            if not args.dry_run:
                db.execute("UPDATE mails SET direction = ? WHERE id = ?",
                           (sens, ligne["id"]))
            sens_corriges += 1

    if args.dry_run:
        db.rollback()
    else:
        db.commit()
    db.close()

    prefixe = "[à blanc] " if args.dry_run else ""
    log(f"\n{prefixe}Corps retrouvés : {remplis}/{total_lignes}")
    if args.sens:
        log(f"{prefixe}Sens corrigés : {sens_corriges}")
    if sans_message:
        log(f"Sans message correspondant dans la boîte : {sans_message} "
            f"(message supprimé de la boîte, ou objet/date qui ne "
            f"correspondent plus).")
    if ambigus:
        log(f"Laissés de côté par prudence : {ambigus} — plusieurs lignes et "
            f"plusieurs messages partagent la même date, le même objet et les "
            f"mêmes destinataires, et l'une d'elles a été modifiée à la main.")
    if args.dry_run:
        log("Rien n'a été écrit.")


if __name__ == "__main__":
    main()
