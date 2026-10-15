"""Un·e eurodéputé·e d'un autre pays n'est pas « plus au Parlement ».

La source ne contient que les 81 élu·es français·es. Estelle Ceulemans, élue
belge en exercice depuis juillet 2024 et ajoutée à la main après une
rencontre, n'y figure pas : elle était annoncée « Plus au Parlement » à chaque
passage du lundi, pendant des semaines. Le script ne sait pas qui a quitté le
Parlement, il sait seulement qui manque à la liste française.

Le discriminant est la SAISIE HUMAINE. On avait d'abord filtré sur
`fiche_url`, et ça s'est retourné dans l'heure : on a posé son lien officiel
sur sa fiche — ce qu'on veut faire, pour le portrait et le lien — et elle est
repassée dans le filtre. `added_by` / `validated_by` disent qu'un humain a
créé ou validé la fiche, donc qu'elle ne vient pas de ce script.
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
            " role TEXT, fiche_url TEXT, added_by INTEGER, validated_by INTEGER)")
        self.db.executemany(
            "INSERT INTO persons (name, role, fiche_url, added_by, validated_by)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                # Importée par le script, toujours dans la liste.
                ("Leïla Chaibi", ROLE,
                 "https://www.europarl.europa.eu/meps/fr/197698", None, None),
                # Importée par le script, absente de la liste : vrai départ.
                ("Ancien Député", ROLE,
                 "https://www.europarl.europa.eu/meps/fr/000001", None, None),
                # Le cas réel : saisie et validée à la main, élue belge, ET
                # portant son lien officiel parce qu'on le lui a posé.
                ("Estelle Ceulemans", ROLE,
                 "https://www.europarl.europa.eu/meps/fr/256880", 16, 16),
                # Saisie à la main, sans validation ni lien officiel.
                ("Autre Pays", ROLE, "https://exemple.be/estelle", 16, None),
            ],
        )
        self.db.commit()

    def _absents(self, vus):
        return [
            nom
            for nom, fiche, ajout, valid in self.db.execute(
                "SELECT name, fiche_url, added_by, validated_by FROM persons"
                " WHERE role = ?", (ROLE,))
            if nom not in vus and pose_par_le_script(fiche)
            and ajout is None and valid is None
        ]

    def test_une_elue_dun_autre_pays_nest_jamais_signalee(self):
        self.assertNotIn("Estelle Ceulemans", self._absents({"Leïla Chaibi"}))

    def test_lui_poser_son_lien_officiel_ne_la_fait_pas_revenir(self):
        """Le piège du premier correctif : sa fiche PORTE le lien officiel."""
        self.assertTrue(pose_par_le_script(
            "https://www.europarl.europa.eu/meps/fr/256880"))
        self.assertNotIn("Estelle Ceulemans", self._absents(set()))

    def test_une_fiche_saisie_a_la_main_nest_pas_signalee(self):
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
        self.assertIn("ajout is None and valid is None", source)
        self.assertNotIn("Plus au Parlement", source)


if __name__ == "__main__":
    unittest.main()
