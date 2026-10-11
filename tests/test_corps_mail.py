"""La mise en forme du corps d'un courriel.

Il s'affichait dans un <pre> : les retours à la ligne durs posés par le client
d'envoi — autour de 75 caractères — étaient conservés tels quels, si bien
qu'une phrase se brisait au milieu quelle que soit la largeur de l'écran. Et
les listes arrivaient éclatées, le « 1. » seul sur sa ligne et son contenu deux
lignes plus bas : c'est ainsi que Gmail dégrade une liste HTML dans la partie
texte du message.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
os.environ.setdefault("APP_PASSWORD", "x")
import app  # noqa: E402

mise_en_forme = app.corps_mail


class CorpsMailTests(unittest.TestCase):
    def test_un_paragraphe_replie_est_recolle(self):
        rendu = mise_en_forme(
            "Nous vous écrivons en tant qu'association Pause IA,\n"
            "pour vous sensibiliser aux risques liés au développement\n"
            "exponentiel des systèmes d'IA.")
        self.assertEqual(rendu.count("<p>"), 1)
        self.assertIn("association Pause IA, pour vous sensibiliser", rendu)

    def test_une_ligne_vide_sépare_bien_deux_paragraphes(self):
        self.assertEqual(mise_en_forme("Bonjour,\n\nAu revoir.").count("<p>"), 2)

    def test_une_puce_seule_est_recollee_a_son_contenu(self):
        # Le cas Gmail : « 1. », une ligne vide, puis le texte.
        rendu = mise_en_forme("   1.\n\n   Depuis mercredi, la France\n"
                              "   est cosignataire.\n")
        self.assertIn('<p class="mail-puce">1. Depuis mercredi, la France '
                      'est cosignataire.</p>', rendu)

    def test_deux_points_de_liste_restent_deux_paragraphes(self):
        rendu = mise_en_forme("1. Premier point\nqui continue\n2. Deuxième point")
        self.assertEqual(rendu.count('class="mail-puce"'), 2)
        self.assertIn("Premier point qui continue", rendu)

    def test_une_url_devient_un_lien_et_perd_ses_chevrons(self):
        rendu = mise_en_forme("Voir <https://pauseia.fr/fr/declaration> svp")
        self.assertIn('href="https://pauseia.fr/fr/declaration"', rendu)
        self.assertNotIn("&lt;https", rendu)

    def test_une_url_avec_des_parametres_n_est_pas_coupee(self):
        # `&` échappé en `&amp;` dans le texte, pas dans la cible du lien.
        rendu = mise_en_forme("https://exemple.fr/a?x=1&y=2")
        self.assertIn('href="https://exemple.fr/a?x=1&amp;y=2"', rendu)
        self.assertNotIn("&amp;gt;", rendu)

    def test_le_gras_des_messageries_est_rendu(self):
        self.assertIn("<strong>A Call for Control</strong>",
                      mise_en_forme("intitulée *A Call for Control*, initiée"))

    def test_une_multiplication_n_est_pas_du_gras(self):
        self.assertNotIn("<strong>", mise_en_forme("3 * 4 * 5 font soixante"))

    def test_une_citation_est_distinguee(self):
        rendu = mise_en_forme("> Nous appelons les gouvernements\n"
                              "> du monde entier\n\nBien à vous")
        self.assertIn('<p class="mail-citation">Nous appelons les '
                      'gouvernements du monde entier</p>', rendu)
        self.assertIn("<p>Bien à vous</p>", rendu)

    def test_le_html_du_message_est_echappe(self):
        rendu = mise_en_forme("Bonjour <script>alert(1)</script>")
        self.assertIn("&lt;script&gt;", rendu)
        self.assertNotIn("<script>", rendu)

    def test_un_corps_vide_ne_fait_pas_tomber_la_page(self):
        self.assertEqual(str(mise_en_forme("")), "")
        self.assertEqual(str(mise_en_forme(None)), "")

    def test_les_retours_windows_sont_traites_comme_les_autres(self):
        self.assertEqual(mise_en_forme("Un\r\n\r\nDeux").count("<p>"), 2)


if __name__ == "__main__":
    unittest.main()
