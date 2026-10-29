#!/usr/bin/env python3
"""Importer les passages sur l'IA trouvés par la veille des élu·es.

La veille (dépôt Pause-IA/declarations-elus-ia, https://veille.pauseia.fr/)
relit chaque matin les comptes rendus et les questions écrites de l'Assemblée,
du Sénat et du Parlement européen, et produit `synthese_elus.json` : pour chaque
élu·e, ses passages qui parlent d'IA (date, type, mots-clés, extrait, lien).
Ce script les range dans la table `veille_passages`, que la fiche de l'élu·e
affiche (« Sur l'IA, d'après la veille »). Demande de Romain, 27/10/2026.

Rapprochement avec les fiches, du plus sûr au moins sûr :
1. l'identifiant officiel (`ids_officiels` de la veille) retrouvé dans le lien
   de la fiche officielle (`persons.fiche_url`) : PA… pour l'Assemblée,
   matricule pour le Sénat, numéro pour le Parlement européen ;
2. sinon le nom, accents et majuscules ignorés, s'il ne désigne qu'une seule
   fiche, ou une seule fiche dont la fonction correspond à la chambre.
Un nom qui désigne plusieurs fiches est laissé de côté plutôt que deviné : un
passage affiché sur la mauvaise fiche serait pire qu'un passage absent. Les
personnalités auditionnées ne sont pas rapprochées (le nom seul ne suffit pas).

Chaque passage remplace tout : la table est le reflet du dernier fichier de la
veille, donc une correction côté veille se répercute, et relancer ne crée aucun
doublon. Rien d'autre n'est modifié dans le CRM.

    python3 utils/import_veille.py --file synthese_elus.json --dry-run
    python3 utils/import_veille.py --file synthese_elus.json
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importruns  # noqa: E402

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "meetings.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS veille_passages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    date       TEXT,      -- AAAA-MM-JJ du passage
    chambre    TEXT,      -- Assemblée nationale, Sénat, Parlement européen, Gouvernement
    type       TEXT,      -- séance, question écrite
    poids      INTEGER,   -- poids de la famille de mots-clés la plus forte
    mots       TEXT,      -- JSON {famille: [mots]}
    extrait    TEXT,
    marques    TEXT,      -- JSON [[début, fin], …] : mots-clés à mettre en gras
    lien       TEXT,      -- source officielle (https seulement)
    genere_le  TEXT       -- date du fichier de la veille
);
CREATE INDEX IF NOT EXISTS idx_veille_passages_person ON veille_passages(person_id);
"""

# Fonction(s) attendue(s) sur la fiche, par chambre de la veille : sert à
# départager deux fiches au même nom.
ROLES_PAR_CHAMBRE = {
    "Assemblée nationale": ("Député·e",),
    "Sénat": ("Sénateur·ice",),
    "Parlement européen": ("Député·e européen·ne",),
    "Gouvernement": ("Président·e de la République", "Premier·e ministre",
                     "Ministre", "Ministre délégué·e", "Secrétaire d'État",
                     "Autre fonction gouvernementale"),
}

RE_AN = re.compile(r"/deputes/(PA\d+)", re.I)
RE_SENAT = re.compile(r"/senateur/[^/]*?(\d{5}[a-z])\.html", re.I)
RE_PE = re.compile(r"europarl\.europa\.eu/meps/[a-z]{2}/(\d+)", re.I)


def log(msg):
    print(msg, flush=True)


def norm(nom):
    """« Grégory  ALLIONE » -> « gregory allione » (accents, casse, tirets)."""
    sans = "".join(c for c in unicodedata.normalize("NFKD", nom or "")
                   if not unicodedata.combining(c))
    return re.sub(r"[\s'’-]+", " ", sans).strip().lower()


def identifiant_de_fiche(url):
    """L'identifiant officiel lu dans le lien de la fiche, ou ''."""
    for motif in (RE_AN, RE_SENAT, RE_PE):
        m = motif.search(url or "")
        if m:
            return m.group(1).upper()
    return ""


def ensure_table(db):
    db.executescript(SCHEMA)


def index_des_fiches(db):
    """({identifiant: [person_id]}, {nom normalisé: [(person_id, rôles)]})."""
    par_id, par_nom = {}, {}
    for pid, nom, role, fiche in db.execute(
            "SELECT id, name, role, fiche_url FROM persons"):
        ident = identifiant_de_fiche(fiche)
        if ident:
            par_id.setdefault(ident, []).append(pid)
        par_nom.setdefault(norm(nom), []).append((pid, role or ""))
    return par_id, par_nom


