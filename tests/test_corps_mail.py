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
import time
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


class EnteteTransfertTests(unittest.TestCase):
    """Un message transféré arrivait d'un seul tenant.

    Le filtre recolle les lignes repliées par le client d'envoi, ce qui est
    juste pour une phrase et faux pour un en-tête : « De : … » et « Objet : … »
    sont deux informations, pas une phrase coupée en deux.
    """

    def test_chaque_ligne_d_entete_reste_sur_sa_ligne(self):
        rendu = mise_en_forme(
            "Début du message transféré :\n"
            "De: Leo Lefebvre <leo@exemple.fr>\n"
            "Date: 8 octobre 2026\n"
            "Objet: Suite à notre entretien\n")
        self.assertEqual(rendu.count("<p>"), 4)
        self.assertIn("<p>Objet: Suite à notre entretien</p>", rendu)

    def test_le_corps_qui_suit_est_recolle_normalement(self):
        rendu = mise_en_forme(
            "Objet: Suite\n\nMonsieur le député,\nJe tenais à vous\nremercier.")
        self.assertIn("Monsieur le député, Je tenais à vous remercier.", rendu)

    def test_un_mot_suivi_de_deux_points_n_est_pas_un_entete(self):
        # « Nos propositions : » doit rester collé à ce qui suit.
        rendu = mise_en_forme("Nos propositions :\nun colloque au Parlement.")
        self.assertIn("Nos propositions : un colloque", rendu)


class HtmlEnTexteTests(unittest.TestCase):
    """Le repli quand un courriel n'a pas de partie texte.

    Il remplaçait chaque balise par une espace et ne décodait jamais les
    entités : « &nbsp; », « &lt; » et « &gt; » arrivaient en clair, et tout le
    message tenait sur une ligne.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "utils"))
        from import_campaign_mails import html_en_texte   # noqa: PLC0415
        self.convertir = html_en_texte

    def test_les_entites_sont_decodees(self):
        rendu = self.convertir("<p>Monsieur&nbsp;: &lt;a&gt; &amp; voilà</p>")
        self.assertEqual(rendu, "Monsieur : <a> & voilà")

    def test_les_blocs_deviennent_des_lignes(self):
        rendu = self.convertir("<div>De: Leo</div><div>Objet: Suite</div>")
        self.assertEqual(rendu.splitlines(), ["De: Leo", "Objet: Suite"])

    def test_un_saut_de_ligne_html_en_est_un(self):
        self.assertEqual(self.convertir("Cordialement,<br>Leo").splitlines(),
                         ["Cordialement,", "Leo"])

    def test_une_liste_devient_des_puces(self):
        rendu = self.convertir("<ul><li>Un colloque</li><li>Une tribune</li></ul>")
        self.assertIn("- Un colloque", rendu)
        self.assertIn("- Une tribune", rendu)

    def test_le_css_ne_se_retrouve_pas_dans_le_message(self):
        rendu = self.convertir("<style>.x{color:red}</style><p>Bonjour</p>")
        self.assertEqual(rendu, "Bonjour")
        self.assertNotIn("color:red", rendu)

    def test_le_javascript_non_plus(self):
        self.assertEqual(
            self.convertir("<script>alert(1)</script><p>Bonjour</p>"), "Bonjour")

    def test_la_partie_texte_reste_preferee_quand_elle_existe(self):
        import email                                      # noqa: PLC0415
        from import_campaign_mails import extract_body    # noqa: PLC0415
        msg = email.message.EmailMessage()
        msg.set_content("La version texte.")
        msg.add_alternative("<p>La version HTML.</p>", subtype="html")
        self.assertIn("La version texte.", extract_body(msg))


class LimiteDebitTests(unittest.TestCase):
    """Le compteur anti-spam des formulaires publics ne doit pas fuir.

    Le ménage portait sur les horodatages, jamais sur les clés : une adresse
    vue une fois gardait son entrée pour toujours. Un formulaire public est
    scanné en continu et gunicorn tourne des semaines sans redémarrer.
    """

    def setUp(self):
        app._submission_log.clear()
        self.client = app.app.test_client()

    def _soumettre(self, ip):
        return self.client.get("/declarer", environ_base={"REMOTE_ADDR": ip})

    def test_une_adresse_sans_envoi_recent_ne_laisse_pas_de_trace(self):
        with app.app.test_request_context(environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            app._rate_limited()
        self.assertNotIn("1.2.3.4", app._submission_log)

    def test_les_envois_recents_sont_bien_comptes(self):
        with app.app.test_request_context(environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            app._record_submission()
            self.assertFalse(app._rate_limited())
        self.assertEqual(len(app._submission_log["1.2.3.4"]), 1)

    def test_au_dela_du_cap_la_limite_s_applique(self):
        with app.app.test_request_context(environ_base={"REMOTE_ADDR": "9.9.9.9"}):
            for _ in range(app.RATE_LIMIT_MAX):
                app._record_submission()
            self.assertTrue(app._rate_limited())

    def test_les_adresses_inactives_sont_balayees(self):
        vieux = time.monotonic() - app.RATE_LIMIT_WINDOW - 1
        for i in range(app.RATE_LIMIT_MAX_IPS + 10):
            app._submission_log[f"10.0.{i // 256}.{i % 256}"] = [vieux]
        with app.app.test_request_context(environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            app._rate_limited()
        self.assertLessEqual(len(app._submission_log), 1)
