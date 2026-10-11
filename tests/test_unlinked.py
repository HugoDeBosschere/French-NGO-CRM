"""Cover the queue once it became an interface: /echanges/a-rattacher.

`civicrm_pending` has always existed, but only a script could read it — so the
one place holding the answer to "why isn't my exchange here?" was a command
line. These tests pin what the page promises: the waiting addresses are shown,
saying who someone is records an alias (never overwrites their main address),
and a human's "ignore" survives the nightly scripts.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


class UnlinkedPageTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                   # noqa: PLC0415
        self.app = app
        app.init_db()
        # Whichever test module imported `app` first decided the path; work on
        # that one, and start from a clean slate rather than assuming an
        # untouched file.
        path = str(app.DB_PATH)
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        self.db = sqlite3.connect(path)
        self.addCleanup(self.db.close)
        for table in ("person_emails", "civicrm_pending", "mail_members",
                      "mail_persons", "mails", "members", "persons"):
            self.db.execute(f"DELETE FROM {table}")
        # AUTOINCREMENT does not reset when rows are deleted, so never assume 1.
        self.person_id = self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES ('Marie Rondel', 'Journaliste', 'Inconnue', "
            "'marie.rondel@tokamak.fr', ?)", (NOW,)).lastrowid
        self.db.execute(
            "INSERT INTO civicrm_pending (email, display, first_seen, last_seen, "
            "seen_count, status) VALUES ('m.rondel@tokamak.fr', 'm.rondel', ?, ?, "
            "80, 'absent')", (NOW, NOW))
        self.db.commit()

    def _pending(self, address="m.rondel@tokamak.fr"):
        return self.db.execute(
            "SELECT status, person_id FROM civicrm_pending WHERE email = ?",
            (address,)).fetchone()

    def test_the_waiting_addresses_are_listed_with_their_weight(self):
        html = self.client.get("/echanges/a-rattacher").get_data(as_text=True)
        self.assertIn("m.rondel@tokamak.fr", html)
        self.assertIn("80", html)                       # 80 courriels concernés
        self.assertIn("Inconnue de CiviCRM", html)

    def test_linking_records_an_alias_and_leaves_the_main_address_alone(self):
        self.client.post("/echanges/a-rattacher/lier",
                         data={"email": "m.rondel@tokamak.fr", "person_id": str(self.person_id)})
        self.assertEqual(self._pending(), ("resolved", self.person_id))
        self.assertEqual(
            self.db.execute("SELECT email, person_id, source FROM person_emails")
            .fetchone(),
            ("m.rondel@tokamak.fr", self.person_id, "interface"))
        # Someone writes from several addresses; the one seen here is not
        # necessarily the one to write back to.
        self.assertEqual(
            self.db.execute("SELECT email FROM persons WHERE id = ?",
                            (self.person_id,)).fetchone()[0],
            "marie.rondel@tokamak.fr")

    def test_a_linked_address_leaves_the_queue(self):
        self.client.post("/echanges/a-rattacher/lier",
                         data={"email": "m.rondel@tokamak.fr", "person_id": str(self.person_id)})
        html = self.client.get("/echanges/a-rattacher").get_data(as_text=True)
        # NB: the address still shows in the confirmation flash — what must be
        # gone is the row, so assert on the list being empty.
        self.assertIn("Aucune adresse en attente", html)
        self.assertNotIn("Rattacher</button>", html)

    def test_linking_to_nobody_is_refused_rather_than_guessed(self):
        self.client.post("/echanges/a-rattacher/lier",
                         data={"email": "m.rondel@tokamak.fr", "person_id": ""})
        self.assertEqual(self._pending()[0], "absent")

    def test_an_unknown_person_is_a_404_not_a_dangling_alias(self):
        response = self.client.post(
            "/echanges/a-rattacher/lier",
            data={"email": "m.rondel@tokamak.fr", "person_id": "999"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM person_emails").fetchone()[0], 0)

    def test_ignoring_moves_it_out_of_the_way_and_is_reversible(self):
        self.client.post("/echanges/a-rattacher/ignorer",
                         data={"email": "m.rondel@tokamak.fr"})
        self.assertEqual(self._pending()[0], "ignored")
        html = self.client.get("/echanges/a-rattacher/ignorees").get_data(as_text=True)
        self.assertIn("m.rondel@tokamak.fr", html)
        self.client.post("/echanges/a-rattacher/ignorer",
                         data={"email": "m.rondel@tokamak.fr", "undo": "1"})
        self.assertEqual(self._pending()[0], "pending")

    def test_a_human_ignore_survives_the_nightly_scripts(self):
        # The whole point of a fourth status: --retry-absent revives 'absent',
        # and the import keeps counting sightings, but neither may undo a
        # decision someone took in the interface.
        import civicrm_lookup as cl                    # noqa: PLC0415
        self.client.post("/echanges/a-rattacher/ignorer",
                         data={"email": "m.rondel@tokamak.fr"})
        cl.cmd_retry_absent(self.db, type("A", (), {"commit": True})())
        cl.enqueue(self.db, "m.rondel@tokamak.fr", "m.rondel", NOW)
        self.db.commit()
        self.assertEqual(self._pending()[0], "ignored")
        self.assertEqual(self.db.execute(
            "SELECT seen_count FROM civicrm_pending WHERE email = ?",
            ("m.rondel@tokamak.fr",)).fetchone()[0], 81)

    def test_the_queue_is_counted_in_the_tab(self):
        html = self.client.get("/echanges").get_data(as_text=True)
        self.assertIn("À rattacher", html)
        self.assertIn('<span class="badge">1</span>', html)


if __name__ == "__main__":
    unittest.main()


class PruneScopeTests(unittest.TestCase):
    """--prune retire ce que le nouveau périmètre n'admet plus.

    Les adresses mises en file avant ce resserrement sont, pour une bonne part,
    des données personnelles de tiers : la correspondance privée des membres
    passe par la même boîte d'audit. Elles n'ont rien à faire là, et il ne
    suffit pas de cesser d'en ajouter.
    """

    def setUp(self):
        import civicrm_lookup as cl                  # noqa: PLC0415
        import maildomains as md                     # noqa: PLC0415
        self.cl, self.md = cl, md
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT, email TEXT);")
        cl.ensure_civicrm_tables(self.db)
        self.db.execute("INSERT INTO persons (name, email) "
                        "VALUES ('Tristan Vey', 'tvey@lefigaro.fr')")
        for email_, status in (("xnouveau@lefigaro.fr", "pending"),
                               ("f.trichet@mairie-nantes.fr", "absent"),
                               ("dr.durand@orange.fr", "pending"),
                               ("contact@plombier-92.fr", "absent"),
                               ("redaction@lefigaro.fr", "pending"),
                               ("cbouchouchi@nouvelobs.com", "resolved")):
            self.db.execute(
                "INSERT INTO civicrm_pending (email, first_seen, last_seen, "
                "status) VALUES (?, ?, ?, ?)", (email_, NOW, NOW, status))
        self.db.commit()
        self.addCleanup(md.refresh_seed_only)
        self.addCleanup(self.db.close)

    def _left(self):
        return {r[0] for r in self.db.execute("SELECT email FROM civicrm_pending")}

    def test_prune_keeps_only_what_is_in_scope(self):
        self.cl.cmd_prune(self.db, type("A", (), {"commit": True})())
        self.assertEqual(self._left(), {
            "xnouveau@lefigaro.fr",          # domaine porté par une fiche
            "f.trichet@mairie-nantes.fr",    # institution publique
            "cbouchouchi@nouvelobs.com",     # déjà résolue : on n'y touche pas
        })

    def test_a_dry_run_removes_nothing(self):
        self.cl.cmd_prune(self.db, type("A", (), {"commit": False})())
        self.assertEqual(len(self._left()), 6)


class AideEtTiretsTests(unittest.TestCase):
    """Ce que la page explique, et comment elle l'écrit.

    « À rattacher » posait une question sans la poser et proposait trois
    boutons sans dire ce qu'ils font. Et les tirets longs sont proscrits dans
    le texte visible.
    """

    def setUp(self):
        os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
        os.environ["APP_PASSWORD"] = "x"
        import app                                    # noqa: PLC0415
        app.init_db()
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def _html(self):
        return self.client.get("/echanges/a-rattacher").get_data(as_text=True)

    def test_la_question_posee_est_ecrite(self):
        self.assertIn("À qui appartient cette adresse", self._html())

    def test_les_trois_actions_sont_expliquees(self):
        html = self._html()
        for mot in ("Rattacher", "Pas une personne à suivre",
                    "Bloquer cette adresse"):
            self.assertIn(mot, html)
        self.assertIn("Trois réponses possibles", html)

    def test_la_difference_entre_les_deux_refus_est_dite(self):
        # « Ignorer » et « Ne jamais enregistrer » ne disaient pas en quoi
        # elles diffèrent, et le texte que j'avais écrit était faux : les deux
        # retirent l'adresse de la liste. La vraie différence porte sur ce qui
        # arrive ENSUITE aux courriels.
        html = self._html()
        self.assertIn("ses courriels entreront normalement", html)
        self.assertIn("jamais", html)

    def test_aucun_tiret_long_dans_le_texte_visible(self):
        self.assertNotIn("—", self._html())

    def test_la_page_de_depot_non_plus(self):
        self.assertNotIn(
            "—", self.client.get("/echanges/deposer").get_data(as_text=True))
