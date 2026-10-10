"""Cover the CiviCRM bridge: the contract, the value mapping, and the queue.

The mapping is the part a CiviCRM upgrade can silently break, so the contract
test here is the same guard the scripts rely on at runtime.
"""
import email
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "utils"))

import civicrm as cc  # noqa: E402
import maildomains as md  # noqa: E402
import import_member_mails as im  # noqa: E402
import civicrm_lookup as cl  # noqa: E402
from import_civicrm_medias import load_media_index, upsert_media  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")
TODAY = NOW[:10]

# A record shaped exactly as `cv api4 Contact.get` returns one, taken from the
# real export (Le Figaro's newsroom uses <initial><surname>@lefigaro.fr).
FIGARO = {
    "id": 792,
    "display_name": "Tristan Vey",
    "contact_sub_type": ["Journaliste"],
    "email_primary.email": "tvey@lefigaro.fr",
    "employer_id.display_name": "LE FIGARO",
    "Analyse_strat_gique_Pause_IA.Alignement": "2",
    "Analyse_strat_gique_Pause_IA.Niveau_d_influence": "3",
    "Description_courte.Description_courte": "Sciences et technologies.",
    "Compte_R_seaux_Sociaux.Twitter": "https://x.com/TristanVey",
    "Compte_R_seaux_Sociaux.LinkedIn": "",
}


class ContractTests(unittest.TestCase):
    def test_complete_export_passes(self):
        cc.assert_contract([FIGARO], cc.CONTACT_FIELDS)

    def test_empty_export_is_legitimate(self):
        cc.assert_contract([], cc.CONTACT_FIELDS)

    def test_a_missing_optional_field_only_warns(self):
        # Exactly what the 593-contact export did: CiviCRM stores the social
        # group as multi-record, so APIv4's dotted syntax never returns it.
        # Losing a Twitter handle must not refuse 593 journalists.
        export = dict(FIGARO)
        del export["Compte_R_seaux_Sociaux.Twitter"]
        del export["Compte_R_seaux_Sociaux.LinkedIn"]
        said = []
        absent = cc.assert_contract([export], cc.CONTACT_FIELDS, "contact",
                                    optional=cc.CONTACT_FIELDS_OPTIONAL,
                                    warn=said.append)
        self.assertEqual(set(absent), set(cc.CONTACT_FIELDS_OPTIONAL))
        self.assertTrue(said and "facultatif" in said[0])

    def test_a_required_field_still_stops_everything(self):
        export = dict(FIGARO)
        del export["employer_id.display_name"]
        with self.assertRaises(cc.ContractError) as ctx:
            cc.assert_contract([export], cc.CONTACT_FIELDS, "contact",
                               optional=cc.CONTACT_FIELDS_OPTIONAL, warn=None)
        self.assertIn("employer_id", str(ctx.exception))

    def test_one_odd_record_does_not_fail_the_whole_export(self):
        # Deciding from records[0] alone — as this did at first — turned a
        # single unusual row into a failed run of the entire export.
        odd = {"id": 1, "display_name": "Partiel"}
        with_odd_first = [odd] + [dict(FIGARO) for _ in range(5)]
        cc.assert_contract(with_odd_first, cc.CONTACT_FIELDS, "contact",
                           optional=cc.CONTACT_FIELDS_OPTIONAL, warn=None)

    def test_renamed_custom_field_is_caught(self):
        broken = dict(FIGARO)
        del broken["Analyse_strat_gique_Pause_IA.Alignement"]
        with self.assertRaises(cc.ContractError) as ctx:
            cc.assert_contract([broken], cc.CONTACT_FIELDS)
        self.assertIn("Alignement", str(ctx.exception))

    def test_non_list_is_caught(self):
        with self.assertRaises(cc.ContractError):
            cc.assert_contract({"id": 1}, cc.CONTACT_FIELDS)


