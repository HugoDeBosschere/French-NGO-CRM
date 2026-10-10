#!/usr/bin/env python3
"""Take a consistent backup of meetings.db, whatever its journal mode.

`cp meetings.db backup.db` stopped being correct the day the database moved to
WAL journal mode (September 2026, to stop the app and the mail importers
deadlocking each other). In WAL, recent commits live in a separate
`meetings.db-wal` file until a checkpoint folds them back in, so a plain copy
silently loses them — and looks perfectly fine.

`VACUUM INTO` writes a single self-contained file with everything committed at
the moment it runs, from a normal connection, with the app still serving. It
also compacts, so the backup is usually smaller than the live file.

    python3 utils/backup_db.py                          # meetings.db.bak-YYYY-MM-DD
    python3 utils/backup_db.py --label avant-civicrm    # …bak-avant-civicrm-YYYY-MM-DD
    python3 utils/backup_db.py --out /tmp/snapshot.db
    python3 utils/backup_db.py --dir /sauvegardes --keep 14   # usage planifié

Chaque sauvegarde est **relue** avant d'être annoncée : `integrity_check` plus
un comptage des tables principales. Une sauvegarde que personne n'a ouverte est
une espérance, pas une sauvegarde — et c'est le jour de la restauration qu'on
découvrirait le contraire.

Inside the container, where the database lives:

    docker exec website-meeting-app python3 /app/utils/backup_db.py
"""
import argparse
import glob
import os
import sqlite3
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="database to back up")
    parser.add_argument("--out", help="destination file (default: alongside the "
                                      "database, named .bak-<label>-<date>)")
    parser.add_argument("--label", help="what this snapshot is for, e.g. "
                                        "« avant-civicrm »")
    parser.add_argument("--dir", help="répertoire de destination (le nom reste "
                                      "automatique) — pour un usage planifié")
    parser.add_argument("--keep", type=int, metavar="N",
                        help="ne garder que les N sauvegardes les plus récentes "
                             "de ce répertoire ; les autres sont supprimées")
    args = parser.parse_args()

    out = args.out
    if not out:
        suffix = f"-{args.label}" if args.label else ""
        stem = os.path.basename(args.db) if args.dir else args.db
        base = os.path.join(args.dir, stem) if args.dir else stem
        out = f"{base}.bak{suffix}-{date.today().isoformat()}"
    # Un passage planifié ne doit jamais échouer parce qu'il a déjà tourné
    # aujourd'hui : on ajoute l'heure plutôt que d'abandonner, et surtout
    # plutôt que d'écraser la sauvegarde du matin. Et si deux passages tombent
    # dans la même seconde — une relance à la main juste après le timer, ou un
    # test — l'heure ne suffit plus : on numérote, sinon la deuxième copie
    # écrase silencieusement la première, dans le seul filet de l'outil.
    if os.path.exists(out):
        horodate = f"{out}-{datetime.now().strftime('%H%M%S')}"
        out = horodate
        n = 2
        while os.path.exists(out):
            out = f"{horodate}-{n}"
            n += 1
    if args.dir:
        os.makedirs(args.dir, exist_ok=True)

    db = sqlite3.connect(args.db)
    try:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        # VACUUM INTO takes a literal, not a placeholder, so the path is
        # quoted by hand — doubling any single quote, as SQL requires.
        db.execute("VACUUM INTO '" + out.replace("'", "''") + "'")
    finally:
        db.close()

    ok, detail = verify(out)
    if not ok:
        print(f"ABANDON — la sauvegarde écrite est inutilisable : {detail}")
        print(f"  fichier conservé pour examen : {out}")
        return 1

    size = os.path.getsize(out)
    print(f"Sauvegarde écrite : {out}")
    print(f"  {size / 1_048_576:.1f} Mio | journal de la source : {mode} | {detail}")
    if mode.lower() == "wal":
        print("  (en WAL, `cp meetings.db` seul perdrait les écritures récentes)")

    if args.keep:
        for removed in rotate(out, args.keep):
            print(f"  ancienne sauvegarde supprimée : {os.path.basename(removed)}")
    return 0


def verify(path):
    """Relire la sauvegarde. Retourne (ok, détail lisible).

    `VACUUM INTO` peut réussir et laisser un fichier tronqué si le disque se
    remplit en cours de route : l'erreur n'arrive qu'à la lecture. On ouvre donc
    ce qu'on vient d'écrire, on vérifie l'intégrité et on compte les lignes des
    deux tables qui portent le sens de l'outil. Une base valide mais vide serait
    un échec tout aussi silencieux.
    """
    # `connect()` n'ouvre pas le fichier : il ne fait qu'enregistrer un chemin.
    # Un fichier tronqué ou corrompu ne se signale qu'à la première lecture,
    # d'où le try qui englobe les requêtes et pas seulement la connexion.
    copy = None
    try:
        copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            return False, f"integrity_check : {verdict}"
        counts = {}
        for table in ("persons", "mails"):
            try:
                counts[table] = copy.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        if counts.get("persons") == 0:
            return False, "aucune fiche dans la copie"
        return True, (f"relue : {counts['persons']} fiche(s), "
                      f"{counts['mails']} courriel(s)")
    except sqlite3.Error as exc:
        return False, f"illisible ({exc})"
    finally:
        if copy is not None:
            copy.close()


def rotate(latest, keep):
    """Supprimer les sauvegardes au-delà des `keep` plus récentes. Retourne la liste.

    Le motif est dérivé du fichier qu'on vient d'écrire, donc la rotation ne
    touche jamais qu'à des sauvegardes du même jeu — une base voisine dans le
    même répertoire n'est pas concernée.
    """
    stem = latest.split(".bak")[0]
    found = sorted(glob.glob(f"{stem}.bak*"), key=os.path.getmtime, reverse=True)
    removed = []
    for path in found[keep:]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            continue
    return removed


if __name__ == "__main__":
    sys.exit(main())
