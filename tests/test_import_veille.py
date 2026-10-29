"""Les passages sur l'IA de la veille des élu·es, sur la fiche de chaque élu·e.

Demande de Romain (27/10/2026) : quand on ouvre la fiche d'un·e élu·e dans le
CRM, voir ce qu'il ou elle a dit sur l'IA, d'après la veille
(veille.pauseia.fr). utils/import_veille.py range les passages de
synthese_elus.json dans `veille_passages` ; la fiche les affiche.

Ce qui compte le plus : ne jamais afficher un passage sur la mauvaise fiche.
D'où le rapprochement par identifiant officiel d'abord, et le refus de deviner
entre deux homonymes.

Lancer depuis la racine :  python3 -m unittest discover -s tests
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "utils"))

os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
os.environ.setdefault("APP_PASSWORD", "x")
import app  # noqa: E402
import import_veille as iv  # noqa: E402


def base_au_schema_de_l_application():
    chemin = tempfile.mkstemp(suffix=".db")[1]
    ancien, app.DB_PATH = app.DB_PATH, Path(chemin)
    try:
        app.init_db()
    finally:
        app.DB_PATH = ancien
    return chemin


def passage(extrait="L'intelligence artificielle pose un risque d'extinction.",
            date="2026-10-01", lien="https://www.assemblee-nationale.fr/c/1"):
    debut = extrait.find("extinction")
    marques = [[debut, debut + len("extinction")]] if debut >= 0 else []
    return {"date": date, "type": "séance", "poids": 10,
            "mots": {"Risques extrêmes": ["extinction"]},
            "extrait": extrait, "marques": marques, "lien": lien}


def personne(nom, chambre, ids=(), passages=None):
    return {"nom": nom, "chambre": chambre, "ids_officiels": list(ids),
            "passages": passages if passages is not None else [passage()]}


class Base(unittest.TestCase):
    def setUp(self):
        self.chemin = base_au_schema_de_l_application()
        self.db = sqlite3.connect(self.chemin)
        self.addCleanup(self.db.close)

    def fiche(self, nom, role, fiche_url=None):
        return self.db.execute(
            "INSERT INTO persons (name, role, political_group, stance, fiche_url, "
            "created_at) VALUES (?, ?, '', 'Inconnue', ?, '2026-01-01')",
            (nom, role, fiche_url)).lastrowid

    def importer(self, *personnes, genere_le="2026-10-29"):
        self.db.commit()
        return iv.importer(self.db, {"genere_le": genere_le,
                                     "personnes": list(personnes)})

    def passages_de(self, pid):
        return self.db.execute(
            "SELECT extrait FROM veille_passages WHERE person_id = ?", (pid,)).fetchall()


class Identifiants(unittest.TestCase):
    def test_lus_dans_les_liens_officiels(self):
        self.assertEqual(iv.identifiant_de_fiche(
            "https://www.assemblee-nationale.fr/dyn/deputes/PA1008"), "PA1008")
        self.assertEqual(iv.identifiant_de_fiche(
            "https://www.senat.fr/senateur/sido_bruno01058x.html"), "01058X")
        self.assertEqual(iv.identifiant_de_fiche(
            "https://www.europarl.europa.eu/meps/fr/256869"), "256869")
        self.assertEqual(iv.identifiant_de_fiche("https://exemple.org/x"), "")
        self.assertEqual(iv.identifiant_de_fiche(None), "")


class Rapprochement(Base):
    def test_par_identifiant_meme_si_le_nom_differe(self):
        # La fiche dit « Bruno Sido », la veille « M. Sido (Bruno) » : seul
        # l'identifiant peut les relier.
        pid = self.fiche("Bruno Sido", "Sénateur·ice",
                         "https://www.senat.fr/senateur/sido_bruno01058x.html")
        r = self.importer(personne("Sido Bruno", "Sénat", ids=["01058X"]))
        self.assertEqual(r["comptes"], {"identifiant": 1})
        self.assertEqual(len(self.passages_de(pid)), 1)

    def test_par_nom_sans_accents_ni_majuscules(self):
        pid = self.fiche("Grégory Allione", "Député·e européen·ne")
        self.importer(personne("GREGORY ALLIONE", "Parlement européen"))
        self.assertEqual(len(self.passages_de(pid)), 1)

    def test_homonymes_departages_par_la_fonction(self):
        deputee = self.fiche("Marie Dupont", "Député·e")
        self.fiche("Marie Dupont", "Journaliste")
        r = self.importer(personne("Marie Dupont", "Assemblée nationale"))
        self.assertEqual(r["comptes"], {"nom et fonction": 1})
        self.assertEqual(len(self.passages_de(deputee)), 1)

    def test_homonymes_indiscernables_laisses_de_cote(self):
        a = self.fiche("Jean Martin", "Député·e")
        b = self.fiche("Jean Martin", "Député·e")
        r = self.importer(personne("Jean Martin", "Assemblée nationale"))
        self.assertEqual(r["comptes"], {"homonymes": 1})
        self.assertEqual(self.passages_de(a) + self.passages_de(b), [])

    def test_personnalite_auditionnee_jamais_rapprochee_par_le_nom(self):
        pid = self.fiche("Yoshua Bengio", "Personnalité publique")
        r = self.importer(personne("Yoshua Bengio", "Personnalités auditionnées"))
        self.assertEqual(r["comptes"], {"personnalité auditionnée": 1})
        self.assertEqual(self.passages_de(pid), [])

    def test_ancien_fichier_sans_identifiants(self):
        pid = self.fiche("Alain David", "Député·e")
        p = personne("Alain David", "Assemblée nationale")
        del p["ids_officiels"]
        self.importer(p)
        self.assertEqual(len(self.passages_de(pid)), 1)


class Reecriture(Base):
    def test_relancer_ne_cree_pas_de_doublon_et_suit_la_veille(self):
        pid = self.fiche("Alain David", "Député·e",
                         "https://www.assemblee-nationale.fr/dyn/deputes/PA1008")
        p = personne("Alain David", "Assemblée nationale", ids=["PA1008"])
        self.importer(p)
        self.importer(p)
        self.assertEqual(len(self.passages_de(pid)), 1)
        # La veille corrige un extrait : la fiche suit.
        p["passages"] = [passage(extrait="Nouvel extrait sur l'IA.")]
        self.importer(p)
        self.assertEqual(self.passages_de(pid), [("Nouvel extrait sur l'IA.",)])

    def test_simulation_n_ecrit_rien(self):
        self.fiche("Alain David", "Député·e")
        self.db.commit()
        r = iv.importer(self.db, {"personnes": [personne("Alain David",
                                                         "Assemblée nationale")]},
                        dry_run=True)
        self.assertEqual(r["passages"], 1)
        n = self.db.execute("SELECT COUNT(*) FROM veille_passages").fetchone()[0]
        self.assertEqual(n, 0)

    def test_lien_non_https_ecarte(self):
        pid = self.fiche("Alain David", "Député·e")
        self.importer(personne("Alain David", "Assemblée nationale",
                               passages=[passage(lien="javascript:alert(1)")]))
        lien = self.db.execute("SELECT lien FROM veille_passages WHERE person_id = ?",
                               (pid,)).fetchone()[0]
        self.assertEqual(lien, "")

    def test_fichier_vide_ne_vide_pas_la_table(self):
        pid = self.fiche("Alain David", "Député·e")
        self.importer(personne("Alain David", "Assemblée nationale"))
        vide = tempfile.mkstemp(suffix=".json")[1]
        Path(vide).write_text(json.dumps({"genere_le": "2026-10-30", "personnes": []}))
        with mock.patch.object(sys, "argv", ["import_veille.py", "--file", vide,
                                             "--db", self.chemin]):
            with self.assertRaises(SystemExit):
                iv.main()
        self.assertEqual(len(self.passages_de(pid)), 1)


class Fiche(Base):
    def setUp(self):
        super().setUp()
        self.ancien, app.DB_PATH = app.DB_PATH, Path(self.chemin)
        self.addCleanup(setattr, app, "DB_PATH", self.ancien)
        self.client = app.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True

    def page(self, pid):
        return self.client.get(f"/people/{pid}").get_data(as_text=True)

    def test_section_avec_mots_cles_en_gras_et_source(self):
        pid = self.fiche("Alain David", "Député·e")
        self.importer(personne("Alain David", "Assemblée nationale"))
        html = self.page(pid)
        self.assertIn("Sur l'IA, d'après la veille", html)
        self.assertIn("<strong>extinction</strong>.", html)
        self.assertIn('href="https://www.assemblee-nationale.fr/c/1"', html)
        self.assertIn("relire la source", html)

    def test_extrait_echappe(self):
        pid = self.fiche("Alain David", "Député·e")
        self.importer(personne("Alain David", "Assemblée nationale", passages=[
            passage(extrait="<script>alert(1)</script> intelligence artificielle")]))
        html = self.page(pid)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_aucune_section_sans_passage(self):
        pid = self.fiche("Alain David", "Député·e")
        self.db.commit()
        self.assertNotIn("d'après la veille", self.page(pid))

    def test_au_dela_de_cinq_passages_le_reste_est_replie(self):
        pid = self.fiche("Alain David", "Député·e")
        self.importer(personne("Alain David", "Assemblée nationale", passages=[
            passage(date=f"2026-10-0{i}") for i in range(1, 8)]))
        html = self.page(pid)
        self.assertIn("Voir les 2 autres passages", html)
        self.assertEqual(html.count('class="card veille-passage"'), 7)


class Decoupage(unittest.TestCase):
    def test_marques_hors_bornes_ou_chevauchantes(self):
        self.assertEqual(app._segments_veille("abcdef", [[1, 3], [2, 4], [5, 99]]),
                         [("a", False), ("bc", True), ("d", True), ("e", False),
                          ("f", True)])
        self.assertEqual(app._segments_veille("abc", [["x", 2]]), [("abc", False)])


if __name__ == "__main__":
    unittest.main()