class MappingTests(unittest.TestCase):
    def test_journalist_maps_whole_record(self):
        row = cc.contact_to_person(FIGARO, TODAY)
        self.assertEqual(row["name"], "Tristan Vey")
        self.assertEqual(row["contact_type"], "Journaliste")
        self.assertEqual(row["stance"], "Plutôt favorable")   # Alignement "2"
        self.assertEqual(row["email"], "tvey@lefigaro.fr")
        self.assertEqual(row["media_name"], "LE FIGARO")
        self.assertEqual(row["social_links"], "https://x.com/TristanVey")
        self.assertIn("Sciences et technologies.", row["notes"])
        self.assertIn("Élevé", row["notes"])                  # Niveau d'influence "3"

    def test_alignement_is_read_by_value_not_label(self):
        for value, stance in (("1", "Neutre / indécis"), ("3", "Favorable"),
                              ("4", "Opposé"), ("5", "Inconnu")):
            rec = dict(FIGARO, **{"Analyse_strat_gique_Pause_IA.Alignement": value})
            self.assertEqual(cc.contact_to_person(rec, TODAY)["stance"], stance)

    def test_missing_alignement_is_unknown(self):
        rec = dict(FIGARO, **{"Analyse_strat_gique_Pause_IA.Alignement": None})
        self.assertEqual(cc.contact_to_person(rec, TODAY)["stance"], "Inconnu")

    def test_unmapped_subtype_keeps_its_origin_in_the_notes(self):
        rec = dict(FIGARO, contact_sub_type=["Influenceur"])
        row = cc.contact_to_person(rec, TODAY)
        self.assertEqual(row["contact_type"], "Autre")
        self.assertIn("Influenceur", row["notes"])

    def test_a_volunteer_maps_to_autre_and_is_therefore_out_of_scope(self):
        # The first real sync found 26 contacts by address and 21 were the
        # association's own volunteers and allies — Hugo De Bosschere among
        # them, a teammate. They are legitimately in CiviCRM; they have no
        # business being contacts in a journal of external relations. The
        # marker --apply keys off is contact_type == "Autre".
        for subtype in ("B_n_vole", "Sympathisant"):
            row = cc.contact_to_person(
                dict(FIGARO, contact_sub_type=[subtype],
                     display_name="Jeanne Bazard",
                     **{"email_primary.email": "jeannebaz@protonmail.com"}),
                TODAY)
            self.assertEqual(row["contact_type"], "Autre", subtype)
            self.assertIn(subtype, row["notes"])

    def test_a_journalist_at_a_local_radio_is_in_scope(self):
        # From the same batch: the five that did belong there.
        row = cc.contact_to_person(
            dict(FIGARO, display_name="Aurélien Vurli",
                 **{"email_primary.email": "aurelien.vurli@rcf.fr",
                    "employer_id.display_name": "RCF HAUTS DE FRANCE"}),
            TODAY)
        self.assertEqual(row["contact_type"], "Journaliste")
        self.assertEqual(row["media_name"], "RCF HAUTS DE FRANCE")

    def test_multivalued_subtype_prefers_the_mapped_one(self):
        rec = dict(FIGARO, contact_sub_type=["B_n_vole", "Journaliste"])
        self.assertEqual(cc.contact_to_person(rec, TODAY)["contact_type"],
                         "Journaliste")

    def test_a_newsroom_desk_recorded_as_a_contact_is_dropped(self):
        # Fifteen of these turned up in the real 593-contact export of group 12.
        for desk in ("debats@lefigaro.fr", "standard@lepoint.fr",
                     "redaction@positivr.fr", "contributions@huffpost.fr",
                     "jean-marc.lalanne@inrocks.com"):
            self.assertTrue(cc.looks_like_an_address(desk), desk)
            self.assertIsNone(
                cc.contact_to_person(dict(FIGARO, display_name=desk), TODAY), desk)

    def test_a_real_name_is_not_mistaken_for_an_address(self):
        for name in ("Tristan Vey", "Caroline De Malet", "Jojol", "M. Tesquet"):
            self.assertFalse(cc.looks_like_an_address(name), name)

    def test_civility_is_stripped_from_the_fiche_name(self):
        # CiviCRM keeps "M. Olivier Tesquet" and "Mme Alexia Borg" as entered.
        for raw, expected in (("M. Olivier Tesquet", "Olivier Tesquet"),
                              ("Mme Alexia Borg", "Alexia Borg"),
                              ("Dr Jean Dupont", "Jean Dupont"),
                              ("Tristan Vey", "Tristan Vey")):
            row = cc.contact_to_person(dict(FIGARO, display_name=raw), TODAY)
            self.assertEqual(row["name"], expected)

    def test_nameless_record_is_dropped(self):
        self.assertIsNone(cc.contact_to_person(dict(FIGARO, display_name="  "),
                                               TODAY))

    def test_media_name_folding_matches_across_case_and_accents(self):
        self.assertEqual(cc.norm_name("LE FIGARO"), cc.norm_name("Le Figaro"))
        self.assertEqual(cc.norm_name("Médiapart"), cc.norm_name("MEDIAPART"))
        self.assertNotEqual(cc.norm_name("LE FIGARO"),
                            cc.norm_name("LE FIGARO ECONOMIE"))


