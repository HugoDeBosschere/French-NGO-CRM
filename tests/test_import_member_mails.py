"""Unit tests for the member-mail classifier (utils/import_member_mails.py).

Focus on classify(): the direction (sent/received), the élu·e matching layers
(address, body scan, name pattern) and the member attribution — the logic that
decides what gets recorded. No network, no IMAP: an in-memory SQLite DB and
e-mail messages built from strings.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import email
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "utils"))

import import_member_mails as im  # noqa: E402


def make_msg(from_, to, subject="Objet test", body="Bonjour,\n\nCordialement.",
             cc=None, extra_headers=""):
    headers = f"From: {from_}\r\nTo: {to}\r\n"
    if cc:
        headers += f"Cc: {cc}\r\n"
    headers += f"Subject: {subject}\r\n"
    if extra_headers:
        headers += extra_headers
    raw = headers + "\r\n" + body
    return email.message_from_string(raw)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT, email TEXT)"
        )
        # A few élu·es: two with official emails, one with only a name (for the
        # name-pattern layer), plus a homonym to prove ambiguity is skipped.
        self.db.executemany(
            "INSERT INTO persons (name, email) VALUES (?, ?)",
            [
                ("Boris Vallaud", "boris.vallaud@assemblee-nationale.fr"),
                ("Dieynaba Diop", "dieynaba.diop@assemblee-nationale.fr"),
                ("Jean Dupont", None),        # unique name, no email
                ("Marie Martin", None),       # homonym pair below
                ("Marie Martin", None),
            ],
        )
        im.ensure_member_tables(self.db)  # members, thread_persons, aliases, …
        self.db.commit()
        self.email_index = im.load_email_index_with_aliases(self.db)
        self.name_patterns = im.build_name_pattern_index(self.db)

    def classify(self, msg):
        return im.classify(msg, self.db, self.email_index, self.name_patterns)

    def test_member_to_elu_is_sent(self):
        msg = make_msg("Romain <romain@pauseia.fr>",
                       "Boris Vallaud <boris.vallaud@assemblee-nationale.fr>")
        direction, matches, member, learn, low = self.classify(msg)
        self.assertEqual(direction, "sent")
        self.assertEqual([n for _p, n in matches], ["Boris Vallaud"])
        self.assertEqual(member, ("romain@pauseia.fr", "Romain"))
        self.assertIsNone(learn)
        self.assertFalse(low)

    def test_elu_to_member_is_received(self):
        msg = make_msg("Boris Vallaud <boris.vallaud@assemblee-nationale.fr>",
                       "Romain <romain@pauseia.fr>")
        direction, matches, member, learn, low = self.classify(msg)
        self.assertEqual(direction, "received")
        self.assertEqual([n for _p, n in matches], ["Boris Vallaud"])
        self.assertEqual(member, ("romain@pauseia.fr", "Romain"))
        self.assertFalse(low)

    def test_multiple_elus_in_recipients(self):
        msg = make_msg(
            "romain@pauseia.fr",
            "boris.vallaud@assemblee-nationale.fr",
            cc="dieynaba.diop@assemblee-nationale.fr",
        )
        direction, matches, _member, _learn, low = self.classify(msg)
        self.assertEqual(direction, "sent")
        self.assertCountEqual([n for _p, n in matches],
                              ["Boris Vallaud", "Dieynaba Diop"])
        self.assertFalse(low)

    def test_group_address_is_not_a_member(self):
        # campagne@ is a group, not an individual member → not member↔élu.
        msg = make_msg("campagne@pauseia.fr",
                       "boris.vallaud@assemblee-nationale.fr")
        direction, matches, member, _learn, _low = self.classify(msg)
        self.assertIsNone(direction)
        self.assertEqual(matches, [])
        self.assertIsNone(member)

    def test_two_non_members_ignored(self):
        msg = make_msg("someone@example.com", "other@example.org")
        self.assertEqual(self.classify(msg), (None, [], None, None, False))

    def test_body_scan_matches_quoted_official_address(self):
        # Reply from a non-official cabinet address to a member, quoting the
        # élu·e's official address in the body → matched via the body scan.
        body = ("Merci pour votre message.\n\n"
                "> De : boris.vallaud@assemblee-nationale.fr\n"
                "> à moi")
        msg = make_msg("Cabinet <contact@cabinet-vallaud.fr>",
                       "Romain <romain@pauseia.fr>", body=body)
        direction, matches, member, _learn, low = self.classify(msg)
        self.assertEqual(direction, "received")
        self.assertEqual([n for _p, n in matches], ["Boris Vallaud"])
        self.assertEqual(member, ("romain@pauseia.fr", "Romain"))
        self.assertFalse(low)

    def test_name_pattern_is_low_confidence(self):
        # Non-official address whose local part maps to a unique élu·e name.
        msg = make_msg("romain@pauseia.fr", "jean.dupont@gmail.com")
        direction, matches, _member, _learn, low = self.classify(msg)
        self.assertEqual(direction, "sent")
        self.assertEqual([n for _p, n in matches], ["Jean Dupont"])
        self.assertTrue(low)  # always routed to moderation

    def test_name_pattern_skips_homonyms(self):
        # "Marie Martin" exists twice → ambiguous → no match, dropped.
        msg = make_msg("romain@pauseia.fr", "marie.martin@gmail.com")
        direction, matches, _member, _learn, _low = self.classify(msg)
        self.assertIsNone(direction)
        self.assertEqual(matches, [])


class HelperTests(unittest.TestCase):
    def test_is_member_excludes_groups_and_other_domains(self):
        self.assertTrue(im.is_member("romain@pauseia.fr"))
        self.assertFalse(im.is_member("campagne@pauseia.fr"))
        self.assertFalse(im.is_member("someone@example.com"))

    def test_is_official_domains(self):
        self.assertTrue(im.is_official("x@senat.fr"))
        self.assertTrue(im.is_official("y@assemblee-nationale.fr"))
        self.assertTrue(im.is_official("z@europarl.europa.eu"))
        self.assertFalse(im.is_official("a@gmail.com"))


if __name__ == "__main__":
    unittest.main()
