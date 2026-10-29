"""« Toujours en fonction » on an élu·e's fiche.

`in_office` existed, but only the Monday sync could set it, and only on fiches
it created: someone whose mandate ended outside the official lists, or a
fiche typed by hand, could not be marked as a former député·e. The person form
now says it, for élu·es only.
"""
import os
import sqlite3
import tempfile
import unittest


class EndOfMandateTests(unittest.TestCase):
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
        self.db.execute("DELETE FROM persons WHERE name = 'Élise Vernant'")
        self.mod_id = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Camille')").lastrowid
        self.group_id = self.db.execute(
            "INSERT INTO organisations (name, org_type, stance, created_at) "
            "VALUES ('Groupe test', 'Groupe politique', 'Inconnu', '2026-01-01')"
        ).lastrowid
        self.db.commit()

    def _post(self, url, contact_type="Politique", **extra):
        data = {"name": "Élise Vernant", "contact_type": contact_type,
                "role": "Député·e", "stance": "Inconnu",
                "signed_declaration": "0",
                "organisation_ids": str(self.group_id),
                "added_by": str(self.mod_id), "validated_by": str(self.mod_id),
                **extra}
        return self.client.post(url, data=data)

    def _row(self):
        return self.db.execute(
            "SELECT id, in_office FROM persons WHERE name = 'Élise Vernant'"
        ).fetchone()

    def test_a_new_elu_is_in_office_by_default(self):
        self.assertEqual(self._post("/people/new").status_code, 302)
        self.assertEqual(self._row()[1], 1)

    def test_marking_the_mandate_ended_is_saved_and_shown(self):
        self._post("/people/new")
        pid = self._row()[0]
        self._post(f"/people/{pid}/edit", in_office="0")
        self.assertEqual(self._row()[1], 0)
        html = self.client.get(f"/people/{pid}").get_data(as_text=True)
        self.assertIn("Non élu·e actuellement", html)
        self.assertIn("n'est plus en fonction", html)
        self.assertIn("Toujours en fonction", html)
        self.assertIn("Non, mandat terminé", html)
        # And it can be undone.
        self._post(f"/people/{pid}/edit", in_office="1")
        self.assertEqual(self._row()[1], 1)

    def test_only_for_elus(self):
        # A journaliste is never badged « Non élu·e actuellement ».
        self._post("/people/new", contact_type="Journaliste", in_office="0")
        self.assertEqual(self._row()[1], 1)

    def test_the_form_offers_it(self):
        html = self.client.get("/people/new").get_data(as_text=True)
        self.assertIn("Toujours en fonction", html)
        self.assertIn("Non, mandat terminé", html)


if __name__ == "__main__":
    unittest.main()
