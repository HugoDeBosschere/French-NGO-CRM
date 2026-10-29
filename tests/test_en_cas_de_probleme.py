"""« En cas de problème » : who could take a participant's place.

Under the sign-ups of each upcoming rencontre in /todo, and in the rencontre
form, a second list names the utilisateurices standing by. They do not count
towards the minimum, and nobody is both going and standing by: the participant
wins.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta


class BackupTests(unittest.TestCase):
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
        for table in ("meeting_backups", "meeting_persons",
                      "meeting_moderators", "meetings"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.execute("DELETE FROM moderators WHERE name IN ('Camille', 'Sacha')")
        self.camille = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Camille')").lastrowid
        self.sacha = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Sacha')").lastrowid
        self.mid = self.db.execute(
            "INSERT INTO meetings (meeting_date, summary, created_at, done) "
            "VALUES (?, 'x', '2026-01-01', 0)",
            ((date.today() + timedelta(days=5)).isoformat(),)).lastrowid
        # The rencontre form needs at least one fiche to show anything.
        if not self.db.execute("SELECT 1 FROM persons LIMIT 1").fetchone():
            self.db.execute("INSERT INTO persons (name, contact_type, stance, "
                            "created_at) VALUES ('Témoin', 'Autre', 'Inconnu', "
                            "'2026-01-01')")
        self.db.commit()

    def _backups(self):
        return {r[0] for r in self.db.execute(
            "SELECT moderator_id FROM meeting_backups WHERE meeting_id = ?",
            (self.mid,))}

    def _sign_up(self, participants, backups):
        return self.client.post(
            f"/todo/rencontre/{self.mid}/inscriptions",
            data={"participant_ids": [str(i) for i in participants],
                  "backup_ids": [str(i) for i in backups]},
            headers={"X-Requested-With": "XMLHttpRequest"})

    def test_todo_saves_and_shows_backups(self):
        self.assertEqual(self._sign_up([self.camille], [self.sacha]).status_code, 204)
        self.assertEqual(self._backups(), {self.sacha})
        page = self.client.get("/todo").get_data(as_text=True)
        block = page.split("Rencontres à venir", 1)[1]
        self.assertIn("En cas de problème", block)
        self.assertRegex(block, r'name="backup_ids" value="%d"\s+checked' % self.sacha)
        # A backup is not a participant: still one inscrit·e.
        self.assertIn('<span data-count>1</span>', block)

    def test_participant_is_never_also_a_backup(self):
        self._sign_up([self.camille], [self.camille, self.sacha])
        self.assertEqual(self._backups(), {self.sacha})

    def test_rencontre_form_and_page(self):
        self._sign_up([self.camille], [self.sacha])
        form = self.client.get(f"/meetings/{self.mid}/edit").get_data(as_text=True)
        self.assertRegex(form, r'name="backup_ids" value="%d"\s+checked' % self.sacha)
        page = self.client.get(f"/meetings/{self.mid}").get_data(as_text=True)
        self.assertIn("En cas de problème", page)
        self.assertIn("Sacha", page.split("En cas de problème", 1)[1])

    def test_no_backup_no_line_on_the_page(self):
        page = self.client.get(f"/meetings/{self.mid}").get_data(as_text=True)
        self.assertNotIn("En cas de problème", page)


if __name__ == "__main__":
    unittest.main()
