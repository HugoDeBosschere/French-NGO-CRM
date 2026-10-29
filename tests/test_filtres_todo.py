"""The genre / fonction filters of /todo, and where the genre comes from.

The same letter goes to everyone of the same fonction and genre (« Madame la
députée… »), so « Relances à envoyer » and « À contacter » let you pick them
out and copy their addresses at once. The filtering itself runs in the browser;
what is pinned here is what the page hands it, and the genre field it reads.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))


class TodoFiltersTests(unittest.TestCase):
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
        for table in ("to_contact", "meeting_persons", "meeting_moderators",
                      "meetings", "person_organisations", "persons"):
            self.db.execute(f"DELETE FROM {table}")
        self.mod_id = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Camille')").lastrowid
        self.db.commit()

    def _person(self, name, gender, role, email):
        return self.db.execute(
            "INSERT INTO persons (name, contact_type, role, stance, gender, "
            "email, created_at) VALUES (?, 'Politique', ?, 'Inconnu', ?, ?, "
            "'2026-01-01')", (name, role, gender, email)).lastrowid

    def _people_attrs(self, html, css_class):
        return [json.loads(m) for m in re.findall(
            css_class + r"\"[^>]*data-people='([^']*)'", html)]

    def test_to_contact_items_carry_genre_fonction_and_address(self):
        pid = self._person("Élise Vernant", "Femme", "Député·e",
                           "elise.vernant@assemblee-nationale.fr")
        self.db.execute("INSERT INTO to_contact (person_id, created_at) "
                        "VALUES (?, '2026-01-01')", (pid,))
        self.db.commit()
        html = self.client.get("/todo").get_data(as_text=True)
        self.assertEqual(self._people_attrs(html, "todo-contact"), [[{
            "gender": "Femme", "roles": ["Député·e"],
            "email": "elise.vernant@assemblee-nationale.fr"}]])
        # Only the fonctions present are offered, plus « Sans fonction ».
        self.assertIn('<option value="Député·e">Député·e</option>', html)
        self.assertNotIn('<option value="Évêque">', html)
        self.assertIn("Copier les adresses", html)

    def test_a_relance_lists_each_of_its_people(self):
        # Genre and fonction must hold for the same person, so the browser
        # needs them person by person, not merged across the relance.
        a = self._person("Élise Vernant", "Femme", "Député·e", "e@an.fr")
        b = self._person("Marc Dorne", None, "Sénateur·ice", None)
        mid = self.db.execute(
            "INSERT INTO meetings (meeting_date, summary, created_at, done, "
            "follow_up_date) VALUES (?, 'x', '2026-01-01', 1, ?)",
            ((date.today() - timedelta(days=5)).isoformat(),
             (date.today() - timedelta(days=1)).isoformat())).lastrowid
        for pid in (a, b):
            self.db.execute("INSERT INTO meeting_persons (meeting_id, person_id) "
                            "VALUES (?, ?)", (mid, pid))
        self.db.commit()
        html = self.client.get("/todo").get_data(as_text=True)
        people = self._people_attrs(html, "todo-relance")
        self.assertEqual(len(people), 1)
        self.assertCountEqual(people[0], [
            {"gender": "Femme", "roles": ["Député·e"], "email": "e@an.fr"},
            {"gender": "", "roles": ["Sénateur·ice"], "email": ""}])

    def test_the_person_form_records_the_genre(self):
        resp = self.client.post("/people/new", data={
            "name": "Ana Pires", "contact_type": "Journaliste",
            "stance": "Inconnu", "signed_declaration": "0", "gender": "Femme",
            "added_by": str(self.mod_id), "validated_by": str(self.mod_id)})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.execute(
            "SELECT gender FROM persons WHERE name = 'Ana Pires'").fetchone()[0],
            "Femme")

    def test_an_unknown_genre_is_stored_as_not_stated(self):
        self.client.post("/people/new", data={
            "name": "Ana Pires", "contact_type": "Journaliste",
            "stance": "Inconnu", "signed_declaration": "0", "gender": "X",
            "added_by": str(self.mod_id), "validated_by": str(self.mod_id)})
        self.assertIsNone(self.db.execute(
            "SELECT gender FROM persons WHERE name = 'Ana Pires'").fetchone()[0])


class MarkAllDoneTests(unittest.TestCase):
    """« Tout marquer comme fait »: ticks what the filters left on screen."""

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
        for table in ("to_contact", "meeting_persons", "meetings", "persons"):
            self.db.execute(f"DELETE FROM {table}")
        self.people = [self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES (?, 'Politique', 'Inconnu', '2026-01-01')", (n,)).lastrowid
            for n in ("A", "B", "C")]
        for pid in self.people:
            self.db.execute("INSERT INTO to_contact (person_id, created_at) "
                            "VALUES (?, '2026-01-01')", (pid,))
        self.meeting = self.db.execute(
            "INSERT INTO meetings (meeting_date, summary, created_at, done, "
            "follow_up_date) VALUES ('2026-01-01', 'x', '2026-01-01', 1, "
            "'2026-01-02')").lastrowid
        self.db.execute("INSERT INTO meeting_persons (meeting_id, person_id) "
                        "VALUES (?, ?)", (self.meeting, self.people[0]))
        self.db.commit()

    def _done(self, pid):
        return self.db.execute("SELECT done FROM to_contact WHERE person_id = ?",
                               (pid,)).fetchone()[0]

    def test_ticks_exactly_what_is_posted(self):
        a, b, c = self.people
        self.client.post("/todo/tout-cocher", data={
            "item": [f"person:{a}", f"person:{b}", f"meeting:{self.meeting}"]})
        self.assertEqual([self._done(p) for p in self.people], [1, 1, 0])
        self.assertEqual(self.db.execute(
            "SELECT follow_up_done FROM meetings WHERE id = ?",
            (self.meeting,)).fetchone()[0], 1)

    def test_posting_twice_never_unticks(self):
        # The single checkboxes toggle; this must not, or a double click
        # would bring everything back.
        a = self.people[0]
        for _ in range(2):
            self.client.post("/todo/tout-cocher", data={"item": [f"person:{a}"]})
        self.assertEqual(self._done(a), 1)

    def test_garbage_is_ignored(self):
        resp = self.client.post("/todo/tout-cocher", data={
            "item": ["persons:1; DROP", "person:abc", "x:1"]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual([self._done(p) for p in self.people], [0, 0, 0])

    def test_only_what_passes_the_filters_is_ticked(self):
        # The browser's list is not trusted: an entry for someone else, or
        # of another genre / fonction, is posted but must stay unticked.
        a, b, c = self.people
        hugo = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Hugo')").lastrowid
        self.db.execute("UPDATE to_contact SET assigned_to = ? "
                        "WHERE person_id IN (?, ?)", (hugo, a, b))
        self.db.execute("UPDATE persons SET gender = 'Homme', role = 'Député·e'")
        self.db.execute("UPDATE persons SET gender = 'Femme' WHERE id = ?", (b,))
        self.db.commit()
        self.client.post("/todo/tout-cocher", data={
            "item": [f"person:{a}", f"person:{b}", f"person:{c}"],
            "by": "Hugo", "gender": "Homme", "role": "Député·e"})
        # b is Hugo's but a femme, c is a homme député for « quiconque ».
        self.assertEqual([self._done(p) for p in self.people], [1, 0, 0])

    def test_quiconque_means_nobody_assigned(self):
        a, b, _ = self.people
        hugo = self.db.execute(
            "INSERT INTO moderators (name) VALUES ('Hugo')").lastrowid
        self.db.execute("UPDATE to_contact SET assigned_to = ? WHERE person_id = ?",
                        (hugo, a))
        self.db.commit()
        self.client.post("/todo/tout-cocher", data={
            "item": [f"person:{a}", f"person:{b}"], "by": "__anyone__"})
        self.assertEqual([self._done(p) for p in self.people[:2]], [0, 1])

    def test_items_carry_their_key(self):
        html = self.client.get("/todo").get_data(as_text=True)
        self.assertIn(f'data-done="person:{self.people[0]}"', html)
        self.assertIn(f'data-done="meeting:{self.meeting}"', html)
        self.assertIn("Tout marquer comme fait", html)


class SingleTickTests(MarkAllDoneTests):
    """One checkbox, ticked without reloading the page (todo-check.js)."""

    JSON = {"Accept": "application/json"}

    def test_the_page_gets_json_not_a_redirect(self):
        a = self.people[0]
        resp = self.client.post(f"/todo/a-contacter/{a}/done",
                                data={"done": "1"}, headers=self.JSON)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})
        self.assertEqual(self._done(a), 1)

    def test_a_click_sent_twice_stays_ticked(self):
        # With `done` the box is set, not flipped: a second request does not
        # bring the entry back to the TODO.
        a = self.people[0]
        for _ in range(2):
            self.client.post(f"/todo/a-contacter/{a}/done", data={"done": "1"},
                             headers=self.JSON)
        self.assertEqual(self._done(a), 1)
        self.client.post(f"/todo/a-contacter/{a}/done", data={"done": "0"},
                         headers=self.JSON)
        self.assertEqual(self._done(a), 0)

    def test_relances_and_rencontres_too(self):
        for url in (f"/todo/relance/meeting/{self.meeting}/done",
                    f"/todo/rencontre/{self.meeting}/done"):
            resp = self.client.post(url, data={"done": "1"}, headers=self.JSON)
            self.assertEqual(resp.get_json(), {"ok": True})
        self.assertEqual(self.db.execute(
            "SELECT follow_up_done, done FROM meetings WHERE id = ?",
            (self.meeting,)).fetchone(), (1, 1))

    def test_without_javascript_it_still_reloads(self):
        a = self.people[0]
        resp = self.client.post(f"/todo/a-contacter/{a}/done")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._done(a), 1)

    def test_the_forms_say_which_way(self):
        html = self.client.get("/todo").get_data(as_text=True)
        self.assertIn('name="done" value="1"', html)
        self.assertIn("todo-check.js", html)


class BackfillGenreTests(unittest.TestCase):
    def setUp(self):
        import backfill_genre                        # noqa: PLC0415
        self.bg = backfill_genre
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT, "
                        "contact_type TEXT, gender TEXT, added_by INTEGER)")
        self.db.executemany(
            "INSERT INTO persons (name, contact_type, gender, added_by) "
            "VALUES (?, ?, ?, ?)", [
                ("Audrey Abadie-Amiel", "Politique", None, None),
                ("Saisie À-la-main", "Politique", None, 3),
                ("Déjà Renseignée", "Politique", "Homme", None),
                ("Homonyme Journaliste", "Journaliste", None, None),
            ])
        self.genders = {"Audrey Abadie-Amiel": "Femme",
                        "Saisie À-la-main": "Femme",
                        "Déjà Renseignée": "Femme",
                        "Homonyme Journaliste": "Homme"}

    def _gender(self, name):
        return self.db.execute("SELECT gender FROM persons WHERE name = ?",
                               (name,)).fetchone()[0]

    def test_only_script_made_politiques_without_a_genre_are_filled(self):
        self.bg.backfill(self.db, self.genders, commit=True)
        self.assertEqual(self._gender("Audrey Abadie-Amiel"), "Femme")
        self.assertIsNone(self._gender("Saisie À-la-main"))      # typed by hand
        self.assertEqual(self._gender("Déjà Renseignée"), "Homme")  # never overwritten
        self.assertIsNone(self._gender("Homonyme Journaliste"))  # other type

    def test_a_dry_run_writes_nothing(self):
        self.assertEqual(self.bg.backfill(self.db, self.genders, commit=False),
                         (1, 1))
        self.assertIsNone(self._gender("Audrey Abadie-Amiel"))


if __name__ == "__main__":
    unittest.main()
