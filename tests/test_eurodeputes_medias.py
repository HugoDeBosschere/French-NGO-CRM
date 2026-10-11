"""Portraits et fiches officielles des député·es européen·nes.

Les listes des quatre chambres sont tenues à jour chaque lundi, eurodéputé·es
comprises. Mais `sync_emails_from_elus.py`, qui pose les portraits et les
fiches, lit `elus.json` du site, lequel ne contient que les député·es et les
sénateur·ices. Les eurodéputé·es restaient donc sans portrait ni lien, alors
que le Parlement européen sert les deux à une adresse dérivée de
l'identifiant, sans appel d'API.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import insert_eurodeputes as inserteur  # noqa: E402

PHOTO = "https://www.europarl.europa.eu/mepphoto/197698.jpg"
FICHE = "https://www.europarl.europa.eu/meps/fr/197698"


class MediasTests(unittest.TestCase):
    def setUp(self):
        self.chemin = tempfile.mkstemp(suffix=".db")[1]
        self.addCleanup(os.unlink, self.chemin)
        self.db = sqlite3.connect(self.chemin)
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT,"
            " photo_url TEXT, fiche_url TEXT);"
            "INSERT INTO persons (id, name) VALUES (1, 'Leïla Chaibi');")
        self.db.commit()

    def _etat(self):
        return self.db.execute(
            "SELECT photo_url, fiche_url FROM persons WHERE id = 1").fetchone()

    def test_les_deux_sont_poses_quand_ils_manquent(self):
        self.assertEqual(inserteur.poser_medias(self.db, 1, PHOTO, FICHE), 1)
        self.assertEqual(self._etat(), (PHOTO, FICHE))

    def test_une_adresse_officielle_perimee_est_rafraichie(self):
        self.db.execute(
            "UPDATE persons SET photo_url = ? WHERE id = 1",
            ("https://www.europarl.europa.eu/mepphoto/000000.jpg",))
        inserteur.poser_medias(self.db, 1, PHOTO, FICHE)
        self.assertEqual(self._etat()[0], PHOTO)

    def test_une_photo_posee_a_la_main_survit(self):
        a_la_main = "https://photos.pauseia.fr/leila.jpg"
        self.db.execute("UPDATE persons SET photo_url = ? WHERE id = 1",
                        (a_la_main,))
        inserteur.poser_medias(self.db, 1, PHOTO, FICHE)
        self.assertEqual(self._etat()[0], a_la_main)

    def test_rien_a_changer_ne_compte_pas_pour_un_changement(self):
        inserteur.poser_medias(self.db, 1, PHOTO, FICHE)
        self.assertEqual(inserteur.poser_medias(self.db, 1, PHOTO, FICHE), 0)

    def test_une_base_sans_les_colonnes_ne_fait_pas_tomber_le_script(self):
        ancienne = tempfile.mkstemp(suffix=".db")[1]
        self.addCleanup(os.unlink, ancienne)
        db = sqlite3.connect(ancienne)
        self.addCleanup(db.close)
        db.executescript("CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT);"
                         "INSERT INTO persons (id, name) VALUES (1, 'X');")
        self.assertEqual(inserteur.poser_medias(db, 1, PHOTO, FICHE), 0)

    def test_un_domaine_sosie_n_est_pas_traite_comme_officiel(self):
        self.db.execute(
            "UPDATE persons SET photo_url = ? WHERE id = 1",
            ("https://evil-europarl.europa.eu.pirate.fr/p.jpg",))
        inserteur.poser_medias(self.db, 1, PHOTO, FICHE)
        self.assertIn("pirate.fr", self._etat()[0])


if __name__ == "__main__":
    unittest.main()
