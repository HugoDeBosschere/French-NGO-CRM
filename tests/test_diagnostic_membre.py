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


class BoiteTests(unittest.TestCase):
    """Un IMAP qui se comporte comme imaplib : pas de SEARCH avant SELECT.

    La première version appelait `connect_imap()`, qui authentifie sans
    sélectionner, et tombait sur « command SEARCH illegal in state AUTH, only
    allowed in states SELECTED » — dès le premier lancement réel, sur le seul
    maillon que la base ne peut pas raconter.
    """

    class FauxImap:
        def __init__(self):
            self.etat = "AUTH"
            self.ferme = False

        def select(self, boite, readonly=False):
            self.etat = "SELECTED"
            return "OK", [b"2"]

        def search(self, charset, *criteres):
            if self.etat != "SELECTED":
                raise AssertionError(
                    "command SEARCH illegal in state AUTH, "
                    "only allowed in states SELECTED")
            return "OK", [b"1 2"]

        def logout(self):
            self.ferme = True

    def setUp(self):
        self.faux = self.FauxImap()
        self._vrai = dm.connect_imap
        dm.connect_imap = lambda: self.faux
        self.addCleanup(setattr, dm, "connect_imap", self._vrai)

    def test_la_boite_est_selectionnee_avant_toute_recherche(self):
        flux = io.StringIO()
        with redirect_stdout(flux):
            uids = dm.maillon_1("elie@pauseia.fr", 10)
        self.assertEqual(uids, [b"1", b"2"])
        self.assertIn("lecture seule", flux.getvalue())

    def test_la_boite_est_ouverte_en_lecture_seule(self):
        appels = []
        vrai_select = self.faux.select

        def select(boite, readonly=False):
            appels.append(readonly)
            return vrai_select(boite, readonly)

        self.faux.select = select
        with redirect_stdout(io.StringIO()):
            dm.maillon_1("elie@pauseia.fr", 10)
        self.assertEqual(appels, [True])

    def test_une_boite_introuvable_est_dite_et_nefait_pas_tomber_le_script(self):
        self.faux.select = lambda boite, readonly=False: ("NO", [b"nope"])
        flux = io.StringIO()
        with redirect_stdout(flux):
            uids = dm.maillon_1("elie@pauseia.fr", 10)
        self.assertEqual(uids, [])
        self.assertIn("MEMBER_IMAP_", flux.getvalue())

    def test_une_connexion_pretee_nest_pas_fermee_par_le_maillon(self):
        # main() ouvre UNE connexion, déjà sélectionnée, pour la recherche
        # et les verdicts : le maillon ne doit ni la resélectionner ni la
        # fermer sous les pieds de l'appelant.
        self.faux.select("INBOX", readonly=True)
        with redirect_stdout(io.StringIO()):
            dm.maillon_1("elie@pauseia.fr", 10, imap=self.faux)
        self.assertFalse(self.faux.ferme)


class EnveloppeTests(unittest.TestCase):
    """Une liste vide ne dit rien ; « l'élu·e était en copie cachée » dit tout.

    Le premier lancement réel a produit sept fois « aucune fiche ne porte
    l'adresse de l'autre côté : » suivi de RIEN. La liste était vide parce que
    De, À et Copie ne contenaient que des adresses PauseIA : l'élu·e était en
    copie cachée. L'affichage cachait la seule information utile.
    """

    @staticmethod
    def _message(entetes):
        import email                                  # noqa: PLC0415
        msg = email.message.Message()
        for nom, valeur in entetes:
            msg[nom] = valeur
        return msg

    def _sortie(self, msg):
        flux = io.StringIO()
        with redirect_stdout(flux):
            dm.enveloppe(msg)
        return flux.getvalue()

    def test_un_en_tete_denveloppe_est_montre(self):
        texte = self._sortie(self._message(
            [("Delivered-To", "suivi-membres@pauseia.fr"),
             ("X-Gm-Original-To", "jean.dupont@assemblee-nationale.fr")]))
        self.assertIn("jean.dupont@assemblee-nationale.fr", texte)
        self.assertIn("X-Gm-Original-To", texte)

    def test_sans_en_tete_denveloppe_on_le_dit_sans_detour(self):
        texte = self._sortie(self._message([("Subject", "Contact")]))
        self.assertIn("définitivement absent", texte)
        self.assertIn("dépôt manuel", texte)

    def test_un_en_tete_vide_ne_compte_pas(self):
        texte = self._sortie(self._message([("Bcc", "   ")]))
        self.assertIn("définitivement absent", texte)


if __name__ == "__main__":
    unittest.main()
