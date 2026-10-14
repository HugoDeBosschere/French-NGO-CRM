"""Un poste sans titulaire déclaré ne doit pas créer de fiche sans nom.

Le journal de `sync-officials` listait « Ministre délégué·e (15) : - » avec une
entrée vide, et la fusion des doublons signalait chaque jour « 1 fiche sans
nom — à regarder à la main, elle ne peut pas être appariée ». Même cause :
une affectation sans personne à la source produisait un membre au nom vide.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))


class NomVideTests(unittest.TestCase):
    """La règle, isolée du téléchargement."""

    @staticmethod
    def _nom_complet(personne):
        prenom = (personne.get("prenom") or "").strip()
        nom = (personne.get("nom") or "").strip()
        return f"{prenom} {nom}".strip()

    def test_une_affectation_sans_personne_donne_un_nom_vide(self):
        self.assertEqual(self._nom_complet({}), "")

    def test_un_prenom_seul_suffit(self):
        self.assertEqual(self._nom_complet({"prenom": "Sébastien"}), "Sébastien")

    def test_des_espaces_seuls_comptent_pour_vide(self):
        self.assertEqual(self._nom_complet({"prenom": "  ", "nom": " "}), "")

    def test_le_script_ecarte_bien_les_noms_vides(self):
        # Le garde-fou doit être présent dans le code, et dire pourquoi.
        source = open(os.path.join(ROOT, "utils", "extract_gouvernement.py"),
                      encoding="utf-8").read()
        self.assertIn('if not f"{prenom} {nom}".strip():', source)
        self.assertIn("sans_nom.append", source)
        self.assertIn("Sans titulaire nommé à la source", source)


if __name__ == "__main__":
    unittest.main()
