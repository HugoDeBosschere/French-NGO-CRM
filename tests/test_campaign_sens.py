"""Le sens d'un échange citoyen, et le corps enfin conservé.

Le pipeline citoyen ne voit qu'un côté : le citoyen met PauseIA en copie cachée
de ce qu'il envoie. Il gardait l'objet et le nom de l'élu·e, pas le message —
si bien qu'un premier courriel et le même fil trois semaines plus tard, une
fois l'élu·e sorti du silence, se ressemblaient trait pour trait dans la liste.
"""
import email
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import import_campaign_mails as campagne  # noqa: E402


def un_message(objet, corps="Bonjour Madame la Députée,\n\nJe vous écris…"):
    msg = email.message.EmailMessage()
    msg["Subject"] = objet
    msg["Message-ID"] = "<%s@exemple.fr>" % abs(hash(objet))
    msg["Date"] = "Sat, 10 Oct 2026 10:00:00 +0200"
    msg.set_content(corps)
    return msg


class EtapeTests(unittest.TestCase):
    def test_un_objet_nu_est_un_premier_envoi(self):
        self.assertEqual(campagne.etape_echange("Les risques de l'IA"), "envoi")

    def test_les_prefixes_de_reponse_sont_reconnus(self):
        for objet in ("Re: x", "RE : x", "Rép : x", "Ref: x", "AW: x", "re[2]: x"):
            self.assertEqual(campagne.etape_echange(objet), "reponse", objet)

    def test_les_prefixes_de_transfert_sont_reconnus(self):
        for objet in ("TR: x", "Fwd: x", "Fw: x", "WG: x"):
            self.assertEqual(campagne.etape_echange(objet), "transfert", objet)

    def test_un_objet_absent_ne_fait_pas_tomber_le_script(self):
        self.assertEqual(campagne.etape_echange(None), "envoi")

    def test_un_mot_qui_commence_par_re_n_est_pas_une_reponse(self):
        # « Retraites », « Référendum » : sans les deux-points, rien à déduire.
        self.assertEqual(campagne.etape_echange("Retraites et IA"), "envoi")
        self.assertEqual(campagne.etape_echange("Référendum sur l'IA"), "envoi")


class DestinatairesTests(unittest.TestCase):
    """L'indice le plus sûr : à qui le citoyen a-t-il écrit ?"""

    def _msg(self, objet, to=None, cc=None):
        msg = email.message.EmailMessage()
        msg["Subject"] = objet
        if to:
            msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg.set_content("texte")
        return msg

    def test_l_elu_en_destinataire_visible_est_un_premier_envoi(self):
        # Nous sommes en copie cachée : invisible dans le message reçu.
        msg = self._msg("Re: Les risques", to="a.dupont@assemblee-nationale.fr")
        self.assertEqual(
            campagne.etape_echange("Re: Les risques", msg,
                                   {"a.dupont@assemblee-nationale.fr"},
                                   "suivi-campagne@pauseia.fr"),
            "envoi")

    def test_notre_adresse_seule_en_destinataire_est_un_retour(self):
        # Le citoyen nous écrit à nous, sans l'élu·e : il nous transmet.
        msg = self._msg("Les risques", to="suivi-campagne@pauseia.fr")
        self.assertEqual(
            campagne.etape_echange("Les risques", msg,
                                   {"a.dupont@assemblee-nationale.fr"},
                                   "suivi-campagne@pauseia.fr"),
            "reponse")

    def test_l_indice_des_destinataires_l_emporte_sur_l_objet(self):
        # « Re: » dirait « réponse » ; l'élu·e en To dit « envoi ». Un citoyen
        # qui relance son élu·e écrit bien à son élu·e.
        msg = self._msg("Re: Les risques", cc="a.dupont@assemblee-nationale.fr")
        self.assertEqual(
            campagne.etape_echange("Re: Les risques", msg,
                                   {"a.dupont@assemblee-nationale.fr"},
                                   "suivi-campagne@pauseia.fr"),
            "envoi")

    def test_sans_destinataire_exploitable_on_retombe_sur_l_objet(self):
        msg = self._msg("Re: Les risques", to="quelquun@ailleurs.fr")
        self.assertEqual(
            campagne.etape_echange("Re: Les risques", msg,
                                   {"a.dupont@assemblee-nationale.fr"},
                                   "suivi-campagne@pauseia.fr"),
            "reponse")

    def test_un_message_sans_aucun_en_tete_ne_fait_pas_tomber_le_script(self):
        msg = self._msg("Les risques")
        self.assertEqual(campagne.etape_echange("Les risques", msg, set(), None),
                         "envoi")


class EnregistrementTests(unittest.TestCase):
    def setUp(self):
        self.chemin = tempfile.mkstemp(suffix=".db")[1]
        self.db = sqlite3.connect(self.chemin)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE mails (id INTEGER PRIMARY KEY, mail_date TEXT,"
            " direction TEXT, important INTEGER, subject TEXT, summary TEXT,"
            " follow_up_date TEXT, received_by INTEGER, validated_by INTEGER,"
            " document_stored_name TEXT, document_orig_name TEXT,"
            " created_at TEXT);"
            "CREATE TABLE mail_persons (mail_id INTEGER, person_id INTEGER);")
        self.db.execute("INSERT INTO persons (id, name) VALUES (1, 'Anne Dupont')")
        campagne.ensure_state_table(self.db)
        self.db.commit()
        self.addCleanup(self.db.close)
        self.addCleanup(os.unlink, self.chemin)

    def _deposer(self, msg):
        campagne.stage_message(self.db, msg, 1, "INBOX", [(1, "Anne Dupont")],
                               dry_run=False, auto_publish=True)
        self.db.commit()
        return self.db.execute(
            "SELECT x.*, b.body FROM mails x "
            "LEFT JOIN mail_bodies b ON b.mail_id = x.id "
            "ORDER BY x.id DESC LIMIT 1").fetchone()

    def test_le_corps_est_conserve(self):
        ligne = self._deposer(un_message("Les risques de l'IA"))
        self.assertIn("Je vous écris", ligne["body"])

    def test_le_resume_garde_l_etiquette_qui_sert_a_la_colonne_origine(self):
        # /echanges reconnaît un mail citoyen à cette marque-là : si le corps
        # la remplaçait, la colonne « Origine » redeviendrait vide.
        ligne = self._deposer(un_message("Les risques de l'IA"))
        self.assertTrue(ligne["summary"].startswith("Mail d'un citoyen"))

    def test_un_premier_envoi_est_marque_envoye(self):
        self.assertEqual(self._deposer(un_message("Les risques"))["direction"],
                         "sent")

    def test_un_fil_qui_revient_est_marque_recu(self):
        self.assertEqual(self._deposer(un_message("Re: Les risques"))["direction"],
                         "received")
        self.assertEqual(self._deposer(un_message("TR: Les risques"))["direction"],
                         "received")


if __name__ == "__main__":
    unittest.main()
