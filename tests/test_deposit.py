"""Le dépôt manuel d'un courriel (/echanges/deposer).

La capture automatique ne retient que ce que l'association a une raison de
suivre, ce qui laisse dehors un cas courant : le journaliste qui écrit depuis
son adresse personnelle. Le dépôt est la réponse, et il change la nature du
consentement — on ne capte plus, on reçoit ce qu'on nous confie. D'où deux
choses à vérifier ici : que le fichier déposé passe par le MÊME classifieur que
l'import, et que le périmètre restrictif ne s'y applique pas.
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def eml(frm, to, subject="Votre article", message_id="<a@pauseia.fr>", body="Bonjour,"):
    return (f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n"
            f"Message-ID: {message_id}\r\nDate: Thu, 24 Sep 2026 10:00:00 +0200"
            f"\r\n\r\n{body}\r\n").encode()


class DepositTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.app = app
        app.init_db()
        self.db = sqlite3.connect(str(app.DB_PATH))
        self.addCleanup(self.db.close)
        for table in ("mail_members", "mail_persons", "mail_bodies", "mails",
                      "members", "persons", "civicrm_pending", "imported_mails",
                      # `person_emails` manquait : les adresses apprises par
                      # un test fuyaient dans le suivant.
                      "person_emails"):
            try:
                self.db.execute(f"DELETE FROM {table}")
            except sqlite3.Error:
                pass
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES ('Morgane Tual', 'Journaliste', 'Inconnue', "
            "'morgane.tual@lemonde.fr', ?)", (NOW,))
        self.db.execute(
            "INSERT INTO members (email, name, created_at) "
            "VALUES ('maxime@pauseia.fr', 'Maxime Fournes', ?)", (NOW,))
        self.db.commit()
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def _post(self, files, **champs):
        data = {"courriels": [(io.BytesIO(raw), name) for name, raw in files]}
        data.update(champs)
        return self.client.post("/echanges/deposer", data=data,
                                content_type="multipart/form-data",
                                follow_redirects=True)

    def _mails(self):
        return self.db.execute("SELECT subject FROM mails").fetchall()

    def _alias(self):
        return [r[0] for r in self.db.execute("SELECT email FROM person_emails")]

    def test_rattacher_apprend_l_adresse_pour_la_suite_par_defaut(self):
        # Le comportement par défaut, et le plus courant : on dit qui c'est,
        # et ses courriels suivants entrent tout seuls.
        self._post([("a.eml", eml("Morgane Tual <mtual@gmail.com>",
                                  "maxime@pauseia.fr"))],
                   person_nom="Morgane Tual", apprendre="1")
        self.assertIn("mtual@gmail.com", self._alias())

    def test_sans_la_case_le_courriel_entre_mais_la_suite_non(self):
        # La conséquence était silencieuse : renseigner un nom apprenait
        # l'adresse sans le dire. Elle se décide maintenant, et se refuse.
        self._post([("a.eml", eml("Morgane Tual <perso@free.fr>",
                                  "maxime@pauseia.fr"))],
                   person_nom="Morgane Tual")
        self.assertEqual(self._alias(), [])
        self.assertEqual(len(self._mails()), 1)   # le dépôt a bien eu lieu

    def test_la_case_est_cochee_par_defaut_dans_le_formulaire(self):
        html = self.client.get("/echanges/deposer").get_data(as_text=True)
        self.assertIn('name="apprendre"', html)
        self.assertIn("les prochains échanges", html)

    def test_the_page_opens(self):
        self.assertEqual(self.client.get("/echanges/deposer").status_code, 200)

    def test_a_known_exchange_is_recorded(self):
        html = self._post([("entretien.eml", eml(
            "maxime@pauseia.fr", "morgane.tual@lemonde.fr"))]).get_data(as_text=True)
        self.assertIn("courriel(s) enregistré(s)", html)
        self.assertEqual(len(self._mails()), 1)

    def test_the_same_file_twice_is_not_recorded_twice(self):
        payload = [("entretien.eml", eml("maxime@pauseia.fr",
                                         "morgane.tual@lemonde.fr"))]
        self._post(payload)
        html = self._post(payload).get_data(as_text=True)
        self.assertIn("déjà connu", html)
        self.assertEqual(len(self._mails()), 1)

    def test_a_freemail_journalist_is_queued_although_the_import_would_refuse(self):
        # Le cœur de la fonctionnalité : la capture automatique écarte ce gmail
        # car rien ne l'y distingue d'un contact privé ; un dépôt volontaire,
        # lui, vaut consentement.
        html = self._post([("pige.eml", eml(
            "maxime@pauseia.fr", "journaliste.pige@gmail.com",
            message_id="<b@pauseia.fr>"))]).get_data(as_text=True)
        self.assertIn("à rattacher", html)
        queued = {r[0] for r in self.db.execute(
            "SELECT email FROM civicrm_pending")}
        self.assertIn("journaliste.pige@gmail.com", queued)

    def test_a_mail_without_any_member_is_not_recorded(self):
        html = self._post([("externe.eml", eml(
            "a@ailleurs.fr", "b@ailleurs.fr",
            message_id="<c@x.fr>"))]).get_data(as_text=True)
        self.assertIn("sans membre PauseIA identifiable", html)
        self.assertEqual(self._mails(), [])

    def test_a_wrong_file_type_is_refused_by_name(self):
        html = self._post([("photo.png", b"\x89PNG\r\n")]).get_data(as_text=True)
        self.assertIn("refusé", html)

    def test_an_unreadable_file_does_not_take_the_others_down(self):
        html = self._post([
            ("cassé.eml", b"\x00\x01\x02 pas un courriel"),
            ("bon.eml", eml("maxime@pauseia.fr", "morgane.tual@lemonde.fr",
                            message_id="<d@pauseia.fr>")),
        ]).get_data(as_text=True)
        self.assertIn("enregistré", html)
        self.assertEqual(len(self._mails()), 1)

    def test_nothing_sent_says_so(self):
        html = self.client.post("/echanges/deposer", data={},
                                content_type="multipart/form-data",
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Aucun fichier reçu", html)


if __name__ == "__main__":
    unittest.main()


class HeaderDecodingTests(unittest.TestCase):
    """Les objets de courriel, dans les formes qui arrivent vraiment.

    Découvert en déposant un .eml à la main : l'objet « Entretien sur la
    sécurité » se retrouvait en base sous la forme « s??curit?? ».
    L'expéditeur avait mis de l'UTF-8 brut dans l'en-tête, contraire au RFC mais
    courant ; `email` le rend alors sous forme d'objet Header en
    « unknown-8bit », et le convertir en texte remplace chaque octet par « ? ».
    Le défaut touchait aussi l'import automatique, pas seulement le dépôt : il
    est dans le décodeur partagé.
    """

    def _subject(self, raw):
        import email as email_mod                    # noqa: PLC0415
        from import_campaign_mails import decoded    # noqa: PLC0415
        return decoded(email_mod.message_from_bytes(raw).get("Subject"))

    def test_raw_utf8_in_the_header_survives(self):
        self.assertEqual(
            self._subject("Subject: Sécurité de l'IA\r\n\r\nx\r\n".encode("utf-8")),
            "Sécurité de l'IA")

    def test_the_normal_mime_encoding_still_works(self):
        self.assertEqual(
            self._subject(b"Subject: =?UTF-8?Q?S=C3=A9curit=C3=A9?=\r\n\r\nx\r\n"),
            "Sécurité")

    def test_base64_mime_too(self):
        self.assertEqual(
            self._subject(b"Subject: =?UTF-8?B?U8OpY3VyaXTDqQ==?=\r\n\r\nx\r\n"),
            "Sécurité")

    def test_an_old_windows_encoding_falls_back(self):
        self.assertEqual(
            self._subject("Subject: Sécurité\r\n\r\nx\r\n".encode("latin-1")),
            "Sécurité")

    def test_a_header_mixing_encoded_and_plain_parts(self):
        self.assertEqual(
            self._subject(b"Subject: Re: =?UTF-8?Q?S=C3=A9curit=C3=A9?= de l'IA"
                          b"\r\n\r\nx\r\n"),
            "Re: Sécurité de l'IA")

    def test_an_empty_header_is_an_empty_string(self):
        from import_campaign_mails import decoded    # noqa: PLC0415
        self.assertEqual(decoded(None), "")
