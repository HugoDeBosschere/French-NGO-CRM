"""A rencontre's place is optional, and /todo is what makes that safe.

The form used to refuse a présentiel rencontre without a place, which only
forced people to type « à confirmer » — a non-empty field that silenced any
reminder. Now the field may stay empty, and /todo lists the rencontres coming
within PLACE_CONFIRM_DAYS that still have neither a place nor a visio link.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

HEADING = "Confirmer le lien de la visio ou le lieu de la rencontre"


class PlaceToConfirmTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.app = app
        app.init_db()
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        self.db = sqlite3.connect(str(app.DB_PATH))
        self.addCleanup(self.db.close)
        for table in ("meeting_persons", "meeting_moderators", "meetings"):
            self.db.execute(f"DELETE FROM {table}")
        self.mod_id = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Camille')").lastrowid
        self.person_id = self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Élise Vernant', 'Politique', 'Inconnue', '2026-01-01')"
        ).lastrowid
        self.db.commit()

    def _meeting(self, days_ahead, place="", fmt="presentiel", done=0):
        mid = self.db.execute(
            "INSERT INTO meetings (meeting_date, summary, created_at, done, "
            "meeting_format, meeting_place) VALUES (?, 'x', '2026-01-01', ?, ?, ?)",
            ((date.today() + timedelta(days=days_ahead)).isoformat(), done, fmt,
             place)).lastrowid
        self.db.execute("INSERT INTO meeting_persons (meeting_id, person_id) "
                        "VALUES (?, ?)", (mid, self.person_id))
        self.db.commit()
        return mid

    def _sign_up(self, mid):
        self.db.execute("INSERT INTO meeting_moderators (meeting_id, moderator_id) "
                        "VALUES (?, ?)", (mid, self.mod_id))
        self.db.commit()

    def _listed(self):
        html = self.client.get("/todo").get_data(as_text=True)
        block = html.split(HEADING, 1)[1].split("<h2", 1)[0]
        return block

    def test_presentiel_without_place_is_accepted(self):
        resp = self.client.post("/meetings/new", data={
            "person_ids": str(self.person_id),
            "participant_ids": str(self.mod_id),
            "recorded_by": str(self.mod_id),
            "validated_by": str(self.mod_id),
            "meeting_date": (date.today() + timedelta(days=20)).strftime("%d/%m/%Y"),
            "meeting_format": "presentiel",
            "meeting_place": "",
            "summary": "Premier contact",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0], 1)

    def test_close_meeting_without_place_is_listed(self):
        mid = self._meeting(10)
        self.assertIn(f"/meetings/{mid}/edit", self._listed())

    def test_visio_without_link_is_listed_too(self):
        mid = self._meeting(3, fmt="visio")
        self.assertIn(f"/meetings/{mid}/edit", self._listed())

    def test_blank_place_counts_as_missing(self):
        mid = self._meeting(2, place="   ")
        self.assertIn(f"/meetings/{mid}/edit", self._listed())

    def test_not_listed_when_far_placed_done_or_past(self):
        far = self._meeting(11)
        placed = self._meeting(5, place="Assemblée nationale")
        done = self._meeting(5, done=1)
        past = self._meeting(-1)
        block = self._listed()
        for mid in (far, placed, done, past):
            self.assertNotIn(f"/meetings/{mid}/edit", block)

    def test_who_goes_is_shown(self):
        # Whoever is signed up is the one who has to chase the place or link.
        self._sign_up(self._meeting(4))
        self.assertIn("<strong>Camille</strong>", self._listed())

    def test_nobody_signed_up_is_flagged(self):
        self._meeting(4)
        self.assertIn("personne d'inscrit·e", self._listed())


if __name__ == "__main__":
    unittest.main()