class GenericAddressTests(unittest.TestCase):
    def test_newsroom_desks_are_not_worth_a_fiche(self):
        for addr in ("redaction@lemonde.fr", "contact@lefigaro.fr",
                     "no-reply@lemonde.fr", "presse@ngo.org"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_the_robots_the_first_real_run_actually_queued(self):
        # Every address the audit mailbox produced on 24/09: four robots, zero
        # journalists. They are the reason this filter exists.
        for addr in ("automated@airbnb.com", "notify@mail.notion.com",
                     "notify@mail.notion.so",
                     "bonjour@fresquedesrisquesdelia.org"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_the_robots_the_first_backfill_let_through(self):
        # Straight off the 2 079-message sweep: both were queued a dozen times
        # each before the filters grew to cover them.
        self.assertTrue(cl.is_generic("drive-shares-dm-noreply@google.com"))
        self.assertTrue(cl.is_generic("laredoute@news.laredoute.fr"))

    def test_a_robot_fragment_anywhere_in_the_local_part_counts(self):
        for addr in ("bounce-123@x.fr", "list-unsubscribe@y.org",
                     "auto-notification-42@z.com"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_a_sending_subdomain_is_bulk(self):
        for addr in ("x@news.leparisien.fr", "y@email.airbnb.com",
                     "z@mailing.example.org"):
            self.assertTrue(cl.is_generic(addr), addr)

    def test_a_journalist_on_a_plain_domain_still_passes(self):
        # The filters must not swallow the people this exists for.
        for addr in ("tvey@lefigaro.fr", "jboone@lesechos.fr",
                     "mtual@lemonde.fr", "a.grimonpont@leparisien.fr"):
            self.assertFalse(cl.is_generic(addr), addr)

    def test_a_two_label_domain_is_never_bulk_by_its_name(self):
        # "news.fr" would be a média, not a sending platform.
        self.assertFalse(cl.is_generic("redacteur@news.fr"))

    def test_our_own_domains_are_not_contacts(self):
        # The Fresque site is ours; a mail from it is internal, not a lead.
        self.assertTrue(cl.is_generic("quelquun@fresquedesrisquesdelia.org"))
        self.assertTrue(cl.is_generic("x@mail.fresquedesrisquesdelia.org"))

    def test_a_person_is(self):
        for addr in ("tvey@lefigaro.fr", "sylvain.rolland@latribune.fr",
                     "e.bastie@lefigaro.fr"):
            self.assertFalse(cl.is_generic(addr), addr)

    def test_a_malformed_address_is_skipped_rather_than_queued(self):
        for addr in ("", None, "pas-une-adresse"):
            self.assertTrue(cl.is_generic(addr))


class BulkMailTests(unittest.TestCase):
    """The structural filter: a machine writing to a list, whatever its address."""

    def _msg(self, headers):
        raw = "From: Someone <s@example.org>\r\nTo: m@pauseia.fr\r\n"
        raw += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        return email.message_from_string(raw + "\r\nbody\r\n")

    def test_list_unsubscribe_marks_a_newsletter(self):
        self.assertTrue(cl.is_bulk(self._msg(
            {"List-Unsubscribe": "<https://x.test/u>"})))

    def test_precedence_bulk_and_auto_submitted(self):
        self.assertTrue(cl.is_bulk(self._msg({"Precedence": "bulk"})))
        self.assertTrue(cl.is_bulk(self._msg(
            {"Auto-Submitted": "auto-generated"})))

    def test_auto_submitted_no_is_a_real_person(self):
        self.assertFalse(cl.is_bulk(self._msg({"Auto-Submitted": "no"})))

    def test_a_plain_message_is_not_bulk(self):
        self.assertFalse(cl.is_bulk(self._msg({"Subject": "Votre tribune"})))


class QueueFromMailTests(unittest.TestCase):
    """Ce qui entre dans la file depuis un vrai courriel de membre.

    La file fonctionnait par exclusion : tout ce qui n'était ni un membre, ni un
    robot, ni une adresse générique y entrait. Or la règle Workspace copie
    **toute** la correspondance externe de l'association, mails personnels des
    membres compris — médecin, banque, famille. Ces adresses finissaient dans
    une page consultable par toute l'équipe.

    Il faut donc désormais une raison positive d'entrer : un domaine déjà porté
    par une fiche, un domaine de média attesté par CiviCRM, ou une institution
    publique. Ces tests pinnent les deux côtés de la frontière.
    """

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, email TEXT
            );
            """
        )
        cl.ensure_civicrm_tables(self.db)
        # Une seule fiche au Figaro suffit : il ne s'agit pas de décider à qui
        # appartient le domaine, seulement de savoir qu'il nous concerne.
        self.db.execute("INSERT INTO persons (name, email) "
                        "VALUES ('Tristan Vey', 'tvey@lefigaro.fr')")
        self.db.commit()
        md.refresh_scope(self.db)
        self.addCleanup(md.refresh_seed_only)
        self.addCleanup(self.db.close)

    def _mail(self, frm, to, extra=""):
        raw = (f"From: {frm}\r\nTo: {to}\r\n"
               f"Subject: Votre article\r\nMessage-ID: <x@pauseia.fr>\r\n"
               f"{extra}\r\nBonjour,\r\n")
        return email.message_from_string(raw)

    def _queued(self):
        return {r[0] for r in self.db.execute(
            "SELECT email FROM civicrm_pending")}

    def test_an_unknown_address_on_a_known_media_domain_is_queued(self):
        # Le cas qui justifie la file : un journaliste du Figaro sans fiche.
        im.queue_unknown_counterparts(
            self.db, self._mail("flavien@pauseia.fr", "xnouveau@lefigaro.fr"),
            NOW)
        self.assertIn("xnouveau@lefigaro.fr", self._queued())

    def test_a_personal_freemail_contact_is_never_queued(self):
        # Le médecin d'un membre. Rien ne le distingue d'un journaliste qui
        # écrirait de son gmail : on renonce au second pour protéger le premier.
        im.queue_unknown_counterparts(
            self.db, self._mail("flavien@pauseia.fr", "dr.durand@orange.fr"),
            NOW)
        self.assertEqual(self._queued(), set())

    def test_an_unrelated_company_is_not_queued_either(self):
        im.queue_unknown_counterparts(
            self.db, self._mail("flavien@pauseia.fr", "contact@plombier-92.fr"),
            NOW)
        self.assertEqual(self._queued(), set())

    def test_a_town_hall_is_queued_even_though_nobody_knows_it(self):
        # Les élu·es locaux n'ont aucune source d'adresses exploitable ; cette
        # file est la seule façon de les faire entrer dans le CRM.
        im.queue_unknown_counterparts(
            self.db,
            self._mail("flavien@pauseia.fr", "f.trichet@mairie-nantes.fr"), NOW)
        self.assertIn("f.trichet@mairie-nantes.fr", self._queued())

    def test_a_prefecture_too(self):
        im.queue_unknown_counterparts(
            self.db, self._mail("pref-bop@nord.gouv.fr", "flavien@pauseia.fr"),
            NOW)
        self.assertIn("pref-bop@nord.gouv.fr", self._queued())

    def test_the_count_of_what_was_left_out_is_returned(self):
        # Pour que le journal d'import dise ce qu'il écarte, sans le stocker.
        # Troisième valeur : les adresses hors périmètre entrées quand même
        # parce que le corps parle d'élu·es (voir tests/test_parle_delus.py).
        queued, out_of_scope, via_elus = im.queue_unknown_counterparts(
            self.db, self._mail("flavien@pauseia.fr", "dr.durand@orange.fr"),
            NOW)
        self.assertEqual((queued, out_of_scope, via_elus), (0, 1, 0))

    def test_a_personal_address_writing_about_an_elu_is_queued(self):
        # Le citoyen qui nous répond « j'ai obtenu un rendez-vous avec mon
        # député » : adresse personnelle, donc hors périmètre, mais le corps
        # donne la raison positive d'entrer. Sans ça, il passait à la trappe.
        msg = self._mail("flavien@pauseia.fr", "citoyen@orange.fr")
        msg.set_payload(
            "J'ai obtenu un rendez-vous avec mon député, merci pour l'outil.",
            charset="utf-8")
        queued, out_of_scope, via_elus = im.queue_unknown_counterparts(
            self.db, msg, NOW)
        self.assertEqual((queued, out_of_scope, via_elus), (1, 0, 1))
        self.assertIn("citoyen@orange.fr", self._queued())

    def test_the_member_s_own_address_is_never_queued(self):
        im.queue_unknown_counterparts(
            self.db, self._mail("flavien@pauseia.fr", "x@lefigaro.fr"), NOW)
        self.assertNotIn("flavien@pauseia.fr", self._queued())

    def test_a_bulk_message_queues_nothing(self):
        im.queue_unknown_counterparts(
            self.db,
            self._mail("news@lefigaro.fr", "flavien@pauseia.fr",
                       "List-Unsubscribe: <https://x.test/u>\r\n"),
            NOW)
        self.assertEqual(self._queued(), set())

    def test_member_to_member_queues_nothing(self):
        im.queue_unknown_counterparts(
            self.db, self._mail("flavien@pauseia.fr", "romain@pauseia.fr"), NOW)
        self.assertEqual(self._queued(), set())


class QueueAndApplyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact_type TEXT NOT NULL,
                stance TEXT NOT NULL, email TEXT, social_links TEXT, notes TEXT,
                added_by INTEGER, validated_by INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE organisations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, org_type TEXT NOT NULL, stance TEXT NOT NULL,
                notes TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE person_organisations (
                person_id INTEGER NOT NULL, organisation_id INTEGER NOT NULL,
                PRIMARY KEY (person_id, organisation_id)
            );
            """
        )
        # NB: person_emails is created by ensure_civicrm_tables below.
        cl.ensure_civicrm_tables(self.db)

    def tearDown(self):
        self.db.close()

    def _pending(self, email):
        return self.db.execute(
            "SELECT status, person_id FROM civicrm_pending WHERE email = ?",
            (email,)).fetchone()

    def test_enqueue_is_idempotent_and_counts_sightings(self):
        self.assertTrue(cl.enqueue(self.db, "tvey@lefigaro.fr", "Tristan Vey", NOW))
        self.assertFalse(cl.enqueue(self.db, "TVEY@lefigaro.fr", "", NOW))
        count, = self.db.execute(
            "SELECT seen_count FROM civicrm_pending WHERE email = ?",
            ("tvey@lefigaro.fr",)).fetchone()
        self.assertEqual(count, 2)

    def test_generic_addresses_never_enter_the_queue(self):
        self.assertFalse(cl.enqueue(self.db, "redaction@lemonde.fr", "", NOW))
        self.assertIsNone(self._pending("redaction@lemonde.fr"))

    def test_apply_creates_the_fiche_and_links_the_media(self):
        cl.enqueue(self.db, "tvey@lefigaro.fr", "Tristan Vey", NOW)
        person_id, action = cl.create_or_attach(
            self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
            cl.load_person_index(self.db), load_media_index(self.db))
        self.assertEqual(action, "created")
        name, ctype, mail = self.db.execute(
            "SELECT name, contact_type, email FROM persons WHERE id = ?",
            (person_id,)).fetchone()
        self.assertEqual((name, ctype, mail),
                         ("Tristan Vey", "Journaliste", "tvey@lefigaro.fr"))
        org, = self.db.execute(
            "SELECT o.name FROM organisations o "
            "JOIN person_organisations po ON po.organisation_id = o.id "
            "WHERE po.person_id = ?", (person_id,)).fetchone()
        self.assertEqual(org, "LE FIGARO")

    def test_existing_seeded_fiche_gains_the_address_instead_of_a_duplicate(self):
        # One of the 133 journalists seeded from the old press CRM: a name, no mail.
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Inconnu', ?)", (NOW,))
        person_id, action = cl.create_or_attach(
            self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
            cl.load_person_index(self.db), load_media_index(self.db))
        self.assertEqual(action, "attached")
        total, = self.db.execute(
            "SELECT COUNT(*) FROM persons WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(total, 1)
        mail, = self.db.execute(
            "SELECT email FROM persons WHERE id = ?", (person_id,)).fetchone()
        self.assertEqual(mail, "tvey@lefigaro.fr")

    def test_attaching_fills_the_blanks_of_a_seeded_fiche(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Inconnu', ?)", (NOW,))
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        stance, social, notes = self.db.execute(
            "SELECT stance, social_links, notes FROM persons "
            "WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(stance, "Plutôt favorable")
        self.assertEqual(social, "https://x.com/TristanVey")
        self.assertIn("CiviCRM", notes)

    def test_attaching_never_overwrites_what_a_moderator_typed(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, social_links, notes, "
            "created_at) VALUES ('Tristan Vey', 'Journaliste', 'Opposé', "
            "'https://perso.fr', 'Rencontré en mars.', ?)", (NOW,))
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        stance, social, notes, mail = self.db.execute(
            "SELECT stance, social_links, notes, email FROM persons "
            "WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(stance, "Opposé")            # their judgement wins
        self.assertEqual(social, "https://perso.fr")
        self.assertEqual(notes, "Rencontré en mars.")
        self.assertEqual(mail, "tvey@lefigaro.fr")    # but the blank is filled

    def test_a_hand_typed_address_is_never_overwritten(self):
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Favorable', 'perso@vey.fr', ?)",
            (NOW,))
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        mail, = self.db.execute(
            "SELECT email FROM persons WHERE name = 'Tristan Vey'").fetchone()
        self.assertEqual(mail, "perso@vey.fr")

    def test_the_same_media_is_not_created_twice_across_case(self):
        self.db.execute(
            "INSERT INTO organisations (name, org_type, stance, created_at) "
            "VALUES ('Le Figaro', 'Média', 'Inconnu', ?)", (NOW,))
        index = load_media_index(self.db)
        org_id, created = upsert_media(self.db, "LE FIGARO", NOW, index)
        self.assertFalse(created)
        total, = self.db.execute(
            "SELECT COUNT(*) FROM organisations WHERE org_type = 'Média'").fetchone()
        self.assertEqual(total, 1)
        self.assertIsNotNone(org_id)

    def test_a_second_address_is_kept_as_an_alias(self):
        # CiviCRM holds ~1.6 addresses per journalist. A member wrote to the
        # desk address; the fiche already carries the newsroom one. The second
        # has to stay matchable, or the next mail from it is queued again.
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, email, created_at) "
            "VALUES ('Tristan Vey', 'Journaliste', 'Inconnu', 'tvey@lefigaro.fr', ?)",
            (NOW,))
        row = cc.contact_to_person(FIGARO, TODAY)
        row["email"] = "tristan.vey@lefigaro.fr"          # the matched address
        person_id, action = cl.create_or_attach(
            self.db, row, NOW, cl.load_person_index(self.db),
            load_media_index(self.db))
        self.assertEqual(action, "attached")
        alias = self.db.execute(
            "SELECT person_id, source FROM person_emails WHERE email = ?",
            ("tristan.vey@lefigaro.fr",)).fetchone()
        self.assertEqual(alias, (person_id, "civicrm"))
        # …and the fiche's own address is untouched.
        mail, = self.db.execute(
            "SELECT email FROM persons WHERE id = ?", (person_id,)).fetchone()
        self.assertEqual(mail, "tvey@lefigaro.fr")

    def test_no_alias_row_when_the_address_is_the_fiche_s_own(self):
        cl.create_or_attach(self.db, cc.contact_to_person(FIGARO, TODAY), NOW,
                            cl.load_person_index(self.db), load_media_index(self.db))
        total, = self.db.execute("SELECT COUNT(*) FROM person_emails").fetchone()
        self.assertEqual(total, 0)

    def test_one_person_at_two_medias_stays_one_fiche(self):
        # Pierre Dandumont writes for MacGeneration and iGeneration; the real
        # export carries him twice. Anthony Morel likewise, for BFM and RMC.
        first = dict(FIGARO, display_name="Pierre Dandumont",
                     **{"employer_id.display_name": "MACGENERATION",
                        "email_primary.email": "pd@macg.fr"})
        second = dict(first, **{"employer_id.display_name": "IGENERATION",
                                "email_primary.email": "pd@igen.fr"})
        index, media_index = cl.load_person_index(self.db), load_media_index(self.db)
        pid1, a1 = cl.create_or_attach(
            self.db, cc.contact_to_person(first, TODAY), NOW, index, media_index)
        pid2, a2 = cl.create_or_attach(
            self.db, cc.contact_to_person(second, TODAY), NOW, index, media_index)
        self.assertEqual((a1, a2), ("created", "attached"))
        self.assertEqual(pid1, pid2)
        medias = [r[0] for r in self.db.execute(
            "SELECT o.name FROM organisations o "
            "JOIN person_organisations po ON po.organisation_id = o.id "
            "WHERE po.person_id = ? ORDER BY o.name", (pid1,))]
        self.assertEqual(medias, ["IGENERATION", "MACGENERATION"])

    def test_a_homonym_in_another_type_stays_a_separate_fiche(self):
        # "Laurent Alexandre" is both an LFI député and a chroniqueur.
        self.db.execute(
            "INSERT INTO persons (name, contact_type, stance, created_at) "
            "VALUES ('Laurent Alexandre', 'Politique', 'Inconnu', ?)", (NOW,))
        rec = dict(FIGARO, display_name="Laurent Alexandre",
                   **{"email_primary.email": "lalexandre@lexpress.fr"})
        _pid, action = cl.create_or_attach(
            self.db, cc.contact_to_person(rec, TODAY), NOW,
            cl.load_person_index(self.db), load_media_index(self.db))
        self.assertEqual(action, "created")
        total, = self.db.execute(
            "SELECT COUNT(*) FROM persons WHERE name = 'Laurent Alexandre'"
        ).fetchone()
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()


class ApplyNamesTests(unittest.TestCase):
    """Resolving a queued address by its newsroom's convention.

    The point of the regression here: candidates are grouped by the DOMAIN of
    their own address, not by their employer's name. CiviCRM labels employers
    regionally — francetv.fr's 2 055 journalists are spread over "FRANCE 3 PARIS
    ILE-DE-FRANCE", "FRANCE 3 OCCITANIE" and dozens more — so a média-keyed
    lookup compared a queued address against a few dozen people instead of all
    of them, and quietly resolved almost nothing.
    """

    class Args:
        commit = True
        include_other = False

        def __init__(self, path):
            self.apply_names = path

    def setUp(self):
        import learn_conventions as lc
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact_type TEXT NOT NULL,
                stance TEXT NOT NULL, email TEXT, social_links TEXT, notes TEXT,
                added_by INTEGER, validated_by INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE organisations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, org_type TEXT NOT NULL, stance TEXT NOT NULL,
                notes TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE person_organisations (
                person_id INTEGER NOT NULL, organisation_id INTEGER NOT NULL,
                PRIMARY KEY (person_id, organisation_id)
            );
            """
        )
        cl.ensure_civicrm_tables(self.db)
        lc.ensure_table(self.db)
        lc.store(self.db, [("francetv.fr", "FRANCE 3 PARIS ILE-DE-FRANCE",
                            "prenom.nom", 2055, 0.93)], "civicrm", NOW)
        self.addCleanup(self.db.close)

    def _record(self, cid, name, mail, media):
        row = dict(FIGARO)
        row.update({"id": cid, "display_name": name,
                    "email_primary.email": mail,
                    "employer_id.display_name": media})
        return row

    def _run(self, records):
        import json
        import tempfile
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(records, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        cl.cmd_apply_names(self.db, self.Args(fh.name))

    def test_resolves_against_a_regional_newsroom_it_never_names(self):
        cl.enqueue(self.db, "emmanuel.pall@francetv.fr", "", NOW)
        # The journalist sits under a *different* regional label than the one
        # the convention carries: only the shared domain connects them.
        self._run([self._record(1, "Emmanuel Pall", "e.pall@francetv.fr",
                                "FRANCE 3 OCCITANIE")])
        status, person_id = self.db.execute(
            "SELECT status, person_id FROM civicrm_pending WHERE email = ?",
            ("emmanuel.pall@francetv.fr",)).fetchone()
        self.assertEqual(status, "resolved")
        name, mail, notes = self.db.execute(
            "SELECT name, email, notes FROM persons WHERE id = ?",
            (person_id,)).fetchone()
        self.assertEqual((name, mail), ("Emmanuel Pall",
                                        "emmanuel.pall@francetv.fr"))
        # A convention is a habit, not a rule: the fiche has to say so.
        self.assertIn("à confirmer", notes)
        # And it names the DOMAIN the convention came from, not the label most
        # of that domain's addresses happen to carry — this journalist sits
        # under another regional newsroom entirely.
        self.assertIn("francetv.fr", notes)
        self.assertNotIn("FRANCE 3 PARIS", notes)

    def test_a_freelance_whose_own_address_is_elsewhere_still_counts(self):
        cl.enqueue(self.db, "pierre.debaudouin@francetv.fr", "", NOW)
        self._run([self._record(2, "Pierre Debaudouin",
                                "pierre.debaudouin@gmail.com",
                                "FRANCE 3 PARIS ILE-DE-FRANCE")])
        status, = self.db.execute(
            "SELECT status FROM civicrm_pending WHERE email = ?",
            ("pierre.debaudouin@francetv.fr",)).fetchone()
        self.assertEqual(status, "resolved")

    def test_two_journalists_building_the_same_address_are_left_in_the_queue(self):
        # Two *different* people, one address: Le Figaro's <initiale><nom> turns
        # both Pierre and Paul Dupont into pdupont@. Nobody can tell them apart,
        # so the address waits for a human. (Two people bearing the same name are
        # a different matter: this CRM has always treated a name within a type as
        # one person, and the queue follows that.)
        import learn_conventions as lc
        lc.store(self.db, [("lefigaro.fr", "LE FIGARO", "pnom", 77, 1.0)],
                 "civicrm", NOW)
        cl.enqueue(self.db, "pdupont@lefigaro.fr", "", NOW)
        self._run([
            self._record(3, "Pierre Dupont", "pierre.dupont@lefigaro.fr",
                         "LE FIGARO"),
            self._record(4, "Paul Dupont", "paul.dupont@lefigaro.fr",
                         "LE FIGARO - ONLINE"),
        ])
        status, = self.db.execute(
            "SELECT status FROM civicrm_pending WHERE email = ?",
            ("pdupont@lefigaro.fr",)).fetchone()
        self.assertEqual(status, "pending")

    def test_an_unknown_domain_is_left_alone(self):
        cl.enqueue(self.db, "willa@godemandguide.co", "", NOW)
        self._run([self._record(5, "Willa Nobody", "willa@godemandguide.co",
                                "GO DEMAND GUIDE")])
        status, = self.db.execute(
            "SELECT status FROM civicrm_pending WHERE email = ?",
            ("willa@godemandguide.co",)).fetchone()
        self.assertEqual(status, "pending")


class PatternsOutputTests(unittest.TestCase):
    """`--patterns` stdout must be JSON and nothing else.

    civicrm-sync.sh pipes it into `json.load` to build the CiviCRM query. The
    summary line used to go to stdout as well, so the parse failed and the
    caller's `|| echo {}` turned that into a cheerful "no convention applies".
    """

    def setUp(self):
        import learn_conventions as lc
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE persons (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
            CREATE TABLE organisations (id INTEGER PRIMARY KEY, name TEXT,
                                        org_type TEXT);
            CREATE TABLE person_organisations (person_id INT, organisation_id INT);
            """
        )
        cl.ensure_civicrm_tables(self.db)
        lc.ensure_table(self.db)
        lc.store(self.db, [("francetv.fr", "FRANCE 3", "prenom.nom", 2055, 0.99)],
                 "civicrm", NOW)
        self.addCleanup(self.db.close)

    def _stdout(self, **flags):
        import contextlib
        import io
        import types
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), \
                contextlib.redirect_stderr(io.StringIO()):
            cl.cmd_patterns(self.db, types.SimpleNamespace(**flags))
        return buffer.getvalue()

    def test_stdout_parses_as_json(self):
        cl.enqueue(self.db, "pierre.debaudouin@francetv.fr", "", NOW)
        import json
        data = json.loads(self._stdout(all=False))
        self.assertEqual(data, {"francetv.fr": {"media": "FRANCE 3",
                                                "template": "prenom.nom"}})

    def test_stdout_parses_as_json_with_all(self):
        import json
        self.assertIn("francetv.fr", json.loads(self._stdout(all=True)))

    def test_an_empty_queue_still_yields_valid_json(self):
        import json
        self.assertEqual(json.loads(self._stdout(all=False)), {})


