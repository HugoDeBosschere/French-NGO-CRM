"""Le type « Think tank » (demande de Jeanne) et utils/insert_think_tanks.py.

Un type ajouté à moitié se voit mal : une fiche « Membre d'un think tank »
dont le formulaire ne propose aucune organisation, ou une étiquette sans
couleur. Ces cas vérifient que le type est présent partout où les autres le
sont, et que le script de peuplement est idempotent.

Lancer depuis la racine :  python3 -m unittest discover -s tests
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "utils"))

os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
os.environ.setdefault("APP_PASSWORD", "x")
import app  # noqa: E402
import insert_think_tanks as itt  # noqa: E402

CONTACT = "Membre d'un think tank"
ORG = "Think tank"
SCRIPT = os.path.join(ROOT, "utils", "insert_think_tanks.py")


def base_au_schema_de_l_application():
    chemin = tempfile.mkstemp(suffix=".db")[1]
    ancien, app.DB_PATH = app.DB_PATH, Path(chemin)
    try:
        app.init_db()
    finally:
        app.DB_PATH = ancien
    return chemin


def lancer(chemin, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        env={**os.environ, "IMAP_DB_PATH": chemin},
        capture_output=True, text=True)


class TypePartout(unittest.TestCase):
    def test_listes_python(self):
        self.assertIn(CONTACT, app.CONTACT_TYPES)
        self.assertIn(ORG, app.ORG_TYPES)
        self.assertEqual(app.ORG_TYPE_BY_CONTACT_TYPE[CONTACT], ORG)
        self.assertIn(ORG, app.NEUTRAL_ORG_TYPES)
        self.assertIn(CONTACT, app.GENRE_BY_CONTACT_TYPE)
        self.assertIn(ORG, app.GENRE_BY_ORG_TYPE)
        self.assertIn(CONTACT, app.ROLES_BY_CONTACT_TYPE)
        for role in app.ROLES_BY_CONTACT_TYPE[CONTACT]:
            self.assertIn(role, app.ROLES)

    def test_masques_js_alignes_sur_app(self):
        js = open(os.path.join(ROOT, "static", "form-masks.js"), encoding="utf-8").read()
        self.assertIn(f'"{CONTACT}": "{ORG}"', js)
        neutres = re.search(r"NEUTRAL_ORG_TYPES\s*=\s*(\[[^\]]*\])", js).group(1)
        self.assertEqual(json.loads(neutres), app.NEUTRAL_ORG_TYPES)

    def test_etiquettes(self):
        macros = open(os.path.join(ROOT, "templates", "_macros.html"), encoding="utf-8").read()
        self.assertIn(f"'{ORG}'", macros)
        self.assertIn(f'"{CONTACT}"', macros)
        people = open(os.path.join(ROOT, "templates", "people.html"), encoding="utf-8").read()
        self.assertIn("Membres d'un think tank", people)


class Script(unittest.TestCase):
    def compter(self, chemin):
        with sqlite3.connect(chemin) as db:
            return db.execute(
                "SELECT COUNT(*) FROM organisations WHERE org_type = ?", (ORG,)).fetchone()[0]

    def test_liste_propre(self):
        noms = [itt.key(n) for n, _, _ in itt.THINK_TANKS]
        self.assertEqual(len(noms), len(set(noms)), "doublon dans la liste")
        for _, lien, domaine in itt.THINK_TANKS:
            self.assertTrue(lien.startswith("https://"), lien)
            self.assertTrue(domaine)

    def test_simulation_n_ecrit_rien(self):
        chemin = base_au_schema_de_l_application()
        r = lancer(chemin)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"ajoutés : {len(itt.THINK_TANKS)}", r.stdout)
        self.assertEqual(self.compter(chemin), 0)

    def test_commit_puis_relance_idempotente(self):
        chemin = base_au_schema_de_l_application()
        self.assertEqual(lancer(chemin, "--commit").returncode, 0)
        self.assertEqual(self.compter(chemin), len(itt.THINK_TANKS))
        r = lancer(chemin, "--commit")
        self.assertIn("ajoutés : 0", r.stdout)
        self.assertEqual(self.compter(chemin), len(itt.THINK_TANKS))
        with sqlite3.connect(chemin) as db:
            stances = {s for (s,) in db.execute(
                "SELECT DISTINCT stance FROM organisations WHERE org_type = ?", (ORG,))}
            sans_auteur = db.execute(
                "SELECT COUNT(*) FROM organisations WHERE org_type = ? AND added_by IS NULL",
                (ORG,)).fetchone()[0]
        self.assertEqual(stances, {"Inconnu"})
        self.assertEqual(sans_auteur, 0)

    def test_homonyme_d_un_autre_type_laisse_tel_quel(self):
        chemin = base_au_schema_de_l_application()
        with sqlite3.connect(chemin) as db:
            db.execute(
                "INSERT INTO organisations (name, org_type, stance, created_at) "
                "VALUES ('THE SHIFT PROJECT', 'ONG', 'Pour', '2026-01-01')")
        r = lancer(chemin, "--commit")
        self.assertIn("déjà en « ONG »", r.stdout)
        self.assertEqual(self.compter(chemin), len(itt.THINK_TANKS) - 1)
        with sqlite3.connect(chemin) as db:
            self.assertEqual(db.execute(
                "SELECT org_type, stance FROM organisations WHERE name = 'THE SHIFT PROJECT'"
            ).fetchone(), ("ONG", "Pour"))


if __name__ == "__main__":
    unittest.main()
