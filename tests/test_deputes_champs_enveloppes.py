"""Un champ enveloppé par la source ne doit pas faire tomber la synchro.

Le 12/10/2026, `sync-officials` a échoué sur l'Assemblée nationale avec
« TypeError: unsupported operand type(s) for +=: 'dict' and 'str' », à la
ligne qui ajoutait le département au lieu de naissance : le dump, du XML
converti en JSON, livre un même champ tantôt comme chaîne, tantôt comme objet
(`{"#text": …}` quand il porte un attribut, `{"@xsi:nil": "true"}` quand il
est vide). Le cas était déjà traité pour `uid`, nulle part ailleurs.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import extract_deputes  # noqa: E402


def acteur(naissance, **ident_extra):
    """Le minimum qu'attend `build` pour un·e député·e en exercice."""
    ident = {"prenom": "Camille", "nom": "Durand", "civ": "Mme"}
    ident.update(ident_extra)
    return {
        "uid": {"#text": "PA840235"},
        "etatCivil": {"ident": ident, "infoNaissance": naissance},
        "mandats": {"mandat": [{"typeOrgane": "ASSEMBLEE", "dateDebut": "2024-07-18"}]},
    }


class ChampsEnveloppesTests(unittest.TestCase):
    def test_le_cas_tombe_en_production(self):
        """villeNais nue, depNais enveloppé : c'est ce qui a planté."""
        fiche = extract_deputes.build(
            acteur({"villeNais": "Nantes", "depNais": {"#text": "Loire-Atlantique"}})
        )
        self.assertEqual(fiche["lieu_naissance"], "Nantes (Loire-Atlantique)")

    def test_un_champ_vide_enveloppe_ne_laisse_pas_de_parenthese(self):
        fiche = extract_deputes.build(
            acteur({"villeNais": "Nantes", "depNais": {"@xsi:nil": "true"}})
        )
        self.assertEqual(fiche["lieu_naissance"], "Nantes")

    def test_sans_ville_le_departement_seul_ne_sort_pas(self):
        fiche = extract_deputes.build(acteur({"depNais": "Loire-Atlantique"}))
        self.assertEqual(fiche["lieu_naissance"], "")

    def test_le_nom_reste_lisible_si_la_source_l_enveloppe(self):
        fiche = extract_deputes.build(
            acteur({}, prenom={"#text": "Camille"}, nom={"#text": "Durand"})
        )
        self.assertEqual(fiche["nom"], "Camille Durand")
        self.assertEqual(fiche["url_an"].rsplit("_", 1)[-1], "PA840235")

    def test_une_adresse_enveloppee_ne_fait_pas_tomber_la_fiche(self):
        a = acteur({"villeNais": "Nantes"})
        a["adresses"] = {"adresse": [
            {"@xsi:type": "AdresseMail_Type", "valElec": {"#text": "c@an.fr"}},
            {"@xsi:type": "AdressePostale_Type",
             "codePostal": {"#text": "44000"}, "ville": "Nantes"},
        ]}
        fiche = extract_deputes.build(a)
        self.assertEqual(fiche["emails"], [{"email": "c@an.fr"}])
        self.assertEqual(fiche["adresses"], [{"adresse": "44000 Nantes"}])


if __name__ == "__main__":
    unittest.main()
