"""Portraits et fiches officielles : remplis, puis tenus à jour.

Le script ne remplissait que les champs vides. Une valeur était donc juste une
fois, jamais rafraîchie : à chaque législature les portraits de l'Assemblée
changent d'URL, et les anciennes restaient en base en affichant du vide,
puisque l'interface efface l'image cassée.

Rafraîchir, oui — mais seulement ce qui vient manifestement de la source
officielle. Une photo déposée à la main par l'équipe reste intouchable.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "utils", "sync_emails_from_elus.py")
sys.path.insert(0, os.path.join(ROOT, "utils"))

import sync_emails_from_elus as sync  # noqa: E402

AN = "https://www2.assemblee-nationale.fr/static/tribun/17/photos/42.jpg"
AN_NEUVE = "https://www2.assemblee-nationale.fr/static/tribun/18/photos/42.jpg"
FICHE = "https://www.assemblee-nationale.fr/dyn/deputes/OSX42"


class DomaineTests(unittest.TestCase):
    def test_les_sources_officielles_sont_reconnues(self):
        self.assertTrue(sync.pose_par_le_script(AN))
        self.assertTrue(sync.pose_par_le_script("https://www.senat.fr/senfic/x.html"))

    def test_une_photo_posee_a_la_main_ne_l_est_pas(self):
        self.assertFalse(sync.pose_par_le_script("https://upload.wikimedia.org/p.jpg"))
        self.assertFalse(sync.pose_par_le_script(""))
        self.assertFalse(sync.pose_par_le_script(None))

    def test_un_domaine_sosie_ne_passe_pas(self):
        # `endswith` nu laisserait passer evil-assemblee-nationale.fr.
        self.assertFalse(
            sync.pose_par_le_script("https://evil-assemblee-nationale.fr/p.jpg"))


class SynchroTests(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dossier, "meetings.db")
        db = sqlite3.connect(self.db_path)
        db.executescript(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT,"
            " email TEXT, photo_url TEXT, fiche_url TEXT);")
        db.commit()
        db.close()

    def _elus(self, photo=AN_NEUVE, nb_remplissage=120):
        """elus.json avec une fiche utile, et assez d'autres pour passer le
        garde-fou « source presque vide »."""
        deputes = [{"nom": "Anne Dupont", "photo": photo, "contactUrl": FICHE,
                    "email": "a.dupont@assemblee-nationale.fr",
                    "emailConfidence": "high"}]
        for i in range(nb_remplissage):
            deputes.append({
                "nom": f"Témoin {i}",
                "photo": f"https://www2.assemblee-nationale.fr/p{i}.jpg",
                "contactUrl": f"https://www.assemblee-nationale.fr/d/{i}",
                "email": f"t{i}@assemblee-nationale.fr",
                "emailConfidence": "high"})
        chemin = os.path.join(self.dossier, "elus.json")
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump({"deputes": deputes, "senateurs": []}, f)
        return chemin

    def _personne(self, photo=None, fiche=None):
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO persons (name, photo_url, fiche_url) "
                   "VALUES ('Anne Dupont', ?, ?)", (photo, fiche))
        db.commit(); db.close()

    def _lancer(self, elus):
        return subprocess.run(
            [sys.executable, SCRIPT, "--db", self.db_path, "--elus", elus],
            capture_output=True, text=True)

    def _etat(self):
        db = sqlite3.connect(self.db_path)
        ligne = db.execute(
            "SELECT photo_url, fiche_url FROM persons WHERE name = 'Anne Dupont'"
        ).fetchone()
        db.close()
        return ligne

    def test_un_champ_vide_est_rempli(self):
        self._personne()
        self.assertEqual(self._lancer(self._elus()).returncode, 0)
        self.assertEqual(self._etat(), (AN_NEUVE, FICHE))

    def test_une_url_officielle_perimee_est_rafraichie(self):
        self._personne(photo=AN, fiche=FICHE)
        self._lancer(self._elus())
        self.assertEqual(self._etat()[0], AN_NEUVE)

    def test_une_photo_posee_a_la_main_survit(self):
        a_la_main = "https://photos.pauseia.fr/anne-dupont.jpg"
        self._personne(photo=a_la_main)
        self._lancer(self._elus())
        self.assertEqual(self._etat()[0], a_la_main)

    def test_rien_a_changer_ne_reecrit_rien(self):
        self._personne(photo=AN_NEUVE, fiche=FICHE)
        sortie = self._lancer(self._elus())
        self.assertIn("Portraits / fiches renseignés : 0", sortie.stdout)

    def test_une_source_presque_vide_est_un_echec_pas_un_silence(self):
        # Sans ce garde-fou, un elus.json tronqué rendrait 0 et l'alerte
        # systemd ne partirait jamais.
        self._personne()
        resultat = self._lancer(self._elus(nb_remplissage=2))
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("source probablement cassée", resultat.stderr)
        self.assertEqual(self._etat(), (None, None))


if __name__ == "__main__":
    unittest.main()
