"""L'adresse d'un·e eurodéputé·e, quelle que soit la forme du champ hasEmail.

Le 19/10/2026 la synchro des eurodéputé·es est tombée sur une liste là où elle
attendait une chaîne, et toute la chambre a échoué. Ces cas figent la règle.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utils"))

from extract_eurodeputes import adresse  # noqa: E402


class TestAdresse(unittest.TestCase):
    def test_chaine_simple(self):
        self.assertEqual(adresse("mailto:jean.dupont@europarl.europa.eu"),
                         "jean.dupont@europarl.europa.eu")

    def test_sans_prefixe(self):
        self.assertEqual(adresse("jean.dupont@europarl.europa.eu"),
                         "jean.dupont@europarl.europa.eu")

    def test_liste_prefere_la_forme_canonique(self):
        """Le cas qui a fait tomber la synchro : deux adresses pour une personne."""
        self.assertEqual(
            adresse(["mailto:jean.dupont@ep.europa.eu",
                     "mailto:jean.dupont@europarl.europa.eu"]),
            "jean.dupont@europarl.europa.eu")

    def test_liste_sans_forme_canonique_prend_la_premiere(self):
        self.assertEqual(adresse(["mailto:a@ep.europa.eu", "mailto:b@ep.europa.eu"]),
                         "a@ep.europa.eu")

    def test_liste_json_ld(self):
        self.assertEqual(adresse([{"id": "mailto:jean.dupont@europarl.europa.eu"}]),
                         "jean.dupont@europarl.europa.eu")

    def test_rien_rend_none(self):
        """Pas d'adresse : l'élu·e est signalé·e, la chambre n'échoue pas."""
        for valeur in (None, "", "   ", [], ["mailto:"], {}, 42, [None]):
            with self.subTest(valeur=valeur):
                self.assertIsNone(adresse(valeur))


if __name__ == "__main__":
    unittest.main()
