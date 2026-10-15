"""Un·e eurodéputé·e d'un autre pays n'est pas « plus au Parlement ».

La source ne contient que les 81 élu·es français·es. Estelle Ceulemans, élue
belge en exercice depuis juillet 2024 et ajoutée à la main après une
rencontre, n'y figure pas : elle était annoncée « Plus au Parlement » à chaque
passage du lundi, pendant des semaines. Le script ne sait pas qui a quitté le
Parlement, il sait seulement qui manque à la liste française.

Le discriminant est `fiche_url` : c'est le script qui l'écrit, et seulement
pour les gens de cette liste. Une fiche qui la porte relève de lui ; une fiche
sans elle vient d'ailleurs.
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

from sync_emails_from_elus import pose_par_le_script  # noqa: E402

ROLE = "Député·e européen·ne"


class DepartsTests(unittest.TestCase):
    """La règle, isolée du téléchargement et des insertions."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT,"
            " role TEXT, fiche_url TEXT)")
        self.db.executemany(
            "INSERT INTO persons (name, role, fiche_url) VALUES (?, ?, ?)",
            [
                # Importée par le script, toujours dans la liste.
                ("Leïla Chaibi", ROLE,
                 "https://www.europarl.europa.eu/meps/fr/197698"),
                # Importée par le script, absente de la liste : vrai départ.
                ("Ancien Député", ROLE,
                 "https://www.europarl.europa.eu/meps/fr/000001"),
                # Ajoutée à la main, élue belge : n'a jamais été dans la liste.
                ("Estelle Ceulemans", ROLE, None),
                # Ajoutée à la main avec un lien quelconque.
                ("Autre Pays", ROLE, "https://exemple.be/estelle"),
            ],
        )
        self.db.commit()

    def _absents(self, vus):
        return [
            nom
            for nom, fiche in self.db.execute(
                "SELECT name, fiche_url FROM persons WHERE role = ?", (ROLE,))
            if nom not in vus and pose_par_le_script(fiche)
        ]

    def test_une_elue_dun_autre_pays_nest_jamais_signalee(self):
        self.assertNotIn("Estelle Ceulemans", self._absents({"Leïla Chaibi"}))

    def test_un_lien_non_officiel_ne_la_fait_pas_entrer_non_plus(self):
        self.assertNotIn("Autre Pays", self._absents({"Leïla Chaibi"}))

    def test_un_vrai_depart_est_toujours_signale(self):
        self.assertIn("Ancien Député", self._absents({"Leïla Chaibi"}))

    def test_qui_est_dans_la_liste_nest_pas_un_depart(self):
        self.assertNotIn("Leïla Chaibi", self._absents({"Leïla Chaibi"}))

    def test_le_script_porte_bien_le_filtre(self):
        """Verrou : un retour en arrière sur insert_eurodeputes casse ici."""
        with open(os.path.join(ROOT, "utils", "insert_eurodeputes.py"),
                  encoding="utf-8") as f:
            source = f.read()
        self.assertIn("pose_par_le_script(fiche)", source)
        self.assertNotIn("Plus au Parlement", source)


if __name__ == "__main__":
    unittest.main()
