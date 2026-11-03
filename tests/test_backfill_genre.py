"""backfill_genre.py doit tourner seul, avec ce que contient le dépôt.

Le 02/11/2026, sync-officials.service a échoué : backfill_genre.py importait
ses lecteurs de genre depuis export_contacts_xlsx.py, un script d'export
ponctuel resté hors du dépôt (« No module named 'export_contacts_xlsx' »). Les
lecteurs vivent désormais dans backfill_genre.py ; ces cas l'assurent, sur les
vrais jeux de données de actual_dataset/.

Lancer depuis la racine :  python3 -m unittest discover -s tests
"""
import os
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
import backfill_genre as bg  # noqa: E402


def base_au_schema_de_l_application():
    chemin = tempfile.mkstemp(suffix=".db")[1]
    ancien, app.DB_PATH = app.DB_PATH, Path(chemin)
    try:
        app.init_db()
    finally:
        app.DB_PATH = ancien
    return chemin


class Autonome(unittest.TestCase):
    def test_aucun_import_du_script_d_export(self):
        self.assertNotIn("export_contacts_xlsx", sys.modules)

    def test_le_script_tourne_comme_sur_le_serveur(self):
        # Même appel que sync_officials.py : un processus à part, --commit.
        chemin = base_au_schema_de_l_application()
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "utils", "backfill_genre.py"),
             "--commit"],
            env={**os.environ, "IMAP_DB_PATH": chemin},
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class Lecteurs(unittest.TestCase):
    """Sur les jeux de données du dépôt : un nom par chambre."""

    def test_assemblee(self):
        self.assertEqual(bg.deputes_genders()["Audrey Abadie-Amiel"], "Femme")

    def test_senat_par_la_civilite(self):
        self.assertEqual(bg.senateurices_genders()["François Patriat"], "Homme")

    def test_parlement_europeen(self):
        self.assertEqual(bg.eurodeputes_genders()["Grégory Allione"], "Homme")


class Remplissage(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(base_au_schema_de_l_application())
        self.addCleanup(self.db.close)

    def fiche(self, nom, contact_type="Politique", added_by=None, gender=None):
        if added_by is not None:
            self.db.execute("INSERT OR IGNORE INTO moderators (id, name) VALUES (?, 'Hugo')",
                            (added_by,))
        return self.db.execute(
            "INSERT INTO persons (name, contact_type, political_group, stance, "
            "added_by, gender, created_at) VALUES (?, ?, '', 'Inconnue', ?, ?, '2026-01-01')",
            (nom, contact_type, added_by, gender)).lastrowid

    def genre(self, pid):
        return self.db.execute("SELECT gender FROM persons WHERE id = ?",
                               (pid,)).fetchone()[0]

    def test_ne_remplit_que_le_vide_des_fiches_creees_par_script(self):
        vide = self.fiche("Audrey Abadie-Amiel")
        a_la_main = self.fiche("François Patriat", added_by=1)
        deja = self.fiche("Grégory Allione", gender="Non précisé")
        journaliste = self.fiche("Audrey Abadie-Amiel", contact_type="Journaliste")
        bg.backfill(self.db, bg.known_genders(), commit=True)
        self.assertEqual(self.genre(vide), "Femme")
        self.assertIsNone(self.genre(a_la_main))
        self.assertEqual(self.genre(deja), "Non précisé")
        self.assertIsNone(self.genre(journaliste))


if __name__ == "__main__":
    unittest.main()
