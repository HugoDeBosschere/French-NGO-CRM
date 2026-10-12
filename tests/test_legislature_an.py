"""Une dissolution ne doit plus bloquer la synchro des député·es.

L'URL du dump de l'Assemblée contient le numéro de législature. En dur, il
condamnait la synchro à échouer en 404 à chaque élection générale, jusqu'à ce
qu'un humain pose AN_LEGISLATURE. On demande donc à la source si la
législature suivante existe, avec deux règles qui comptent plus que la
découverte elle-même :

- une panne n'est pas une absence : seul un 404 fait conclure « pas publiée » ;
- on ne redescend jamais : une fois la 18ᵉ adoptée, un 404 passager sur son
  dump ne doit pas réimporter les 577 député·es de la 17ᵉ comme s'ils
  siégeaient encore.
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import sync_officials as so  # noqa: E402


def http_error(code):
    return urllib.error.HTTPError("u", code, "", {}, None)


class LegislatureTests(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        self._etat = so.AN_LEGISLATURE_ETAT
        so.AN_LEGISLATURE_ETAT = os.path.join(self.dossier.name, "an.json")
        self.addCleanup(setattr, so, "AN_LEGISLATURE_ETAT", self._etat)
        self._force = os.environ.pop("AN_LEGISLATURE", None)
        if self._force is not None:
            self.addCleanup(os.environ.__setitem__, "AN_LEGISLATURE", self._force)

    @staticmethod
    def _publiees(*numeros):
        def existe(url):
            return any(f"/repository/{n}/" in url for n in numeros)
        return existe

    def test_sans_nouveau_dump_on_garde_la_legislature_connue(self):
        self.assertEqual(
            so.legislature_an(self._publiees(17), journaliser=False), 17
        )

    def test_un_nouveau_dump_est_adopte_tout_seul(self):
        self.assertEqual(
            so.legislature_an(self._publiees(17, 18), journaliser=False), 18
        )

    def test_la_legislature_adoptee_est_memorisee(self):
        so.legislature_an(self._publiees(17, 18), journaliser=False)
        with open(so.AN_LEGISLATURE_ETAT, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["legislature"], 18)

    def test_on_ne_redescend_jamais_apres_un_404_passager(self):
        # La 18ᵉ a été adoptée la semaine dernière ; cette semaine son dump
        # répond 404 et la 19ᵉ n'existe pas. Réimporter la 17ᵉ serait pire que
        # de ne rien faire : on reste sur la 18ᵉ et c'est le téléchargement
        # qui échouera, avec alerte.
        so._retenir_plancher(18)
        self.assertEqual(
            so.legislature_an(self._publiees(17), journaliser=False), 18
        )

    def test_une_panne_nest_pas_une_absence(self):
        def existe(url):
            raise http_error(503)
        with self.assertRaises(urllib.error.HTTPError):
            so.legislature_an(existe, journaliser=False)

    def test_un_etat_illisible_ne_bloque_pas(self):
        with open(so.AN_LEGISLATURE_ETAT, "w", encoding="utf-8") as f:
            f.write("pas du json")
        self.assertEqual(
            so.legislature_an(self._publiees(17), journaliser=False), 17
        )

    def test_la_variable_denvironnement_gagne_et_ne_sonde_rien(self):
        def existe(url):
            raise AssertionError("aucune requête ne doit partir")
        os.environ["AN_LEGISLATURE"] = "42"
        self.addCleanup(os.environ.pop, "AN_LEGISLATURE", None)
        self.assertEqual(so.legislature_an(existe, journaliser=False), 42)

    def test_lurl_porte_bien_le_numero(self):
        self.assertIn("/repository/18/", so.an_zip_url(18))


class ExisteTests(unittest.TestCase):
    """`_existe` lit la réponse HTTP, et seul un 404 vaut « absente »."""

    def setUp(self):
        self.appels = []
        self._vrai = so.urllib.request.urlopen

        def faux(requete, timeout=None):
            self.appels.append(requete.get_method())
            reponse = self.reponses.pop(0)
            if isinstance(reponse, Exception):
                raise reponse
            return reponse

        so.urllib.request.urlopen = faux
        self.addCleanup(setattr, so.urllib.request, "urlopen", self._vrai)

    class Flux:
        def close(self):
            pass

    def test_une_reponse_normale_vaut_publiee(self):
        self.reponses = [self.Flux()]
        self.assertTrue(so._existe("https://exemple/18.zip"))
        self.assertEqual(self.appels, ["HEAD"])

    def test_un_404_vaut_absente(self):
        self.reponses = [http_error(404)]
        self.assertFalse(so._existe("https://exemple/19.zip"))

    def test_un_serveur_qui_refuse_HEAD_est_redemande_en_GET(self):
        self.reponses = [http_error(405), self.Flux()]
        self.assertTrue(so._existe("https://exemple/18.zip"))
        self.assertEqual(self.appels, ["HEAD", "GET"])

    def test_un_404_au_second_essai_vaut_aussi_absente(self):
        self.reponses = [http_error(403), http_error(404)]
        self.assertFalse(so._existe("https://exemple/19.zip"))

    def test_une_panne_remonte(self):
        self.reponses = [http_error(500)]
        with self.assertRaises(urllib.error.HTTPError):
            so._existe("https://exemple/18.zip")


if __name__ == "__main__":
    unittest.main()
