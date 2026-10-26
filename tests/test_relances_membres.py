"""La « Relance prévue » des courriels de membres importés automatiquement.

Demande de Hugo (23/10/2026) : les courriels membres ↔ élu·es ajoutés par
utils/import_member_mails.py arrivaient sans date de relance ni « Qui a reçu /
envoyé le mail ? ». Sans relance, le CRM ne rappelle jamais de relancer
l'élu·e, alors qu'un courriel saisi à la main en reçoit une à +10 jours.

On vérifie l'import (les futurs courriels) et le rattrapage
(utils/backfill_member_relances.py, les courriels déjà importés), sur une base
au vrai schéma de l'application, créée dans un fichier temporaire.

Lancer depuis la racine :  python3 -m unittest discover -s tests
"""
import email
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "utils"))

os.environ.setdefault("CRM_DB_PATH", tempfile.mkstemp(suffix=".db")[1])
os.environ.setdefault("APP_PASSWORD", "x")
import app  # noqa: E402
import backfill_member_relances as rattrapage  # noqa: E402
import import_member_mails as im  # noqa: E402


def base_au_schema_de_l_application():
    """Une base neuve, créée par l'application elle-même (init_db)."""
    chemin = tempfile.mkstemp(suffix=".db")[1]
    ancien, app.DB_PATH = app.DB_PATH, Path(chemin)
    try:
        app.init_db()
    finally:
        app.DB_PATH = ancien
    db = sqlite3.connect(chemin)
    im.ensure_member_tables(db)
    im.ensure_state_table(db)  # imported_mails, comme le fait l'import
    return db


def courriel(expediteur, destinataire, date="Thu, 01 Oct 2026 10:00:00 +0200"):
    return email.message_from_string(
        f"From: {expediteur}\r\nTo: {destinataire}\r\nSubject: Rencontre\r\n"
        f"Date: {date}\r\nMessage-ID: <{expediteur}-{date}@test>\r\n\r\nBonjour,\r\n")


class RegleDesDixJours(unittest.TestCase):
    def test_meme_delai_que_les_formulaires(self):
        # Si l'un change sans l'autre, un courriel importé ne serait plus suivi
        # comme un courriel saisi à la main.
        self.assertEqual(im.RELANCE_JOURS, app.FOLLOW_UP_DEFAULT_DAYS["mail"])

    def test_dix_jours_apres_le_courriel(self):
        self.assertEqual(im.relance_par_defaut("2026-10-01"), "2026-10-11")
        self.assertEqual(im.relance_par_defaut("2026-12-28"), "2027-01-07")

    def test_date_inutilisable_pas_de_relance(self):
        self.assertIsNone(im.relance_par_defaut(""))
        self.assertIsNone(im.relance_par_defaut("01/10/2026"))


