"""Les deux défauts silencieux des listes d'élu·es, verrouillés par des tests.

Contexte : après le renouvellement sénatorial du 27 septembre 2026, le CRM ne
connaissait que 289 des 348 sénateurices. Le journal du 5 octobre le disait
sans le dire — « Skipped (already present): 290 » puis « UNMAPPED groups (58) »,
290 + 58 = 348 — et le travail se déclarait réussi.

Ces tests décrivent ce qui doit se passer à la place.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import elus_roster as er  # noqa: E402


class GroupeSansEtiquette(unittest.TestCase):
    """Un élu sans groupe déclaré doit ENTRER, pas disparaître."""

    CORRESPONDANCE = {"NI": "Non-inscrit (Sénat)"}

    def test_aucun_donne_le_groupe_dattente_et_nest_pas_une_anomalie(self):
        # « Aucun » est ce que renvoie l'API du Sénat pour une personne
        # fraîchement élue : un état normal, pas une erreur.
        groupe, anomalie = er.resoudre_groupe("Aucun", self.CORRESPONDANCE, "Sénat")
        self.assertEqual(groupe, "Sans groupe déclaré (Sénat)")
        self.assertIsNone(anomalie)

    def test_vide_et_absent_sont_traites_pareil(self):
        for valeur in (None, "", "   ", "sans groupe", "NR", "-"):
            groupe, anomalie = er.resoudre_groupe(valeur, self.CORRESPONDANCE, "Sénat")
            self.assertEqual(groupe, "Sans groupe déclaré (Sénat)", valeur)
            self.assertIsNone(anomalie, valeur)

    def test_un_groupe_connu_passe_normalement(self):
        self.assertEqual(
            er.resoudre_groupe("NI", self.CORRESPONDANCE, "Sénat"),
            ("Non-inscrit (Sénat)", None),
        )

    def test_un_libelle_inconnu_est_une_anomalie_mais_ne_perd_personne(self):
        # La source a changé de vocabulaire : il faut le signaler. Mais la
        # personne entre quand même, avec le groupe d'attente — perdre un élu
        # est pire que lui donner provisoirement le mauvais groupe.
        groupe, anomalie = er.resoudre_groupe("GRP-2027", self.CORRESPONDANCE, "Sénat")
        self.assertEqual(groupe, "Sans groupe déclaré (Sénat)")
        self.assertEqual(anomalie, "GRP-2027")


class CumulDeMandats(unittest.TestCase):
    """Une fiche existante doit RECEVOIR le nouveau mandat, pas l'ignorer."""

    ORDRE = ["Ministre", "Député·e", "Sénateur·ice", "Président·e de région"]

    def test_le_mandat_sajoute_a_celui_deja_porte(self):
        # Le cas réel : Christine Bost, présidente de région, élue au Sénat.
        self.assertEqual(
            er.merge_roles("Président·e de région", "Sénateur·ice", self.ORDRE),
            "Sénateur·ice, Président·e de région",
        )

    def test_rejouer_limport_ne_duplique_pas_le_role(self):
        deja = "Sénateur·ice, Président·e de région"
        self.assertEqual(er.merge_roles(deja, "Sénateur·ice", self.ORDRE), deja)

    def test_une_fiche_sans_role_recoit_simplement_le_mandat(self):
        for vide in (None, ""):
            self.assertEqual(er.merge_roles(vide, "Sénateur·ice", self.ORDRE),
                             "Sénateur·ice")

    def test_un_role_saisi_a_la_main_nest_jamais_efface(self):
        # Quelqu'un a tapé « Attaché parlementaire » dans l'interface : ce
        # travail humain survit à l'import automatique.
        obtenu = er.merge_roles("Attaché parlementaire", "Sénateur·ice", self.ORDRE)
        self.assertIn("Attaché parlementaire", obtenu)
        self.assertIn("Sénateur·ice", obtenu)


class CoherenceAvecLApp(unittest.TestCase):
    """Les garde-fous qui empêchent d'écrire des valeurs inaffichables."""

    def test_chaque_groupe_dattente_existe_dans_app_py(self):
        groupes = er.app_constant("POLITICAL_GROUPS")
        for chambre, attendu in er.GROUPE_ATTENTE.items():
            self.assertIn(attendu, groupes[chambre], chambre)

    def test_une_cible_absente_dapp_py_refuse_de_tourner(self):
        with self.assertRaises(SystemExit):
            er.verifier_groupes("Sénat", {"X": "Groupe qui n'existe pas"})

    def test_les_correspondances_reelles_sont_toutes_valides(self):
        # Verrou de non-régression : renommer un groupe dans app.py sans
        # toucher aux tables de correspondance casse ce test, pas la prod.
        import insert_senateurices as isen
        er.verifier_groupes("Sénat", isen.GROUPE_TO_GROUP)


if __name__ == "__main__":
    unittest.main()
