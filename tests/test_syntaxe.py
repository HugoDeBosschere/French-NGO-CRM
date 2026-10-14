"""Tout fichier Python du dépôt doit compiler, même celui qu'aucun test n'importe.

Le 14/10/2026, `utils/diagnostic_membre.py` portait une f-string à guillemets
doubles imbriqués, autorisée depuis Python 3.12 seulement. Le conteneur tourne
en 3.12, donc la production et la machine de développement n'ont rien vu ; la
CI, qui teste aussi 3.11, a refusé le dépôt. Elle ne l'a vu que parce qu'un
test importait ce module : un script sans test aurait cassé en silence jusqu'au
jour où un timer l'exécute à 5 h du matin.

Ce test compile donc chaque fichier suivi par git, avec l'interprète courant.
"""
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SyntaxeTests(unittest.TestCase):
    def test_chaque_fichier_python_compile(self):
        suivis = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertGreater(len(suivis), 20, "la liste des fichiers est suspecte")
        fautifs = []
        for relatif in suivis:
            chemin = os.path.join(ROOT, relatif)
            with open(chemin, encoding="utf-8") as f:
                source = f.read()
            try:
                compile(source, chemin, "exec")
            except SyntaxError as exc:
                fautifs.append(f"{relatif}:{exc.lineno} {exc.msg}")
        self.assertEqual(fautifs, [], "\n".join(fautifs))


if __name__ == "__main__":
    unittest.main()
