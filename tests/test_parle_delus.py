"""Le filet qui rattrape le citoyen écrivant depuis son adresse personnelle.

Le filtre de périmètre (maildomains.in_scope) demande une raison POSITIVE
d'entrer en file, parce que la règle Workspace copie aussi les mails personnels
des membres et que « rien ne distingue le journaliste qui écrit de son gmail du
médecin d'un membre ». Conséquence assumée, mais trop large : un citoyen qui
nous répond « j'ai obtenu un rendez-vous avec mon député » passait entre les
mailles.

Parler d'élu·es EST cette raison positive, pour une association dont c'est le
métier. Ces tests tiennent les deux bords : attraper ce cas, et ne pas attraper
le médecin.
"""
import email
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import import_member_mails as imm  # noqa: E402


def courriel(corps):
    brut = (
        "From: a@b.fr\r\nTo: c@d.fr\r\nSubject: test\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n\r\n" + corps
    ).encode("utf-8")
    return email.message_from_bytes(brut)


class ParleDElus(unittest.TestCase):
    def test_le_cas_vise_est_attrape(self):
        # Un citoyen répond à l'association après avoir écrit à son élu.
        self.assertTrue(imm.parle_delus(courriel(
            "Bonjour, j'ai obtenu un rendez-vous avec mon député la semaine "
            "prochaine, merci pour l'outil.")))

    def test_les_variantes_courantes(self):
        for corps in ("J'ai rencontré la sénatrice de mon département.",
                      "Le Sénat a publié le rapport.",
                      "Réunion à l'Assemblée nationale le 12.",
                      "Les parlementaires ont voté hier.",
                      "j ai ecrit a mon depute",          # sans accents
                      "Mes DÉPUTÉS ne répondent pas."):   # casse
            self.assertTrue(imm.parle_delus(courriel(corps)), corps)

    def test_la_vie_privee_des_membres_reste_dehors(self):
        for corps in ("Mon rendez-vous chez le médecin est reporté à mardi.",
                      "Facture n°2026-114, règlement sous 30 jours.",
                      "Les photos du week-end sont en ligne."):
            self.assertFalse(imm.parle_delus(courriel(corps)), corps)

    def test_les_mots_voisins_ne_declenchent_pas(self):
        # Frontières de mots : sans elles, « sénat » attrapait « sénatoriale »
        # de n'importe quelle revue de presse.
        for corps in ("La vie sénatoriale est un sujet de thèse.",
                      "Devis pour la députation des charges sociales."):
            self.assertFalse(imm.parle_delus(courriel(corps)), corps)

    def test_un_corps_illisible_ne_fait_pas_tomber_l_import(self):
        # Un courriel mal formé ne doit jamais coûter le balayage entier.
        class Cassé:
            def get(self, *_a, **_k):
                raise ValueError("corps illisible")
        self.assertFalse(imm.parle_delus(Cassé()))


if __name__ == "__main__":
    unittest.main()
