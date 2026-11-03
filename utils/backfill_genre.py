#!/usr/bin/env python3
"""Fill `persons.gender` for élu·es from the official datasets.

    python3 utils/backfill_genre.py            # dry run: counts only
    python3 utils/backfill_genre.py --commit

The four sources in actual_dataset/ all state it: the Assemblée nationale
(`sexe`), the Sénat and the Gouvernement (`civilite`, « M. » / « Mme »), the
European Parliament (`genre`, already resolved by extract_eurodeputes.py).
Matched on the name the insert_*.py scripts wrote, within contact_type
« Politique ».

Only a NULL gender is filled, and only on a fiche a script created
(added_by IS NULL): what someone typed by hand, including « not stated », is
never overwritten. Nobody outside these lists gets a gender guessed from a
first name — they stay « Non renseigné » until someone fills it in.

Idempotent; run by sync_officials.py after each refresh, so a new élu·e gets
theirs the same week.
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.environ.get("IMAP_DB_PATH") or os.path.join(ROOT, "meetings.db")
DATA = os.path.join(ROOT, "actual_dataset")

# Les trois lecteurs ci-dessous venaient d'export_contacts_xlsx.py, un script
# d'export ponctuel resté hors du dépôt : sync-officials.service échouait le
# 02/11/2026 sur « No module named 'export_contacts_xlsx' ». Ils vivent
# désormais ici, pour que ce script ne dépende de rien d'autre.


def deputes_genders():
    """Assemblée nationale dump: `sexe` is "H" or "F", name is the `nom` field
    (the whole name), which is what insert_deputes.py wrote to `persons`."""
    src = json.load(open(os.path.join(DATA, "deputes_officiel.json"), encoding="utf-8"))
    genre = {"H": "Homme", "F": "Femme"}
    return {
        d["depute"]["nom"]: genre.get(d["depute"].get("sexe"), "")
        for d in src["deputes"]
    }


def senateurices_genders():
    """Sénat dump: read `civilite` ("M." / "Mme"), not `feminise` — the latter
    says whether the person's title is spelled in the feminine, which is a
    different question and does not line up with the count of women."""
    src = json.load(
        open(os.path.join(DATA, "senateurices_actifs.json"), encoding="utf-8")
    )
    genre = {"M.": "Homme", "Mme": "Femme"}
    return {
        f"{s['prenom']} {s['nom']}".strip(): genre.get(s.get("civilite"), "")
        for s in src
    }


def eurodeputes_genders():
    """European Parliament dump: extract_eurodeputes.py already resolved the EU
    `hasGender` vocabulary to "Homme" / "Femme", so read it straight off."""
    src = json.load(open(os.path.join(DATA, "eurodeputes_fr.json"), encoding="utf-8"))
    return {m["nom_complet"]: m.get("genre", "") for m in src}


def gouvernement_genders():
    """Gouvernement dump: `civilite` like the Sénat, under each `membre`."""
    path = os.path.join(DATA, "gouvernement.json")
    src = json.load(open(path, encoding="utf-8"))
    genre = {"M.": "Homme", "Mme": "Femme"}
    out = {}
    for entry in src["gouvernement"]:
        m = entry["membre"]
        g = genre.get(m.get("civilite"), "")
        out[(m.get("nom_complet") or "").strip()] = g
        out[f"{m.get('prenom', '')} {m.get('nom', '')}".strip()] = g
    return out


SOURCES = [
    ("Assemblée nationale", "deputes_officiel.json", deputes_genders),
    ("Sénat", "senateurices_actifs.json", senateurices_genders),
    ("Gouvernement", "gouvernement.json", gouvernement_genders),
    ("Parlement européen", "eurodeputes_fr.json", eurodeputes_genders),
]


def known_genders():
    """name -> « Femme » / « Homme », from every dataset present. A missing
    file is skipped and said, so one absent chamber does not stop the rest."""
    genders = {}
    for label, filename, reader in SOURCES:
        if not os.path.exists(os.path.join(DATA, filename)):
            print(f"  {label} : {filename} absent, ignoré.")
            continue
        found = {k: v for k, v in reader().items() if k and v}
        print(f"  {label} : {len(found)} genres lus.")
        genders.update(found)
    return genders


def backfill(db, genders, commit):
    rows = db.execute(
        "SELECT id, name FROM persons WHERE gender IS NULL "
        "AND added_by IS NULL AND contact_type = 'Politique'").fetchall()
    updates = [(genders[name], pid) for pid, name in rows if name in genders]
    if commit:
        db.executemany("UPDATE persons SET gender = ? WHERE id = ?", updates)
        db.commit()
    return len(rows), len(updates)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", action="store_true",
                        help="écrire en base (sinon simple décompte)")
    args = parser.parse_args()

    db = sqlite3.connect(DB)
    db.execute("PRAGMA busy_timeout = 30000")
    cols = [r[1] for r in db.execute("PRAGMA table_info(persons)")]
    if "gender" not in cols:
        sys.exit("persons.gender absent : lancez d'abord l'application, "
                 "dont init_db() ajoute la colonne.")
    genders = known_genders()
    missing, filled = backfill(db, genders, args.commit)
    verb = "renseigné" if args.commit else "à renseigner"
    print(f"{filled} genre(s) {verb} sur {missing} fiche(s) politique(s) "
          "sans genre.")
    if not args.commit:
        print("(simulation : relancer avec --commit pour écrire)")
    db.close()


if __name__ == "__main__":
    main()