class DuplicateContactTests(ApplyNamesTests):
    """CiviCRM holds duplicate contacts for the same journalist.

    `pierre.debaudouin@francetv.fr` matched five records all reading "Pierre de
    Baudouin". Judging ambiguity on CiviCRM ids made that look like a five-way
    collision, so every duplicated journalist was unresolvable. Ambiguity is
    about names.
    """

    def test_five_records_for_one_person_resolve(self):
        cl.enqueue(self.db, "pierre.debaudouin@francetv.fr", "", NOW)
        self._run([
            self._record(10 + n, "Pierre de Baudouin",
                         f"pierre.debaudouin{n}@francetv.fr", "FRANCE 3")
            for n in range(5)
        ])
        status, person_id = self.db.execute(
            "SELECT status, person_id FROM civicrm_pending WHERE email = ?",
            ("pierre.debaudouin@francetv.fr",)).fetchone()
        self.assertEqual(status, "resolved")
        # And exactly one fiche, not five.
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM persons").fetchone()[0], 1)
        self.assertIsNotNone(person_id)

    def test_a_spelling_variant_of_the_same_name_is_still_one_person(self):
        cl.enqueue(self.db, "pierre.debaudouin@francetv.fr", "", NOW)
        self._run([
            self._record(20, "Pierre de Baudouin", "p.debaudouin@francetv.fr",
                         "FRANCE 3"),
            self._record(21, "Pierre De Baudouin", "pdebaudouin@francetv.fr",
                         "FRANCE 3 OCCITANIE"),
        ])
        status, = self.db.execute(
            "SELECT status FROM civicrm_pending WHERE email = ?",
            ("pierre.debaudouin@francetv.fr",)).fetchone()
        self.assertEqual(status, "resolved")
