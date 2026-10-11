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


class CacheTests(unittest.TestCase):
    """Le cache ne doit pas faire perdre ce qu'on vient d'ajouter.

    L'extracteur réutilise le dataset précédent pour ne pas rappeler l'API du
    Parlement, limitée en débit. Une fiche déjà en cache n'aurait donc jamais
    gagné le portrait ajouté après coup : le premier passage l'aurait posé
    pour tout le monde, et tous les suivants, servis par le cache, l'auraient
    perdu. Régression invisible pendant une semaine.
    """

    def setUp(self):
        import extract_eurodeputes                       # noqa: PLC0415
        self.extracteur = extract_eurodeputes

    def test_le_portrait_et_la_fiche_se_deduisent_de_l_identifiant(self):
        self.assertEqual(self.extracteur.photo_url(197698), PHOTO)
        self.assertEqual(self.extracteur.fiche_url(197698), FICHE)

    def test_une_entree_de_cache_sans_portrait_en_reçoit_un(self):
        # On rejoue ce que fait la branche de cache : tout sauf l'appel réseau.
        ancien = {"identifier": 197698, "nom_complet": "Leïla Chaibi",
                  "email": "l@ep.eu", "groupe": "GUE/NGL",
                  "url": FICHE}          # pas de clé « photo » : cache ancien
        rec = dict(ancien)
        rec["url"] = self.extracteur.fiche_url(197698)
        rec["photo"] = self.extracteur.photo_url(197698)
        self.assertEqual(rec["photo"], PHOTO)
        self.assertEqual(rec["url"], FICHE)


class InsertionCompleteTests(unittest.TestCase):
    """Le script rejoué en entier sur une base jouet.

    Deux défauts constatés sur la base de production, et qu'un test unitaire
    sur `poser_medias` seul n'aurait pas vus : le portrait n'était posé que
    sur les fiches dont le mandat changeait (1 sur 81), et l'API écrivant
    « Leila Chaibi » là où le CRM a « Leïla Chaibi », une fiche en double
    était créée pendant que l'originale était annoncée comme partie.
    """

    def setUp(self):
        import json                                      # noqa: PLC0415
        import insert_eurodeputes                        # noqa: PLC0415
        self.ins = insert_eurodeputes
        self.dossier = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dossier, "meetings.db")
        src = os.path.join(self.dossier, "eurodeputes_fr.json")
        self.ins.DB, self.ins.SRC = self.db_path, src

        db = sqlite3.connect(self.db_path)
        db.executescript("""
            CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT, role TEXT,
              political_group TEXT, stance TEXT, first_contacted TEXT,
              notes TEXT, circonscription TEXT, email TEXT, added_by INTEGER,
              validated_by INTEGER, created_at TEXT, photo_url TEXT,
              fiche_url TEXT);
            CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT,
              org_type TEXT, media_type TEXT, chambre TEXT, orientation TEXT,
              stance TEXT, link TEXT, notes TEXT, added_by INTEGER,
              validated_by INTEGER, created_at TEXT);
            CREATE TABLE person_organisations (person_id INTEGER NOT NULL,
              organisation_id INTEGER NOT NULL,
              PRIMARY KEY (person_id, organisation_id));
        """)
        db.execute(
            "INSERT INTO persons (name, role, political_group, stance, "
            "created_at) VALUES ('Leïla Chaibi', ?, 'La Gauche', 'Inconnu', ?)",
            (self.ins.ROLE, "2026-01-01"))
        db.commit(); db.close()

        json.dump([{"identifier": 197698, "nom_complet": "Leila Chaibi",
                    "groupe": next(iter(self.ins.GROUPE_TO_GROUP)),
                    "email": "l@ep.eu", "url": FICHE, "photo": PHOTO}],
                  open(src, "w", encoding="utf-8"))

    def _fiches(self):
        db = sqlite3.connect(self.db_path)
        lignes = db.execute(
            "SELECT name, photo_url, fiche_url FROM persons "
            "ORDER BY id").fetchall()
        db.close()
        return lignes

    def test_une_fiche_deja_presente_recoit_son_portrait(self):
        self.ins.main()
        self.assertEqual(self._fiches()[0][1], PHOTO)
        self.assertEqual(self._fiches()[0][2], FICHE)

    def test_une_difference_d_accent_ne_cree_pas_de_doublon(self):
        self.ins.main()
        self.assertEqual(len(self._fiches()), 1)
        self.assertEqual(self._fiches()[0][0], "Leïla Chaibi")

    def test_rejouer_le_script_ne_change_rien_de_plus(self):
        self.ins.main()
        avant = self._fiches()
        self.ins.main()
        self.assertEqual(self._fiches(), avant)
