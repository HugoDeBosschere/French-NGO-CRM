"""La sauvegarde : elle est relue, elle tourne, elle ne perd rien.

C'est le seul filet de l'outil — la base vit dans un conteneur, et rien ne
l'écrit ailleurs. Deux choses sont donc vérifiées ici plutôt que supposées : que
la copie est relue avant d'être annoncée (une sauvegarde que personne n'a
ouverte est une espérance), et qu'un passage planifié deux fois le même jour
n'écrase pas la copie précédente.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "utils"))

import backup_db  # noqa: E402


def a_database(path, fiches=3):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE mails (id INTEGER PRIMARY KEY, subject TEXT);")
    for i in range(fiches):
        db.execute("INSERT INTO persons (name) VALUES (?)", (f"Fiche {i}",))
    db.execute("INSERT INTO mails (subject) VALUES ('Entretien')")
    db.commit()
    db.close()
    return path


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = a_database(os.path.join(self.dir, "meetings.db"))

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "utils", "backup_db.py"),
             "--db", self.db, *args],
            capture_output=True, text=True)

    def _backups(self):
        return sorted(f for f in os.listdir(self.dir) if ".bak" in f)

    def test_a_backup_is_written_and_read_back(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("relue : 3 fiche(s), 1 courriel(s)", result.stdout)
        self.assertEqual(len(self._backups()), 1)

    def test_the_copy_holds_the_same_rows(self):
        self._run()
        copy = sqlite3.connect(os.path.join(self.dir, self._backups()[0]))
        self.addCleanup(copy.close)
        self.assertEqual(
            copy.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 3)

    def test_twice_in_one_day_keeps_both(self):
        # Un timer quotidien relancé à la main ne doit ni échouer ni écraser la
        # sauvegarde du matin.
        self._run()
        self._run()
        self.assertEqual(len(self._backups()), 2)

    def test_four_runs_in_one_second_keep_four_copies(self):
        # Deux passages dans la même seconde portaient le même nom : le second
        # écrasait le premier sans rien dire. Quatre copies, quatre fichiers.
        for _ in range(4):
            self._run()
        self.assertEqual(len(self._backups()), 4)

    def test_rotation_keeps_the_most_recent(self):
        for _ in range(4):
            self._run()
        self._run("--keep", "2")
        self.assertEqual(len(self._backups()), 2)

    def test_an_empty_database_is_refused(self):
        # Une base valide mais vide est un échec tout aussi silencieux qu'un
        # fichier tronqué : la restauration rendrait un outil vierge.
        empty = a_database(os.path.join(self.dir, "vide.db"), fiches=0)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "utils", "backup_db.py"),
             "--db", empty], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("inutilisable", result.stdout)

    def test_verify_catches_a_corrupted_file(self):
        self._run()
        victim = os.path.join(self.dir, self._backups()[0])
        with open(victim, "r+b") as handle:      # on abîme la copie
            handle.seek(0)
            handle.write(b"ceci n'est pas une base SQLite")
        ok, detail = backup_db.verify(victim)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_a_destination_directory_is_created(self):
        cible = os.path.join(self.dir, "sauvegardes")
        self._run("--dir", cible, "--keep", "5")
        self.assertTrue(os.path.isdir(cible))
        self.assertEqual(len(os.listdir(cible)), 1)


if __name__ == "__main__":
    unittest.main()
