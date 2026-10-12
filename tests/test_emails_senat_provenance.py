"""L'adresse d'un·e sénateur·ice est déduite : ça doit se voir, et se mesurer.

Le Sénat ne publie aucune adresse dans son API. Celles de ses 348 membres sont
calculées depuis le nom. Deux conséquences testées ici :

- la provenance est enregistrée, et une meilleure source n'est jamais
  rétrogradée en « déduite » par la synchro du lundi ;
- le script de vérification sait distinguer « la convention se trompe » de
  « senat.fr ne publie rien », qui ne veulent pas du tout dire la même chose.
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import insert_senateurices as isen  # noqa: E402
import verifier_emails_senat as ver  # noqa: E402


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE persons (id INTEGER PRIMARY KEY, email TEXT);
            CREATE TABLE person_emails (
                email TEXT PRIMARY KEY, person_id INTEGER NOT NULL,
                source TEXT, created_at TEXT NOT NULL);
        """)
        self.db.execute("INSERT INTO persons (id, email) VALUES (1, 'f.p@senat.fr')")

    def source(self, email="f.p@senat.fr"):
        ligne = self.db.execute(
            "SELECT source FROM person_emails WHERE email = ?", (email,)
        ).fetchone()
        return ligne["source"] if ligne else None

    def test_la_provenance_est_enregistree(self):
        isen.noter_provenance(self.db, 1, "f.p@senat.fr", "2026-10-12")
        self.assertEqual(self.source(), "convention-senat")

    def test_une_source_meilleure_nest_jamais_retrogradee(self):
        # Cas réel : un·e bénévole a confirmé l'adresse, ou un échange l'a
        # apprise. La synchro hebdomadaire ne doit pas l'effacer tous les lundis.
        self.db.execute(
            "INSERT INTO person_emails VALUES ('f.p@senat.fr', 1, 'depot', 'hier')"
        )
        isen.noter_provenance(self.db, 1, "f.p@senat.fr", "2026-10-12")
        self.assertEqual(self.source(), "depot")

    def test_sans_adresse_rien_nest_ecrit(self):
        isen.noter_provenance(self.db, 1, None, "2026-10-12")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM person_emails").fetchone()[0], 0
        )


class VerificationTests(unittest.TestCase):
    GENS = [{"prenom": "François", "nom": "Patriat", "url": "/senateur/p.html"}]

    def test_une_adresse_conforme_est_comptee(self):
        pages = {"https://www.senat.fr/senateur/p.html": "écrire à f.patriat@senat.fr"}
        conformes, divergentes, muettes, _ = ver.verifier(
            self.GENS, lire=pages.get, pause=0
        )
        self.assertEqual(conformes, ["François Patriat"])
        self.assertEqual((divergentes, muettes), ([], []))

    def test_une_divergence_donne_la_ligne_a_reporter(self):
        pages = {"https://www.senat.fr/senateur/p.html": "f.patriat2@senat.fr"}
        _, divergentes, _, _ = ver.verifier(self.GENS, lire=pages.get, pause=0)
        self.assertEqual(divergentes, [("François Patriat",
                                        "f.patriat@senat.fr",
                                        "f.patriat2@senat.fr")])
        self.assertIn('"François Patriat": "f.patriat2@senat.fr"',
                      ver.rapport([], divergentes, [], []))

    def test_une_adresse_de_service_ne_compte_pas_comme_publiee(self):
        pages = {"https://www.senat.fr/senateur/p.html": "webmaster@senat.fr"}
        conformes, divergentes, muettes, _ = ver.verifier(
            self.GENS, lire=pages.get, pause=0
        )
        self.assertEqual((conformes, divergentes), ([], []))
        self.assertEqual(len(muettes), 1)

    def test_aucune_page_lisible_nest_pas_une_convention_fausse(self):
        # La distinction qui compte : un rapport muet ne doit surtout pas se
        # lire comme « la convention est fausse à 100 % ».
        texte = ver.rapport([], [], [("François Patriat", "f.patriat@senat.fr")], [])
        self.assertIn("PAS concluante", texte)
        self.assertNotIn("0 %", texte)

    def test_une_page_injoignable_est_signalee_et_nannule_pas_le_reste(self):
        def lire(url):
            raise OSError("timed out")
        _, _, _, injoignables = ver.verifier(self.GENS, lire=lire, pause=0)
        self.assertEqual(len(injoignables), 1)
        self.assertIn("timed out", ver.rapport([], [], [], injoignables))

    def test_le_taux_est_calcule_sur_les_seules_adresses_verifiables(self):
        texte = ver.rapport(["A", "B", "C"], [("D", "x", "y")],
                            [("E", "z")] * 10, [])
        self.assertIn("sur les 4 adresse(s) vérifiables", texte.lower())
        self.assertIn("75 %", texte)


if __name__ == "__main__":
    unittest.main()
