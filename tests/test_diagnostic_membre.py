"""Le diagnostic doit tourner sur une vraie base, et nommer le bon maillon.

Un membre a signalé que ses échanges n'arrivaient pas dans le CRM. La chaîne
compte cinq maillons et rien, dans l'interface, ne dit lequel a cédé. Ce qui
est testé ici n'est pas la mise en forme, c'est que chaque cause produise la
bonne conclusion — et que le script interroge les colonnes qui existent
réellement, plutôt que de tomber au premier SELECT sur le serveur.
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "utils"))

import diagnostic_membre as dm  # noqa: E402


def base():
    """Une base créée par l'app elle-même : c'est le seul schéma qui compte.

    Le diagnostic lit `mail_members`, `civicrm_pending` et `import_runs`. Une
    base fabriquée à la main dans le test ne prouverait rien : la première
    version du script interrogeait `mails.member_id` et `import_runs.job`, qui
    n'existent ni l'un ni l'autre, et serait tombée sur le serveur.
    """
    os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
    os.environ["APP_PASSWORD"] = "x"
    import app                                       # noqa: PLC0415
    app.init_db()
    db = sqlite3.connect(str(app.DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    import importruns                                # noqa: PLC0415
    from civicrm_lookup import ensure_civicrm_tables  # noqa: PLC0415
    from import_member_mails import ensure_member_tables  # noqa: PLC0415
    importruns.ensure_table(db)
    ensure_member_tables(db)
    ensure_civicrm_tables(db)
    for table in ("mail_members", "mails", "members", "civicrm_pending",
                  "import_runs", "pending_mails"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db


class MaillonsTests(unittest.TestCase):
    def setUp(self):
        self.db = base()
        self.addCleanup(self.db.close)

    @staticmethod
    def _sortie(fn, *a):
        flux = io.StringIO()
        with redirect_stdout(flux):
            resultat = fn(*a)
        return resultat, flux.getvalue()

    def test_une_adresse_personnelle_est_la_cause_et_on_sarrete_la(self):
        reconnu, texte = self._sortie(dm.maillon_3, "elie@gmail.com")
        self.assertFalse(reconnu)
        self.assertIn("pas en @pauseia.fr", texte)

    def test_une_adresse_de_service_est_distinguee_dune_adresse_perso(self):
        reconnu, texte = self._sortie(dm.maillon_3, "contact@pauseia.fr")
        self.assertFalse(reconnu)
        self.assertIn("GROUP_ADDRESSES", texte)

    def test_une_adresse_de_membre_passe(self):
        reconnu, _ = self._sortie(dm.maillon_3, "elie@pauseia.fr")
        self.assertTrue(reconnu)

    def test_un_import_jamais_lance_est_nomme_comme_cause(self):
        _, texte = self._sortie(dm.maillon_2, self.db)
        self.assertIn("Aucun passage", texte)

    def test_un_import_en_echec_est_signale(self):
        self.db.execute(
            "INSERT INTO import_runs (script, started_at, status, imported,"
            " inspected, detail) VALUES ('member_mails', '2026-10-12', 'error',"
            " 0, 0, 'IMAP timeout')"
        )
        _, texte = self._sortie(dm.maillon_2, self.db)
        self.assertIn("ÉCHOUÉ", texte)
        self.assertIn("IMAP timeout", texte)

    def test_un_membre_inconnu_veut_dire_aucun_courriel_jamais_entre(self):
        resultat, texte = self._sortie(dm.maillon_4, self.db, "elie@pauseia.fr")
        self.assertIsNone(resultat)
        self.assertIn("Inconnu de la table", texte)

    def test_un_membre_connu_affiche_son_compte_et_son_dernier_courriel(self):
        self.db.execute(
            "INSERT INTO members (id, email, name, created_at) "
            "VALUES (1, 'elie@pauseia.fr', 'Elie Goudout', '2026-01-01')"
        )
        self.db.execute(
            "INSERT INTO mails (id, mail_date, direction, summary, subject,"
            " created_at) VALUES (7, '2026-09-30', 'sent', 'r', 'Rendez-vous',"
            " '2026-09-30')"
        )
        self.db.execute("INSERT INTO mail_members VALUES (7, 1)")
        resultat, texte = self._sortie(dm.maillon_4, self.db, "elie@pauseia.fr")
        self.assertEqual(resultat, 1)
        self.assertIn("attribués : 1", texte)
        self.assertIn("Rendez-vous", texte)

    def test_une_adresse_en_file_est_nommee_comme_cause(self):
        self.db.execute(
            "INSERT INTO civicrm_pending (email, display, first_seen, last_seen,"
            " last_subject, last_member, last_direction) VALUES"
            " ('depute@exemple.fr', 'Une Députée', '2026-10-01', '2026-10-01',"
            " 'Demande de rendez-vous', 'elie@pauseia.fr', 'sent')"
        )
        _, texte = self._sortie(dm.maillon_5, self.db, "elie@pauseia.fr")
        self.assertIn("depute@exemple.fr", texte)
        self.assertIn("a-rattacher", texte)

    def test_une_file_vide_ecarte_cette_cause(self):
        _, texte = self._sortie(dm.maillon_5, self.db, "elie@pauseia.fr")
        self.assertIn("Aucune", texte)


if __name__ == "__main__":
    unittest.main()
