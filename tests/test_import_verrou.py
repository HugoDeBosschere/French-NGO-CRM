"""L'import des mails membres ne doit pas garder la base verrouillée pendant
qu'il lit la boîte IMAP.

Alertes du 29/10/2026 : import-member-mails.service échouait toutes les dix
minutes sur « sqlite3.OperationalError: database is locked ». Le balayage
complet (`--backfill`, étape 5 de civicrm-sync) parcourt plus de 4 400 courriels.
Il ouvrait une transaction d'écriture au premier courriel enregistré et ne la
validait qu'au bout de COMMIT_EVERY courriels : pendant tout ce temps, passé
surtout à télécharger des courriels, SQLite refusait toute autre écriture. Au-delà
de 30 secondes (busy_timeout), l'import de 10 minutes, l'application et les
autres scripts échouaient.

On vérifie qu'à chaque téléchargement, une autre connexion peut écrire
immédiatement (timeout 0), comme le ferait l'import de 10 minutes.

Lancer depuis la racine :  python3 -m unittest discover -s tests
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "utils"))

os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
os.environ.setdefault("APP_PASSWORD", "x")
import app  # noqa: E402
import import_member_mails as im  # noqa: E402


def base_au_schema_de_l_application():
    chemin = tempfile.mkstemp(suffix=".db")[1]
    ancien, app.DB_PATH = app.DB_PATH, Path(chemin)
    try:
        app.init_db()
    finally:
        app.DB_PATH = ancien
    db = sqlite3.connect(chemin)
    db.execute(
        "INSERT INTO persons (name, email, political_group, stance, created_at) "
        "VALUES ('Gabrielle Cathala', 'gabrielle.cathala@assemblee-nationale.fr', "
        "'LFI', 'Inconnue', '2026-01-01T00:00:00+00:00')")
    db.commit()
    db.close()
    return chemin


def courriel(n, expediteur):
    return (
        f"From: Antoine <{expediteur}>\r\n"
        "To: gabrielle.cathala@assemblee-nationale.fr\r\n"
        f"Subject: Rencontre {n}\r\nDate: Thu, 01 Oct 2026 10:00:00 +0200\r\n"
        f"Message-ID: <verrou-{n}@test>\r\n\r\nBonjour,\r\n").encode()


class FausseBoite:
    """Assez d'IMAP pour main() ; à chaque téléchargement, une autre connexion
    tente d'écrire sans attendre, comme l'import de 10 minutes."""

    def __init__(self, messages, chemin_base):
        self.messages = messages
        self.chemin_base = chemin_base
        self.ecritures_refusees = 0
        self.telechargements = 0

    def select(self, mailbox, readonly=True):
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, commande, *args):
        if commande == "search":
            uids = " ".join(str(u) for u in self.messages)
            return "OK", [uids.encode()]
        if commande == "fetch":
            self.telechargements += 1
            autre = sqlite3.connect(self.chemin_base, timeout=0)
            try:
                autre.execute("BEGIN IMMEDIATE")  # prend le verrou d'écriture
                autre.rollback()
            except sqlite3.OperationalError:
                self.ecritures_refusees += 1
            finally:
                autre.close()
            uid = int(args[0])
            return "OK", [(b"1 (RFC822)", self.messages[uid])]
        raise AssertionError(commande)

    def logout(self):
        pass


class VerrouCourt(unittest.TestCase):
    def lancer(self, messages, *options):
        chemin = base_au_schema_de_l_application()
        boite = FausseBoite(messages, chemin)
        env = {"IMAP_DB_PATH": chemin, "IMPORT_AUTO_PUBLISH": "1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(im, "connect_imap", return_value=boite), \
                mock.patch.object(sys, "argv", ["import_member_mails.py", *options]), \
                mock.patch.object(im, "log"):
            im.main()
        return boite, chemin

    def test_personne_n_est_bloque_pendant_les_telechargements(self):
        # Membres (enregistrés) et inconnus (mis en file) mélangés : les deux
        # chemins écrivent dans la base.
        messages = {u: courriel(u, "antoine@pauseia.fr" if u % 2 else
                                f"inconnu{u}@exemple-media.fr")
                    for u in range(1, 2 * im.COMMIT_EVERY + 7)}
        boite, chemin = self.lancer(messages, "--backfill")
        self.assertEqual(boite.telechargements, len(messages))
        self.assertEqual(boite.ecritures_refusees, 0)
        db = sqlite3.connect(chemin)
        # Rien de perdu : chaque courriel de membre est bien enregistré.
        n = db.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
        self.assertEqual(n, len([u for u in messages if u % 2]))
        db.close()

    def test_le_point_de_reprise_avance_toujours(self):
        messages = {u: courriel(u, "antoine@pauseia.fr")
                    for u in range(1, im.COMMIT_EVERY + 3)}
        _, chemin = self.lancer(messages)
        db = sqlite3.connect(chemin)
        self.assertEqual(im.get_last_uid(db, "members"), max(messages))
        db.close()


if __name__ == "__main__":
    unittest.main()