def rapprocher(personne, par_id, par_nom):
    """(person_id, comment) ou (None, raison)."""
    for ident in personne.get("ids_officiels") or []:
        fiches = par_id.get(str(ident).upper(), [])
        if len(fiches) == 1:
            return fiches[0], "identifiant"
        if len(fiches) > 1:
            return None, "plusieurs fiches pour le même identifiant"
    chambre = personne.get("chambre", "")
    if chambre not in ROLES_PAR_CHAMBRE:
        return None, "personnalité auditionnée"
    candidats = par_nom.get(norm(personne.get("nom", "")), [])
    if len(candidats) == 1:
        return candidats[0][0], "nom"
    if not candidats:
        return None, "aucune fiche"
    roles = ROLES_PAR_CHAMBRE[chambre]
    bons = [pid for pid, role in candidats if any(r in role for r in roles)]
    if len(bons) == 1:
        return bons[0], "nom et fonction"
    return None, "homonymes"


def lien_sur(url):
    return url if (url or "").startswith("https://") else ""


def importer(db, donnees, dry_run=False):
    """Remplace les passages par ceux du fichier. Renvoie un résumé."""
    par_id, par_nom = index_des_fiches(db)
    genere_le = donnees.get("genere_le", "")
    lignes, comptes, ecartes = [], {}, []
    for personne in donnees.get("personnes", []):
        pid, comment = rapprocher(personne, par_id, par_nom)
        comptes[comment] = comptes.get(comment, 0) + 1
        if pid is None:
            ecartes.append((personne.get("nom", ""), personne.get("chambre", ""), comment))
            continue
        for p in personne.get("passages", []):
            lignes.append((
                pid, p.get("date", ""), personne.get("chambre", ""), p.get("type", ""),
                int(p.get("poids") or 0),
                json.dumps(p.get("mots") or {}, ensure_ascii=False),
                p.get("extrait", ""),
                json.dumps(p.get("marques") or []),
                lien_sur(p.get("lien", "")), genere_le))
    if not dry_run:
        db.execute("DELETE FROM veille_passages")
        db.executemany(
            "INSERT INTO veille_passages (person_id, date, chambre, type, poids, "
            "mots, extrait, marques, lien, genere_le) VALUES (?,?,?,?,?,?,?,?,?,?)",
            lignes)
        db.commit()
    return {"passages": len(lignes), "comptes": comptes, "ecartes": ecartes,
            "genere_le": genere_le}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="synthese_elus.json de la veille")
    ap.add_argument("--db", default=os.environ.get("IMAP_DB_PATH", DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true",
                    help="dire ce qui serait importé, sans rien écrire")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as f:
        donnees = json.load(f)
    if not donnees.get("personnes"):
        # Un fichier vide ou tronqué effacerait toute la table : on s'arrête.
        sys.exit(f"{args.file} ne contient aucune personne : rien n'est modifié.")

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    if not args.dry_run:
        ensure_table(db)
    tracker = importruns.track(db, "veille", enabled=not args.dry_run)
    run = tracker.__enter__()
    failure = None
    try:
        r = importer(db, donnees, dry_run=args.dry_run)
        run.imported = r["passages"]
        run.inspected = len(donnees["personnes"])
        rapproches = sum(n for c, n in r["comptes"].items()
                         if c in ("identifiant", "nom", "nom et fonction"))
        run.detail = (f"{r['passages']} passage(s) pour {rapproches} élu·e(s) ; "
                      f"veille du {r['genere_le']}.")
        log(f"Veille du {r['genere_le']} : {len(donnees['personnes'])} personne(s).")
        for comment, n in sorted(r["comptes"].items(), key=lambda x: -x[1]):
            log(f"  {n:4}  {comment}")
        for nom, chambre, raison in r["ecartes"]:
            if raison in ("homonymes", "plusieurs fiches pour le même identifiant"):
                log(f"  à vérifier : {nom} ({chambre}) — {raison}")
        log(f"{r['passages']} passage(s) "
            + ("à importer (dry-run, rien n'est écrit)." if args.dry_run else "importé(s)."))
    except BaseException as exc:             # noqa: BLE001 — noté, puis relancé
        failure = exc
        run.detail = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        tracker.__exit__(type(failure) if failure else None, failure, None)
        db.close()


if __name__ == "__main__":
    main()
