"""La fusion des fiches en double, et surtout : qu'on ne perde aucun échange.

Trois fiches « Yannick Jadot » liées au même courriel, « Anne-Flore Gaspard »
deux fois et « Anne-Flaure Gaspard » une troisième : les scripts d'insertion
apparient par (nom normalisé, type de contact), si bien qu'une personne arrivée
par deux chemins obtient deux fiches.

Ce fichier teste d'abord la seule chose qui ne se rattrape pas : un courriel,
une rencontre, une intervention ou un contenu qui perdrait son rattachement.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "utils", "fusionner_doublons.py")

SCHEMA = """
CREATE TABLE persons (
    id INTEGER PRIMARY KEY, name TEXT, contact_type TEXT, role TEXT,
    political_group TEXT, email TEXT, circonscription TEXT,
    photo_url TEXT, fiche_url TEXT, created_at TEXT);
CREATE TABLE mails (id INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE mail_persons (
    mail_id INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (mail_id, person_id));
CREATE TABLE meetings (id INTEGER PRIMARY KEY);
CREATE TABLE meeting_persons (
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, person_id));
CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE person_organisations (
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, organisation_id));
CREATE TABLE person_emails (
    email TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source TEXT, created_at TEXT);
CREATE TABLE thread_persons (message_id TEXT PRIMARY KEY, person_ids TEXT);
"""


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.chemin = tempfile.mkstemp(suffix=".db")[1]
        self.addCleanup(os.unlink, self.chemin)
        self.db = sqlite3.connect(self.chemin)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.addCleanup(self.db.close)

    def _personne(self, nom, **champs):
        cols = ["name"] + list(champs)
        vals = [nom] + list(champs.values())
        cur = self.db.execute(
            f"INSERT INTO persons ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", vals)
        self.db.commit()
        return cur.lastrowid

    def _lancer(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, "--db", self.chemin, *args],
            capture_output=True, text=True)

    def _ids(self, nom):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM persons WHERE name = ?", (nom,))]

    # --- la garantie ------------------------------------------------------

    def test_un_courriel_de_la_fiche_absorbee_suit_la_survivante(self):
        garde = self._personne("Yannick Jadot", fiche_url="https://senat.fr/x")
        perdu = self._personne("Yannick Jadot")
        self.db.execute("INSERT INTO mails (id) VALUES (7)")
        self.db.execute(
            "INSERT INTO mail_persons VALUES (7, ?)", (perdu,))
        self.db.commit()
        self.assertEqual(self._lancer().returncode, 0)
        self.assertEqual(
            [r[0] for r in self.db.execute(
                "SELECT person_id FROM mail_persons WHERE mail_id = 7")],
            [garde])

    def test_un_courriel_lie_aux_deux_fiches_garde_un_lien_pas_zero(self):
        # Le cas qui fait tomber une fusion naïve : la clé primaire composite
        # refuse le second lien, et une mise à jour non protégée échouerait —
        # ou pire, supprimerait les deux.
        garde = self._personne("Yannick Jadot", fiche_url="https://senat.fr/x")
        perdu = self._personne("Yannick Jadot")
        self.db.execute("INSERT INTO mails (id) VALUES (7)")
        self.db.executemany("INSERT INTO mail_persons VALUES (7, ?)",
                            [(garde,), (perdu,)])
        self.db.commit()
        self._lancer()
        self.assertEqual(
            [r[0] for r in self.db.execute(
                "SELECT person_id FROM mail_persons WHERE mail_id = 7")],
            [garde])

    def test_aucun_echange_ne_devient_orphelin(self):
        garde = self._personne("Anne Dupont", fiche_url="https://an.fr/x")
        perdu = self._personne("anne  DUPONT")      # même nom, normalisé
        self.db.execute("INSERT INTO mails (id) VALUES (1), (2)")
        self.db.execute("INSERT INTO meetings (id) VALUES (3)")
        self.db.executemany("INSERT INTO mail_persons VALUES (?, ?)",
                            [(1, garde), (2, perdu)])
        self.db.execute("INSERT INTO meeting_persons VALUES (3, ?)", (perdu,))
        self.db.commit()
        self._lancer()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(DISTINCT mail_id) FROM mail_persons").fetchone()[0], 2)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(DISTINCT meeting_id) FROM meeting_persons"
        ).fetchone()[0], 1)

    def test_les_organisations_et_adresses_apprises_suivent(self):
        garde = self._personne("Anne Dupont", fiche_url="https://an.fr/x")
        perdu = self._personne("Anne Dupont")
        self.db.execute("INSERT INTO organisations (id, name) VALUES (5, 'LR')")
        self.db.execute("INSERT INTO person_organisations VALUES (?, 5)", (perdu,))
        self.db.execute(
            "INSERT INTO person_emails VALUES ('cab@an.fr', ?, 'fil', 'x')",
            (perdu,))
        self.db.commit()
        self._lancer()
        self.assertEqual(self.db.execute(
            "SELECT person_id FROM person_organisations").fetchone()[0], garde)
        self.assertEqual(self.db.execute(
            "SELECT person_id FROM person_emails").fetchone()[0], garde)

    # --- le choix de la fiche conservée -----------------------------------

    def test_la_fiche_alignee_sur_la_source_officielle_l_emporte(self):
        self._personne("Anne Dupont")                       # créée en premier
        officielle = self._personne("Anne Dupont",
                                    fiche_url="https://an.fr/dyn/deputes/OS1")
        self._lancer()
        self.assertEqual(self._ids("Anne Dupont"), [officielle])

    def test_a_defaut_la_fiche_la_plus_complete(self):
        maigre = self._personne("Marc Olivier")
        riche = self._personne("Marc Olivier", email="m@o.fr",
                               political_group="Écologiste", role="Sénateur·ice")
        self._lancer()
        self.assertEqual(self._ids("Marc Olivier"), [riche])
        self.assertNotIn(maigre, self._ids("Marc Olivier"))

    def test_les_champs_vides_sont_recuperes_sur_les_absorbees(self):
        garde = self._personne("Anne Dupont", fiche_url="https://an.fr/x",
                               role="Députée")
        self._personne("Anne Dupont", email="anne@an.fr",
                       circonscription="Savoie")
        self._lancer()
        ligne = self.db.execute(
            "SELECT email, circonscription FROM persons WHERE id = ?",
            (garde,)).fetchone()
        self.assertEqual(ligne, ("anne@an.fr", "Savoie"))

    def test_les_fonctions_se_cumulent_au_lieu_de_s_ecraser(self):
        garde = self._personne("Anne Dupont", fiche_url="https://an.fr/x",
                               role="Députée")
        self._personne("Anne Dupont", role="Ministre")
        self._lancer()
        role = self.db.execute("SELECT role FROM persons WHERE id = ?",
                               (garde,)).fetchone()[0]
        self.assertIn("Députée", role)
        self.assertIn("Ministre", role)

    # --- ce qu'il ne faut surtout pas fusionner ---------------------------

    def test_deux_fiches_sans_nom_ne_sont_pas_fusionnees_entre_elles(self):
        # Sinon toutes les fiches vides du CRM deviendraient une seule personne.
        self._personne("")
        self._personne("   ")
        self._lancer()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM persons WHERE TRIM(COALESCE(name,'')) = ''"
        ).fetchone()[0], 2)

    def test_des_noms_proches_sont_signales_mais_jamais_fusionnes(self):
        self._personne("Anne-Flore Gaspard")
        self._personne("Anne-Flaure Gaspard")
        sortie = self._lancer("--approchants")
        self.assertEqual(len(self._ids("Anne-Flore Gaspard")), 1)
        self.assertEqual(len(self._ids("Anne-Flaure Gaspard")), 1)
        self.assertIn("NON fusionnés", sortie.stdout)

    def test_deux_personnes_differentes_ne_sont_pas_fusionnees(self):
        self._personne("Anne Dupont")
        self._personne("Marc Olivier")
        self._lancer()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM persons").fetchone()[0], 2)

    def test_au_dela_de_la_limite_le_script_s_arrete_sans_rien_ecrire(self):
        # Le garde-fou des passages automatiques : si le script trouve
        # soudain des centaines de doublons, ce n'est pas que le CRM s'est
        # dégradé d'un coup, c'est que quelque chose a changé.
        for nom in ("Anne Dupont", "Marc Olivier", "Luc Martin"):
            self._personne(nom)
            self._personne(nom)
        resultat = self._lancer("--max", "2")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("au-delà de la limite", resultat.stderr)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM persons").fetchone()[0], 6)

    def test_sous_la_limite_la_fusion_se_fait(self):
        self._personne("Anne Dupont")
        self._personne("Anne Dupont")
        self.assertEqual(self._lancer("--max", "2").returncode, 0)
        self.assertEqual(len(self._ids("Anne Dupont")), 1)

    def test_la_limite_ne_bloque_jamais_un_passage_a_blanc(self):
        # À blanc, rien n'est écrit : la limite n'a pas de raison d'empêcher
        # de REGARDER ce qui serait fusionné — c'est même ce qu'on demande de
        # faire quand la limite a bloqué.
        for nom in ("Anne Dupont", "Marc Olivier", "Luc Martin"):
            self._personne(nom)
            self._personne(nom)
        resultat = self._lancer("--dry-run", "--max", "1")
        self.assertEqual(resultat.returncode, 0)
        self.assertIn("3 fiche(s) seraient absorbées", resultat.stdout)

    def test_a_blanc_n_ecrit_rien(self):
        self._personne("Anne Dupont")
        self._personne("Anne Dupont")
        sortie = self._lancer("--dry-run")
        self.assertIn("Rien écrit", sortie.stdout)
        self.assertEqual(len(self._ids("Anne Dupont")), 2)

    def test_le_fil_de_discussion_suit_la_fiche_conservee(self):
        # thread_persons garde des identifiants en CSV, hors clé étrangère :
        # sans reprise, une réponse serait attribuée à une fiche supprimée et
        # finirait dans la file « à rattacher ».
        garde = self._personne("Anne Dupont", fiche_url="https://an.fr/x")
        perdu = self._personne("Anne Dupont")
        self.db.execute("INSERT INTO thread_persons VALUES ('<a@b>', ?)",
                        (f"{perdu},99",))
        self.db.commit()
        self._lancer()
        ids = self.db.execute(
            "SELECT person_ids FROM thread_persons").fetchone()[0]
        self.assertEqual(ids, f"{garde},99")


if __name__ == "__main__":
    unittest.main()