class ImportAvecRelance(unittest.TestCase):
    def setUp(self):
        self.db = base_au_schema_de_l_application()
        self.db.execute("INSERT INTO moderators (name) VALUES ('Antoine Stm')")
        cur = self.db.execute(
            "INSERT INTO persons (name, email, political_group, stance, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Gabrielle Cathala", "gabrielle.cathala@assemblee-nationale.fr", "LFI",
             "Inconnue", "2026-01-01T00:00:00+00:00"))
        self.elu = (cur.lastrowid, "Gabrielle Cathala")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def enregistrer(self, membre, faible=False):
        msg = courriel("antoine@pauseia.fr", "gabrielle.cathala@assemblee-nationale.fr")
        im.record(self.db, msg, "sent", [self.elu], membre, None, faible,
                  dry_run=False, auto_publish=True)

    def test_courriel_publie_avec_relance_et_utilisateurice(self):
        # Nom d'en-tête en majuscules et espaces en trop : même règle que l'app.
        self.enregistrer(("antoine@pauseia.fr", "ANTOINE  STM"))
        relance, recu_par = self.db.execute(
            "SELECT follow_up_date, received_by FROM mails").fetchone()
        self.assertEqual(relance, "2026-10-11")
        mod_id = self.db.execute("SELECT id FROM moderators").fetchone()[0]
        self.assertEqual(recu_par, mod_id)

    def test_membre_qui_n_est_pas_utilisateurice(self):
        self.enregistrer(("elie@pauseia.fr", "Élie"))
        relance, recu_par = self.db.execute(
            "SELECT follow_up_date, received_by FROM mails").fetchone()
        self.assertEqual(relance, "2026-10-11")  # la relance ne dépend pas du nom
        self.assertIsNone(recu_par)

    def test_brouillon_en_moderation_aussi(self):
        self.enregistrer(("antoine@pauseia.fr", "Antoine Stm"), faible=True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mails").fetchone()[0], 0)
        relance = self.db.execute("SELECT follow_up_date FROM pending_mails").fetchone()[0]
        self.assertEqual(relance, "2026-10-11")


class Rattrapage(unittest.TestCase):
    def setUp(self):
        self.db = base_au_schema_de_l_application()
        self.db.execute("INSERT INTO moderators (name) VALUES ('Antoine Stm')")
        self.mod_id = self.db.execute("SELECT id FROM moderators").fetchone()[0]
        self.antoine = self.membre("antoine@pauseia.fr", "Antoine Stm")
        self.elie = self.membre("elie@pauseia.fr", "Élie")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def membre(self, adresse, nom):
        return self.db.execute(
            "INSERT INTO members (email, name, created_at) VALUES (?, ?, ?)",
            (adresse, nom, "2026-01-01T00:00:00+00:00")).lastrowid

    def courriel(self, ajoute_le, membre=None, relance=None, recu_par=None):
        mail_id = self.db.execute(
            "INSERT INTO mails (mail_date, direction, subject, summary, "
            "follow_up_date, received_by, created_at) VALUES (?, 'sent', ?, ?, ?, ?, ?)",
            (ajoute_le[:10], "Objet", "Corps", relance, recu_par, ajoute_le)).lastrowid
        if membre:
            self.db.execute("INSERT INTO mail_members (mail_id, member_id) VALUES (?, ?)",
                            (mail_id, membre))
        return mail_id

    def relance(self, mail_id):
        return self.db.execute("SELECT follow_up_date, received_by FROM mails "
                               "WHERE id = ?", (mail_id,)).fetchone()

    def test_relance_a_dix_jours_de_l_ajout(self):
        m = self.courriel("2026-10-20T08:05:00+00:00", self.antoine)
        rattrapage.rattraper_publies(self.db, dry_run=False)
        self.assertEqual(self.relance(m), ("2026-10-30", self.mod_id))

    def test_ne_touche_ni_aux_relances_existantes_ni_aux_courriels_saisis(self):
        deja = self.courriel("2026-10-20T08:00:00+00:00", self.antoine, relance="2026-11-15")
        a_la_main = self.courriel("2026-10-20T08:00:00+00:00", membre=None)
        rattrapage.rattraper_publies(self.db, dry_run=False)
        self.assertEqual(self.relance(deja)[0], "2026-11-15")
        self.assertIsNone(self.relance(a_la_main)[0])

    def test_n_ecrase_pas_qui_a_recu(self):
        self.db.execute("INSERT INTO moderators (name) VALUES ('Hugo')")
        hugo = self.db.execute("SELECT id FROM moderators WHERE name = 'Hugo'").fetchone()[0]
        m = self.courriel("2026-10-20T08:00:00+00:00", self.antoine, recu_par=hugo)
        rattrapage.rattraper_publies(self.db, dry_run=False)
        self.assertEqual(self.relance(m), ("2026-10-30", hugo))

    def test_membre_sans_utilisateurice(self):
        m = self.courriel("2026-10-20T08:00:00+00:00", self.elie)
        rattrapage.rattraper_publies(self.db, dry_run=False)
        self.assertEqual(self.relance(m), ("2026-10-30", None))

    def test_simulation_n_ecrit_rien(self):
        m = self.courriel("2026-10-20T08:00:00+00:00", self.antoine)
        poses, _ = rattrapage.rattraper_publies(self.db, dry_run=True)
        self.assertEqual(poses, 1)
        self.assertEqual(self.relance(m), (None, None))

    def test_depuis_ecarte_les_anciens(self):
        vieux = self.courriel("2026-03-01T08:00:00+00:00", self.antoine)
        recent = self.courriel("2026-10-20T08:00:00+00:00", self.antoine)
        rattrapage.rattraper_publies(self.db, dry_run=False, depuis="2026-09-01")
        self.assertIsNone(self.relance(vieux)[0])
        self.assertEqual(self.relance(recent)[0], "2026-10-30")

    def test_idempotent(self):
        self.courriel("2026-10-20T08:00:00+00:00", self.antoine)
        self.assertEqual(rattrapage.rattraper_publies(self.db, dry_run=False)[0], 1)
        self.assertEqual(rattrapage.rattraper_publies(self.db, dry_run=False)[0], 0)

    def test_brouillons_de_l_import(self):
        self.db.execute(
            "INSERT INTO pending_mails (mail_date, direction, summary, submitted_by, "
            "created_at) VALUES ('2026-10-20', 'sent', 'Corps', ?, ?)",
            (im.IMPORT_SOURCE, "2026-10-20T08:00:00+00:00"))
        self.db.execute(
            "INSERT INTO pending_mails (mail_date, direction, summary, submitted_by, "
            "created_at) VALUES ('2026-10-20', 'sent', 'Corps', 'Une citoyenne', ?)",
            ("2026-10-20T08:00:00+00:00",))
        rattrapage.rattraper_brouillons(self.db, dry_run=False)
        relances = dict(self.db.execute(
            "SELECT submitted_by, follow_up_date FROM pending_mails").fetchall())
        self.assertEqual(relances[im.IMPORT_SOURCE], "2026-10-30")
        self.assertIsNone(relances["Une citoyenne"])  # déclaration : pas touchée


if __name__ == "__main__":
    unittest.main()
