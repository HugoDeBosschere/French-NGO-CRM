"""« Autres adresses » on the person form.

`person_emails` could hold several addresses per person, and the import already
used them, but only the import or « À rattacher » could add one: the form had a
single Courriel field, so a known second address could not be typed in, nor a
wrong one removed.
"""
import os
import sqlite3
import tempfile
import unittest


class OtherEmailsTests(unittest.TestCase):
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
        self.db.execute("DELETE FROM persons WHERE name IN "
                        "('Inès Garnier', 'Paul Ravel')")
        self.db.execute("DELETE FROM person_emails WHERE email LIKE '%@exemple.fr'")
        self.db.execute("DELETE FROM civicrm_pending WHERE email LIKE '%@exemple.fr'")
        self.mod_id = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Camille')").lastrowid
        self.db.commit()

    def _post(self, url, name="Inès Garnier", **extra):
        data = {"name": name, "contact_type": "Journaliste",
                "stance": "Inconnu", "signed_declaration": "0",
                "email": "ines@exemple.fr",
                "added_by": str(self.mod_id), "validated_by": str(self.mod_id),
                **extra}
        return self.client.post(url, data=data)

    def _pid(self, name="Inès Garnier"):
        return self.db.execute(
            "SELECT id FROM persons WHERE name = ?", (name,)).fetchone()[0]

    def _extras(self, pid):
        return sorted(r[0] for r in self.db.execute(
            "SELECT email FROM person_emails WHERE person_id = ?", (pid,)))

    def test_added_shown_and_removed(self):
        self._post("/people/new",
                   other_emails="Cabinet@exemple.fr\nperso@exemple.fr, ines@exemple.fr")
        pid = self._pid()
        # Lower-cased, and the main address is not duplicated as an extra.
        self.assertEqual(self._extras(pid),
                         ["cabinet@exemple.fr", "perso@exemple.fr"])
        page = self.client.get(f"/people/{pid}").get_data(as_text=True)
        self.assertIn("perso@exemple.fr", page)
        form = self.client.get(f"/people/{pid}/edit").get_data(as_text=True)
        self.assertIn("cabinet@exemple.fr\nperso@exemple.fr", form)

        self._post(f"/people/{pid}/edit", other_emails="perso@exemple.fr")
        self.assertEqual(self._extras(pid), ["perso@exemple.fr"])

    def test_address_of_someone_else_is_refused(self):
        self._post("/people/new", name="Paul Ravel", email="paul@exemple.fr")
        resp = self._post("/people/new", other_emails="paul@exemple.fr")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("déjà celle de Paul Ravel", resp.get_data(as_text=True))

    def test_invalid_address_is_refused(self):
        resp = self._post("/people/new", other_emails="pas-une-adresse")
        self.assertEqual(resp.status_code, 400)

    def test_queued_address_is_resolved(self):
        self.db.execute(
            "INSERT INTO civicrm_pending (email, first_seen, last_seen) "
            "VALUES ('perso@exemple.fr', '2026-01-01', '2026-01-01')")
        self.db.commit()
        self._post("/people/new", other_emails="perso@exemple.fr")
        row = self.db.execute("SELECT status, person_id FROM civicrm_pending "
                              "WHERE email = 'perso@exemple.fr'").fetchone()
        self.assertEqual(row, ("resolved", self._pid()))

    def test_form_without_the_field_keeps_them(self):
        self._post("/people/new", other_emails="perso@exemple.fr")
        pid = self._pid()
        self._post(f"/people/{pid}/edit")          # no other_emails posted
        self.assertEqual(self._extras(pid), ["perso@exemple.fr"])


if __name__ == "__main__":
    unittest.main()
