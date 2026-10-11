"""Le rattrapage des corps de courriels citoyens déjà importés.

Jusqu'en octobre 2026 le pipeline gardait l'objet et le nom de l'élu·e, pas le
message. Ces courriels-là n'auront jamais de corps par un import ordinaire :
la déduplication se fait sur le Message-ID, et le leur est déjà noté.

Le risque de ce rattrapage tient en une phrase : attribuer les mots d'un
citoyen au mauvais enregistrement. Pour une campagne, date et objet sont
souvent identiques d'un citoyen à l'autre — c'est le principe d'une campagne.
D'où ces tests, qui portent tous sur l'appariement.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import backfill_campagne_corps as rattrapage  # noqa: E402

ETIQ = "Mail d'un citoyen à X : « Objet »"


class FausseLigne(dict):
    """Un sqlite3.Row ne se fabrique pas à la main ; un dict suffit ici."""
    def __getitem__(self, cle):
        return self.get(cle)


def ligne(id_, **champs):
    base = {"id": id_, "mail_date": "2026-10-08",
            "subject": "Les risques de l'IA", "summary": ETIQ,
            "direction": "sent", "document_stored_name": None,
            "validated_by": None, "received_by": None}
    base.update(champs)
    return FausseLigne(base)


def message(corps):
    return (corps, object(), [(1, "Anne Dupont")])


K = ("2026-10-08", "Les risques de l'IA", (1,))


class IntacteTests(unittest.TestCase):
    def test_une_ligne_jamais_touchee_est_intacte(self):
        self.assertTrue(rattrapage.intacte(ligne(1)))

    def test_une_piece_jointe_la_rend_particuliere(self):
        self.assertFalse(rattrapage.intacte(
            ligne(1, document_stored_name="scan.pdf")))

    def test_une_validation_la_rend_particuliere(self):
        self.assertFalse(rattrapage.intacte(ligne(1, validated_by=3)))

    def test_un_resume_reecrit_a_la_main_la_rend_particuliere(self):
        self.assertFalse(rattrapage.intacte(
            ligne(1, summary="Relance importante, à suivre")))


class AppariementTests(unittest.TestCase):
    def test_un_message_pour_une_ligne_est_attribue(self):
        paires, ambigus, manquants = rattrapage.apparier(
            {K: [ligne(1)]}, {K: [message("Bonjour Madame")]})
        self.assertEqual(len(paires), 1)
        self.assertEqual(paires[0][0]["id"], 1)
        self.assertEqual(paires[0][1][0], "Bonjour Madame")
        self.assertEqual((ambigus, manquants), ([], 0))

    def test_une_ligne_sans_message_est_comptee_pas_devinee(self):
        paires, _a, manquants = rattrapage.apparier({K: [ligne(1)]}, {})
        self.assertEqual(paires, [])
        self.assertEqual(manquants, 1)

    def test_plusieurs_lignes_intactes_sont_interchangeables(self):
        # Deux citoyens, même objet, même jour, même élue : les deux lignes
        # sont indiscernables, donc la base obtient le même contenu quel que
        # soit l'ordre. On attribue.
        paires, ambigus, _m = rattrapage.apparier(
            {K: [ligne(1), ligne(2)]},
            {K: [message("Premier"), message("Second")]})
        self.assertEqual(len(paires), 2)
        self.assertEqual(ambigus, [])
        self.assertEqual({p[0]["id"] for p in paires}, {1, 2})

    def test_un_lot_dont_une_ligne_a_ete_annotee_est_laisse_de_cote(self):
        # Là, les lignes ne sont plus interchangeables : l'une porte un
        # travail humain. Attribuer au hasard mettrait les mots d'un citoyen
        # sur l'enregistrement d'un autre.
        paires, ambigus, _m = rattrapage.apparier(
            {K: [ligne(1), ligne(2, document_stored_name="scan.pdf")]},
            {K: [message("Premier"), message("Second")]})
        self.assertEqual(paires, [])
        self.assertEqual(len(ambigus), 1)

    def test_plus_de_messages_que_de_lignes_ne_cree_jamais_de_ligne(self):
        paires, _a, _m = rattrapage.apparier(
            {K: [ligne(1)]},
            {K: [message("Premier"), message("Second"), message("Tiers")]})
        self.assertEqual(len(paires), 1)

    def test_plus_de_lignes_que_de_messages_en_remplit_une_partie(self):
        # « Faisons ce qu'on peut » : deux lignes sur trois valent mieux que
        # zéro, et la troisième reste simplement sans corps.
        paires, ambigus, _m = rattrapage.apparier(
            {K: [ligne(1), ligne(2), ligne(3)]},
            {K: [message("Premier"), message("Second")]})
        self.assertEqual(len(paires), 2)
        self.assertEqual(ambigus, [])

    def test_des_cles_differentes_ne_se_melangent_pas(self):
        autre = ("2026-10-09", "Les risques de l'IA", (1,))
        paires, _a, manquants = rattrapage.apparier(
            {K: [ligne(1)], autre: [ligne(2)]},
            {K: [message("Du 8")]})
        self.assertEqual(len(paires), 1)
        self.assertEqual(paires[0][1][0], "Du 8")
        self.assertEqual(manquants, 1)

    def test_le_meme_objet_pour_deux_elues_differentes_reste_distinct(self):
        # La clé porte l'ensemble des destinataires : deux citoyens écrivant
        # le même jour le même objet à deux élu·es différent·es ne sont pas
        # confondus.
        a = ("2026-10-08", "Les risques", (1,))
        b = ("2026-10-08", "Les risques", (2,))
        paires, _am, _m = rattrapage.apparier(
            {a: [ligne(1)], b: [ligne(2)]},
            {a: [message("Pour la première")], b: [message("Pour la seconde")]})
        corps = {p[0]["id"]: p[1][0] for p in paires}
        self.assertEqual(corps, {1: "Pour la première", 2: "Pour la seconde"})


if __name__ == "__main__":
    unittest.main()
