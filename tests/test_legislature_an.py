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

    def test_un_sondage_en_panne_ne_fait_pas_tomber_la_chambre(self):
        """Le vrai incident du 13/10/2026, celui que ce code a causé.

        L'URL de la 18ᵉ législature, inexistante, a reçu « HTTP Error 503:
        Backend fetch failed » du cache de l'Assemblée au lieu d'un 404. La
        première version remontait l'erreur, et le dump de la 17ᵉ —
        disponible, vérifié au curl la minute suivante — n'était même pas
        téléchargé. On garde le plancher et on continue.
        """
        def existe(url):
            raise http_error(503)
        self.assertEqual(so.legislature_an(existe, journaliser=False), 17)

    def test_un_sondage_en_panne_ne_redescend_pas_sous_le_plancher(self):
        # La protection qui compte reste assurée : après l'adoption de la 18ᵉ,
        # un sondage en panne garde la 18ᵉ, jamais la 17ᵉ.
        so._retenir_plancher(18)

        def existe(url):
            raise TimeoutError("timed out")
        self.assertEqual(so.legislature_an(existe, journaliser=False), 18)

    def test_le_sondage_en_panne_est_dit_dans_le_journal(self):
        import io                                     # noqa: PLC0415
        from contextlib import redirect_stdout        # noqa: PLC0415

        def existe(url):
            raise http_error(503)
        flux = io.StringIO()
        with redirect_stdout(flux):
            so.legislature_an(existe)
        texte = flux.getvalue()
        self.assertIn("503", texte)
        self.assertIn("on garde 17", texte)

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


class TelechargementTests(unittest.TestCase):
    """Une panne de trente secondes ne doit pas coûter une semaine.

    Le 12/10/2026, le dump de l'Assemblée est passé à 18 h 08 et a répondu
    « HTTP Error 503: Backend fetch failed » à 18 h 09. Le script abandonnait
    au premier refus.
    """

    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        self.dest = os.path.join(self.dossier.name, "dump.zip")
        self.attentes = []
        self._vrai = so.urllib.request.urlopen
        self.addCleanup(setattr, so.urllib.request, "urlopen", self._vrai)

    class Flux:
        def __init__(self, contenu=b"zip"):
            self.contenu = contenu

        def read(self, *a):
            contenu, self.contenu = self.contenu, b""
            return contenu

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _reponses(self, *suite):
        restantes = list(suite)

        def faux(requete, timeout=None):
            reponse = restantes.pop(0)
            if isinstance(reponse, Exception):
                raise reponse
            return reponse

        so.urllib.request.urlopen = faux

    def test_un_503_passager_est_reessaye_et_le_fichier_arrive(self):
        self._reponses(http_error(503), self.Flux(b"zip"))
        so.download("https://exemple/dump.zip", self.dest,
                    dormir=self.attentes.append)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"zip")
        self.assertEqual(self.attentes, [5])

    def test_trois_echecs_remontent_lerreur(self):
        self._reponses(http_error(503), http_error(503), http_error(502))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            so.download("https://exemple/dump.zip", self.dest,
                        dormir=self.attentes.append)
        self.assertEqual(ctx.exception.code, 502)
        self.assertEqual(self.attentes, [5, 20])

    def test_un_404_nest_pas_reessaye(self):
        # Il ne changera pas dans vingt secondes : réessayer ne fait que
        # retarder l'alerte.
        self._reponses(http_error(404))
        with self.assertRaises(urllib.error.HTTPError):
            so.download("https://exemple/dump.zip", self.dest,
                        dormir=self.attentes.append)
        self.assertEqual(self.attentes, [])

    def test_une_coupure_reseau_est_reessayee(self):
        self._reponses(TimeoutError("timed out"), self.Flux(b"zip"))
        so.download("https://exemple/dump.zip", self.dest,
                    dormir=self.attentes.append)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"zip")
