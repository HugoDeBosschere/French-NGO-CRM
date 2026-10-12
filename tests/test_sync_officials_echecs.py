"""La cause d'un échec de chambre doit se lire à la FIN du journal.

Le 12/10/2026, l'alerte Discord de `sync-officials` nommait les deux chambres
en échec (Assemblée nationale, Sénat) sans dire pourquoi : le message d'erreur
était imprimé à l'endroit de l'échec, des centaines de lignes avant la fin, et
l'alerte ne garde que la queue du journal. Les causes sont donc récapitulées
juste avant la sortie, et écrites dans `import_runs.detail`.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import sync_officials  # noqa: E402


class Suivi:
    """Le minimum de l'objet `run` d'importruns utilisé ici."""
    detail = None


class EchecsTests(unittest.TestCase):
    FAILURES = [
        ("Assemblée nationale", "HTTPError: HTTP Error 404: Not Found"),
        ("Sénat", "TimeoutError: timed out"),
    ]

    def _lancer(self):
        run = Suivi()
        sortie = io.StringIO()
        with redirect_stdout(sortie):
            with self.assertRaises(SystemExit) as ctx:
                sync_officials.signaler_echecs(self.FAILURES, run)
        return run, sortie.getvalue(), str(ctx.exception)

    def test_la_cause_est_dans_les_dernieres_lignes(self):
        _, journal, _ = self._lancer()
        queue = journal.strip().splitlines()[-2:]
        self.assertIn("404", " ".join(queue))
        self.assertIn("timed out", " ".join(queue))

    def test_chaque_chambre_est_nommee_avec_sa_cause(self):
        _, journal, _ = self._lancer()
        self.assertIn("Assemblée nationale : HTTPError: HTTP Error 404", journal)
        self.assertIn("Sénat : TimeoutError: timed out", journal)

    def test_le_detail_du_suivi_porte_les_causes(self):
        run, _, _ = self._lancer()
        self.assertIn("404", run.detail)
        self.assertIn("timed out", run.detail)

    def test_le_code_de_sortie_est_une_erreur_nommant_les_chambres(self):
        _, _, message = self._lancer()
        self.assertIn("Assemblée nationale, Sénat", message)


if __name__ == "__main__":
    unittest.main()
