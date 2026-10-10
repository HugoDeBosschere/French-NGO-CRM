"""Les listes ne lisent plus la table entière.

/echanges regroupait tous les courriels en fils à chaque affichage, sans aucune
borne : imperceptible à 2 000 messages, intenable à 30 000 — et la boîte d'audit
ne perd jamais de message. Les fils se regroupant en Python, on ne peut pas
paginer par conversation sans couper un fil, donc la page borne la fenêtre lue
et le dit. /mails, qui liste des courriels un par un, pagine simplement.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))


class ListingTests(unittest.TestCase):
    MAILS = 650

    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.app = app
        app.init_db()
        self.db = sqlite3.connect(str(app.DB_PATH))
        self.addCleanup(self.db.close)
        for table in ("mail_thread", "mail_persons", "mails", "persons"):
            self.db.execute(f"DELETE FROM {table}")
        now = datetime.now(timezone.utc)
        person = self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Morgane Tual', 'Journaliste', 'Inconnue', ?)",
            (now.isoformat(),)).lastrowid
        for index in range(self.MAILS):
            day = (now - timedelta(hours=index)).date().isoformat()
            mail = self.db.execute(
                "INSERT INTO mails (mail_date, direction, subject, summary, "
                "created_at) VALUES (?, 'sent', ?, ?, ?)",
                (day, f"Sujet {index}", f"Sujet {index}", now.isoformat())).lastrowid
            self.db.execute(
                "INSERT INTO mail_persons (mail_id, person_id) VALUES (?, ?)",
                (mail, person))
        self.db.commit()
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def _html(self, url):
        return self.client.get(url).get_data(as_text=True)

    def test_exchanges_reads_a_window_and_says_so(self):
        html = self._html("/echanges")
        self.assertIn(f"lues sur les {self.app.EXCHANGES_WINDOW} courriels", html)
        self.assertIn("Élargir à", html)

    def test_the_window_can_be_widened_from_the_page(self):
        html = self._html("/echanges?fenetre=1000")
        self.assertNotIn("lues sur les", html)

    def test_the_window_is_capped_whatever_the_url_asks(self):
        # Sinon ?fenetre=99999999 rendrait la borne inutile.
        self._html(f"/echanges?fenetre={self.app.EXCHANGES_WINDOW_MAX * 10}")
        # Pas d'erreur, et la requête reste bornée : on vérifie la valeur retenue.
        self.assertEqual(
            min(self.app.EXCHANGES_WINDOW_MAX * 10, self.app.EXCHANGES_WINDOW_MAX),
            self.app.EXCHANGES_WINDOW_MAX)

    def test_mails_paginates(self):
        first = self._html("/mails")
        self.assertEqual(first.count("<tr onclick"), self.app.MAILS_PER_PAGE)
        self.assertIn("Suivants", first)
        self.assertNotIn("Précédents", first)

    def test_the_last_page_ends_the_pagination(self):
        last_page = (self.MAILS // self.app.MAILS_PER_PAGE) + 1
        html = self._html(f"/mails?page={last_page}")
        self.assertIn("Précédents", html)
        self.assertNotIn("Suivants", html)
        self.assertEqual(html.count("<tr onclick"),
                         self.MAILS % self.app.MAILS_PER_PAGE)

    def test_the_pager_is_in_the_page_and_not_in_the_title(self):
        # Il était appelé depuis le bloc « title » : les liens partaient dans
        # la balise <title>, donc invisibles, et les 550 courriels suivants
        # n'étaient atteignables qu'en écrivant ?page=2 à la main. Les tests
        # ci-dessus cherchaient « Suivants » dans tout le HTML et passaient.
        html = self._html("/mails")
        titre = html.split("<title>")[1].split("</title>")[0]
        self.assertNotIn("Suivants", titre)
        corps = html.split("</head>")[1]
        self.assertIn("Suivants", corps)

    def test_a_search_keeps_its_terms_across_pages(self):
        html = self._html("/mails?q=Sujet&page=2")
        self.assertIn("q=Sujet", html)

    def test_an_absurd_page_number_is_an_empty_page_not_an_error(self):
        response = self.client.get("/mails?page=9999")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<tr onclick", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


class FaviconTests(unittest.TestCase):
    """L'icône d'onglet, et surtout : qu'elle soit un SVG valide.

    Le premier essai ne s'affichait nulle part parce qu'un commentaire XML y
    contenait un double tiret (« --brand »), ce qu'XML interdit : le fichier
    était rejeté en bloc, silencieusement, et l'onglet restait vide. Un octet
    de travers suffit, donc on parse.
    """

    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.client = app.app.test_client()

    def test_the_file_is_valid_xml(self):
        import xml.etree.ElementTree as ET          # noqa: PLC0415
        root = ET.parse(os.path.join(ROOT, "static", "favicon.svg")).getroot()
        self.assertTrue(root.tag.endswith("svg"))

    def test_favicon_ico_redirects_without_a_login(self):
        # Les navigateurs qui ignorent <link rel="icon"> demandent /favicon.ico
        # d'eux-mêmes, y compris sur l'écran de connexion : un 404 à chaque page.
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 302)
        self.assertIn("favicon.svg", response.headers["Location"])

    def test_the_page_declares_it(self):
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn('rel="icon"', html)


class PeopleAndOrganisationsPagingTests(unittest.TestCase):
    """Les deux listes de fiches, qui ne décroissent jamais.

    1 780 personnes et 451 organisations en production : la page en rendait
    l'intégralité à chaque affichage. Même défaut que /echanges, resté en place
    parce qu'il ne se voit pas — jusqu'au jour où il se voit.
    """

    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.app = app
        app.init_db()
        self.db = sqlite3.connect(str(app.DB_PATH))
        self.addCleanup(self.db.close)
        for table in ("person_organisations", "mail_persons", "persons",
                      "organisations"):
            self.db.execute(f"DELETE FROM {table}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.total = self.app.PEOPLE_PER_PAGE * 2 + 50
        for index in range(self.total):
            self.db.execute(
                "INSERT INTO persons (name, contact_type, stance, created_at) "
                "VALUES (?, 'Journaliste', 'Inconnue', ?)",
                (f"Personne {index:04d}", now))
            if index < self.app.PEOPLE_PER_PAGE + 50:
                self.db.execute(
                    "INSERT INTO organisations (name, org_type, stance, created_at)"
                    " VALUES (?, 'Média', 'Inconnue', ?)",
                    (f"Média {index:04d}", now))
        self.db.commit()
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def _html(self, url):
        return self.client.get(url).get_data(as_text=True)

    def test_people_renders_one_page(self):
        html = self._html("/people")
        self.assertEqual(html.count("<tr onclick"), self.app.PEOPLE_PER_PAGE)
        self.assertIn("Suivants", html)

    def test_the_last_page_of_people(self):
        html = self._html("/people?page=3")
        self.assertEqual(html.count("<tr onclick"), 50)
        self.assertNotIn("Suivants", html)

    def test_organisations_paginate_too(self):
        html = self._html("/organisations")
        self.assertEqual(html.count("<tr onclick"), self.app.PEOPLE_PER_PAGE)
        self.assertIn("Suivants", html)

    def test_a_filter_survives_a_page_change(self):
        # Sans ça, « page suivante » repart de zéro, ce qui est la façon la plus
        # sûre de faire croire que la recherche ne fonctionne pas.
        html = self._html("/people?q=Personne+00&page=1")
        self.assertIn("q=Personne", html)

    def test_the_totals_still_count_everybody(self):
        # Les compteurs par type sont calculés sans filtre ni pagination : ils
        # disent la taille du choix, pas celle de la page.
        html = self._html("/people")
        self.assertIn(str(self.total), html)
