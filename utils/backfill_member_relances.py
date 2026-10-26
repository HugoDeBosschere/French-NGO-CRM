#!/usr/bin/env python3
"""Rattrapage ponctuel : poser une « Relance prévue » (et, quand c'est possible,
« Qui a reçu / envoyé le mail ? ») sur les courriels de membres importés avant
le correctif de import_member_mails.py.

Jusqu'au 26/10/2026, l'import automatique des courriels membres ↔ élu·es
enregistrait ces courriels sans date de relance ni utilisateurice : le CRM ne
rappelait donc jamais de relancer l'élu·e (demande de Hugo, 23/10/2026). Ce
script répare l'existant :

- Courriels publiés (table `mails`) rattachés à un membre (`mail_members`, que
  seul l'import automatique remplit) et sans relance : relance = date d'ajout
  dans le CRM + 10 jours, comme demandé. `received_by` est rempli quand le nom
  du membre correspond à celui d'une utilisateurice, et seulement s'il est vide.
- Brouillons en attente de modération venant de l'import (`pending_mails`,
  `submitted_by` = IMPORT_SOURCE) et sans relance : même règle, pour que le
  formulaire de validation la propose déjà.

Rien d'autre n'est touché : une relance déjà posée, cochée « faite » ou modifiée
à la main reste telle quelle.

Idempotent : le relancer ne change plus rien. À lancer d'abord avec --dry-run,
qui liste ce qui serait modifié sans rien écrire.

  python3 utils/backfill_member_relances.py --dry-run
  python3 utils/backfill_member_relances.py --dry-run --depuis 2026-09-01
  python3 utils/backfill_member_relances.py --depuis 2026-09-01
"""
import argparse
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_campaign_mails import log  # noqa: E402
from import_member_mails import (  # noqa: E402
    DEFAULT_DB, IMPORT_SOURCE, ensure_member_tables, moderateur_du_membre,
    relance_par_defaut,
)


def jour_d_ajout(created_at):
    """« 2026-10-20T08:05:00+00:00 » -> « 2026-10-20 » (None si illisible)."""
    jour = (created_at or "")[:10]
    try:
        date.fromisoformat(jour)
    except ValueError:
        return None
    return jour


def rattraper_publies(db, dry_run, depuis=None):
    """Relance (et utilisateurice si possible) des courriels de membres publiés.

    Renvoie (relances posées, utilisateurices renseignées).
    """
    lignes = db.execute(
        """
        SELECT x.id, x.created_at, x.mail_date, x.received_by, me.name,
               COALESCE(x.subject, '')
          FROM mails x
          JOIN mail_members mm ON mm.mail_id = x.id
          JOIN members      me ON me.id      = mm.member_id
         WHERE x.follow_up_date IS NULL OR trim(x.follow_up_date) = ''
         GROUP BY x.id
         ORDER BY x.created_at
        """
    ).fetchall()
    relances = moderateurs = 0
    for mail_id, created_at, mail_date, received_by, nom_membre, objet in lignes:
        jour = jour_d_ajout(created_at)
        if jour is None or (depuis and jour < depuis):
            continue
        relance = relance_par_defaut(jour)
        if relance is None:
            continue
        moderateur = received_by or moderateur_du_membre(db, nom_membre)
        log(f"  courriel {mail_id} du {mail_date}, ajouté le {jour} : relance "
            f"{relance}" + ("" if received_by or not moderateur
                            else f", utilisateurice n°{moderateur}")
            + f" | {objet[:60]!r}")
        if not dry_run:
            db.execute(
                "UPDATE mails SET follow_up_date = ?, "
                "received_by = COALESCE(received_by, ?) WHERE id = ?",
                (relance, moderateur, mail_id))
        relances += 1
        if moderateur and not received_by:
            moderateurs += 1
    log(f"Courriels publiés : {relances} relance(s) posée(s), "
        f"{moderateurs} « Qui a reçu / envoyé » renseigné(s).")
    return relances, moderateurs


def rattraper_brouillons(db, dry_run, depuis=None):
    """Relance des brouillons d'import en attente de modération."""
    lignes = db.execute(
        """
        SELECT id, created_at FROM pending_mails
         WHERE submitted_by = ?
           AND (follow_up_date IS NULL OR trim(follow_up_date) = '')
        """,
        (IMPORT_SOURCE,),
    ).fetchall()
    poses = 0
    for pid, created_at in lignes:
        jour = jour_d_ajout(created_at)
        if jour is None or (depuis and jour < depuis):
            continue
        if not dry_run:
            db.execute("UPDATE pending_mails SET follow_up_date = ? WHERE id = ?",
                       (relance_par_defaut(jour), pid))
        poses += 1
    log(f"Brouillons en attente : {poses} relance(s) posée(s).")
    return poses


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("IMAP_DB_PATH", DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true",
                    help="lister ce qui serait modifié, sans rien écrire")
    ap.add_argument("--depuis", type=date.fromisoformat, default=None,
                    help="ne traiter que les courriels ajoutés à partir de cette "
                         "date (AAAA-MM-JJ), pour ne pas faire remonter de très "
                         "vieilles relances sur la page « À faire »")
    args = ap.parse_args()
    depuis = args.depuis.isoformat() if args.depuis else None

    db = sqlite3.connect(args.db)
    # L'application écrit dans le même fichier : attendre plutôt qu'échouer sur
    # « database is locked ».
    db.execute("PRAGMA busy_timeout = 30000")
    ensure_member_tables(db)
    rattraper_publies(db, args.dry_run, depuis)
    rattraper_brouillons(db, args.dry_run, depuis)
    if not args.dry_run:
        db.commit()
    db.close()
    log("Terminé." + (" (dry-run, rien n'a été écrit)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
