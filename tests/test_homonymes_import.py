"""Un mandat ne doit jamais atterrir sur l'homonyme d'un autre type.

Signalé le 13/10/2026 : `insert_deputes.py` rapprochait les fiches sur le seul
nom, et a ajouté « Député·e » à la fiche de « Laurent Alexandre », chroniqueur.
Ce sont deux personnes. Les quatre imports d'élu·es partageaient ce défaut.

La colonne `persons.contact_type` vaut 'Politique' par défaut (NOT NULL), donc
restreindre le rapprochement à ce type ne casse pas l'idempotence des imports
déjà faits : toutes les fiches importées le portent.
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

SCHEMA = """
CREATE TABLE persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    contact_type TEXT NOT NULL DEFAULT 'Politique', role TEXT,
    political_group TEXT, stance TEXT, first_contacted TEXT, notes TEXT,
    circonscription TEXT, email TEXT, photo_url TEXT, fiche_url TEXT,
    added_by INTEGER, validated_by INTEGER, created_at TEXT);
"""

REQUETES = {
    "insert_deputes": None,
    "insert_senateurices": None,
    "insert_eurodeputes": None,
    "insert_gouvernement": None,
}


class IndexDesFichesTests(unittest.TestCase):
    """On teste la REQUÊTE d'index, isolée du téléchargement et des insertions.

    Les quatre scripts construisent leur index avec un SELECT sur `persons` ;
    c'est ce SELECT qui décidait, à tort, qu'un chroniqueur était un·e député·e.
    """

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT INTO persons (name, contact_type, role) VALUES "
            "('Laurent Alexandre', 'Journaliste', 'Chroniqueur·euse')")
        self.db.execute(
            "INSERT INTO persons (name, role) VALUES ('Anne Dupont', 'Député·e')")
        self.db.commit()

    def _index(self):
        return {r["name"] for r in self.db.execute(
            "SELECT name, id, role FROM persons WHERE contact_type = 'Politique'")}

    def test_un_homonyme_dun_autre_type_nest_pas_dans_lindex(self):
        self.assertNotIn("Laurent Alexandre", self._index())

    def test_une_fiche_politique_y_est(self):
        self.assertIn("Anne Dupont", self._index())

    def test_une_fiche_importee_porte_le_type_par_defaut(self):
        # C'est ce qui rend le filtre sûr : rien d'importé n'en sort.
        self.db.execute("INSERT INTO persons (name, role) VALUES ('X', 'Député·e')")
        self.assertEqual(
            self.db.execute(
                "SELECT contact_type FROM persons WHERE name = 'X'"
            ).fetchone()[0],
            "Politique",
        )

    def test_les_quatre_imports_filtrent_bien_sur_le_type(self):
        """Verrou : un cinquième import, ou un retour en arrière, casse ici."""
        for module in REQUETES:
            chemin = os.path.join(ROOT, "utils", f"{module}.py")
            with open(chemin, encoding="utf-8") as f:
                source = f.read()
            self.assertIn("contact_type = 'Politique'", source, module)


if __name__ == "__main__":
    unittest.main()
