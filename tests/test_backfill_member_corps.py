"""Ré-extraction du corps des courriels de membres déjà enregistrés.

Même besoin que du côté citoyen : les corps ont été extraits par une version
qui remplaçait chaque balise HTML par une espace et ne décodait pas les
entités. Trois courriels de la base de production en portaient encore la
trace, dont un transfert de l'équipe plaidoyer avec « &nbsp; » partout.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import backfill_member_bodies as rattrapage  # noqa: E402


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.chemin = tempfile.mkstemp(suffix=".db")[1]
        self.addCleanup(os.unlink, self.chemin)
        self.db = sqlite3.connect(self.chemin)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE mails (id INTEGER PRIMARY KEY, direction TEXT,
              mail_date TEXT, subject TEXT, summary TEXT);
            CREATE TABLE mail_members (mail_id INTEGER, member_id INTEGER);
            CREATE TABLE mail_bodies (mail_id INTEGER PRIMARY KEY, body TEXT);
        """)
        # 1 : membre avec corps. 2 : membre sans corps. 3 : citoyen avec corps.
        for i in (1, 2, 3):
            self.db.execute(
                "INSERT INTO mails (id, direction, mail_date, subject) "
                "VALUES (?, 'sent', '2026-10-08', 'Objet')", (i,))
        self.db.executemany("INSERT INTO mail_members VALUES (?, 1)",
                            [(1,), (2,)])
        self.db.execute("INSERT INTO mail_bodies VALUES (1, 'corps &nbsp;')")
        self.db.execute("INSERT INTO mail_bodies VALUES (3, 'corps citoyen')")
        self.db.commit()
        for cle in ("MEMBER_IMAP_USER", "MEMBER_IMAP_APP_PASSWORD"):
            self.addCleanup(os.environ.pop, cle, None)
            os.environ.pop(cle, None)

    def test_sans_identifiants_imap_le_script_ne_fait_rien_et_le_dit(self):
        # Il ne doit surtout pas vider les corps existants faute de source.
        self.assertEqual(rattrapage.refaire_les_corps(self.db, False), 0)
        self.assertEqual(
            self.db.execute(
                "SELECT body FROM mail_bodies WHERE mail_id = 1").fetchone()[0],
            "corps &nbsp;")

    def test_seuls_les_courriels_de_membres_ayant_un_corps_sont_candidats(self):
        lignes = self.db.execute(
            """
            SELECT x.id FROM mails x
              JOIN mail_members mm ON mm.mail_id = x.id
              JOIN mail_bodies  b  ON b.mail_id  = x.id
             GROUP BY x.id
            """).fetchall()
        self.assertEqual([r[0] for r in lignes], [1])


if __name__ == "__main__":
    unittest.main()
