"""« Créer la fiche manquante » on a validation form.

The link used to open a blank form, and creating the fiche left the moderator
on it, far from the entry they were validating. Each missing name now has its
own link: the form comes filled with that name (and the organisation found, or
the type it implies), and saving it goes back to the validation, where the
new fiche is found by its name and ticked.
"""
import html as htmllib
import os
import re
import sqlite3
import tempfile
import unittest


class MissingFicheTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        app.init_db()
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        self.db = sqlite3.connect(str(app.DB_PATH))
        self.addCleanup(self.db.close)
        for table in ("pending_contents", "pending_meetings", "pending_persons"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.execute("DELETE FROM persons WHERE name IN "
                        "('Inès Marchal', 'Témoin')")
        self.db.execute("DELETE FROM organisations WHERE name IN "
                        "('Le Quotidien du Test', 'Radio Inconnue')")
        self.mod_id = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Camille')").lastrowid
        self.media = self.db.execute(
            "INSERT INTO organisations (name, org_type, stance, created_at) "
            "VALUES ('Le Quotidien du Test', 'Média', 'Inconnu', '2026-01-01')"
        ).lastrowid
        # The rencontre form needs at least one fiche to show anything.
        self.db.execute("INSERT INTO persons (name, contact_type, stance, "
                        "created_at) VALUES ('Témoin', 'Autre', 'Inconnu', "
                        "'2026-01-01')")
        self.db.commit()

    def _links(self, page, endpoint):
        return [htmllib.unescape(h) for h in
                re.findall(r'href="(/%s/new\?name=[^"]*)"' % endpoint, page)]

    def test_a_missing_person_comes_with_name_and_organisation(self):
        pid = self.db.execute(
            "INSERT INTO pending_contents (proposed_organisation, proposed_people, "
            "link, published_on, created_at) VALUES ('Le Quotidien du Test', "
            "'Inès Marchal', 'https://x.fr', '2026-10-01', '2026-10-01')").lastrowid
        self.db.commit()
        url = f"/moderation/contenu/{pid}/approve"
        page = self.client.get(url).get_data(as_text=True)
        [link] = self._links(page, "people")
        self.assertIn("name=In%C3%A8s+Marchal", link)
        self.assertIn(f"organisation_id={self.media}", link)

        form = self.client.get(link).get_data(as_text=True)
        self.assertIn('value="Inès Marchal"', form)
        # The média sets the type de contact.
        self.assertRegex(form, r'value="Journaliste"\s+selected')

        resp = self.client.post(link, data={
            "name": "Inès Marchal", "contact_type": "Journaliste",
            "stance": "Inconnu", "signed_declaration": "0",
            "organisation_ids": str(self.media),
            "added_by": str(self.mod_id), "validated_by": str(self.mod_id)})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(url))
        # Back on the validation, she is now found and ticked.
        page = self.client.get(url).get_data(as_text=True)
        self.assertNotIn("Sans correspondance", page.split("Personnes indiquées")[1]
                         .split("</div>")[0])

    def test_a_rencontre_offers_one_link_per_missing_name(self):
        pid = self.db.execute(
            "INSERT INTO pending_meetings (meeting_date, summary, proposed_people, "
            "created_at) VALUES ('2026-10-01', 'x', 'Inès Marchal, Paul Ricard', "
            "'2026-10-01')").lastrowid
        self.db.commit()
        page = self.client.get(f"/moderation/rencontre/{pid}/approve").get_data(
            as_text=True)
        links = self._links(page, "people")
        self.assertEqual(len(links), 2)
        self.assertIn("créer la fiche de Paul Ricard", page)

    def test_a_missing_media_gets_name_and_type(self):
        pid = self.db.execute(
            "INSERT INTO pending_persons (name, contact_type, "
            "proposed_organisation, created_at) VALUES ('Inès Marchal', "
            "'Journaliste', 'Radio Inconnue', '2026-10-01')").lastrowid
        self.db.commit()
        page = self.client.get(f"/moderation/personne/{pid}/approve").get_data(
            as_text=True)
        [link] = self._links(page, "organisations")
        self.assertIn("name=Radio+Inconnue", link)
        self.assertIn("org_type=M%C3%A9dia", link)

    def test_next_never_leaves_the_site(self):
        for bad in ("https://evil.example/", "//evil.example/", "/\\evil"):
            resp = self.client.post("/people/new?next=" + bad, data={
                "name": "Inès Marchal", "contact_type": "Autre",
                "stance": "Inconnu", "signed_declaration": "0",
                "added_by": str(self.mod_id), "validated_by": str(self.mod_id)})
            self.assertNotIn("evil", resp.headers["Location"])
            self.db.execute("DELETE FROM persons WHERE name = 'Inès Marchal'")
            self.db.commit()


if __name__ == "__main__":
    unittest.main()
