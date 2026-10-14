"""Un essai à blanc qui annonce 0 là où le vrai passage en fait 17 ne sert à rien.

Le 12/10/2026, `--backfill --dry-run` a annoncé « queued for CiviCRM: 0 |
outside the queue's scope: 0 | queued because the body mentions élu·es: 0 »,
et le passage réel, sur la même boîte, a mis 17 adresses en file. La cause :
`queue_unknown_counterparts` n'était pas appelé en essai à blanc. Ce sont
précisément les trois chiffres qu'on regarde avant d'élargir un filtre.
"""
import email
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import import_member_mails as im  # noqa: E402
from civicrm_lookup import ensure_civicrm_tables  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def message(bcc):
    return email.message_from_string(
        "From: elie@pauseia.fr\r\n"
        "To: suivi-membres@pauseia.fr\r\n"
        f"Bcc: {bcc}\r\n"
        "Subject: Contact\r\n"
        "\r\nBonjour, au sujet des députés.\r\n"
    )


class FideliteTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE persons (id INTEGER PRIMARY KEY, "
                        "name TEXT, email TEXT)")
        im.ensure_member_tables(self.db)
        ensure_civicrm_tables(self.db)
        self.db.commit()

    def _en_file(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM civicrm_pending").fetchone()[0]

    def test_lessai_a_blanc_compte_comme_le_vrai_passage(self):
        msg = message("inconnu@assemblee-nationale.fr")
        blanc = im.queue_unknown_counterparts(self.db, msg, NOW, dry_run=True)
        vrai = im.queue_unknown_counterparts(self.db, msg, NOW)
        self.assertEqual(blanc[0], vrai[0])
        self.assertEqual(blanc[0], 1)

    def test_lessai_a_blanc_nécrit_rien(self):
        im.queue_unknown_counterparts(
            self.db, message("inconnu@assemblee-nationale.fr"), NOW,
            dry_run=True)
        self.assertEqual(self._en_file(), 0)

    def test_le_vrai_passage_ecrit(self):
        im.queue_unknown_counterparts(
            self.db, message("inconnu@assemblee-nationale.fr"), NOW)
        self.assertEqual(self._en_file(), 1)

    def test_le_hors_perimetre_est_compte_aussi_a_blanc(self):
        # Une adresse hors périmètre dont le corps NE parle PAS d'élu·es.
        msg = email.message_from_string(
            "From: elie@pauseia.fr\r\nTo: suivi-membres@pauseia.fr\r\n"
            "Bcc: medecin@cabinet-prive.example\r\nSubject: RDV\r\n"
            "\r\nBonjour, confirmation de rendez-vous.\r\n")
        _queued, hors, _elus = im.queue_unknown_counterparts(
            self.db, msg, NOW, dry_run=True)
        # Deux, et non une : la boîte d'audit elle-même (suivi-membres@, une
        # adresse de service, donc pas un membre) est comptée hors périmètre à
        # chaque message. C'est l'essentiel des 970 vus en production — du
        # bruit de compteur, pas des adresses en attente.
        self.assertEqual(hors, 2)
        self.assertEqual(self._en_file(), 0)


if __name__ == "__main__":
    unittest.main()
