#!/usr/bin/env python3
"""Seed the médias and the journalistes into the CRM, from data baked in here.

The politique half of `persons` is reproducible from `actual_dataset/*.json`
plus utils/insert_*.py. This is the same thing for the other half: the 165
médias and 133 journalistes that came from the old journalist CRM, whose
database was never version controlled. Everything is in this file, so a fresh
or a production database can be brought up to date without copying a .db
around.

Idempotent, and matched by name within a type — a média is looked up among
médias, a journaliste among journalistes. So a journaliste who shares a name
with an élu·e stays a separate fiche (« Laurent Alexandre » is both an LFI
député and a chroniqueur), and a rerun adds nothing.

    uv run python utils/insert_medias_journalistes.py            # dry run
    uv run python utils/insert_medias_journalistes.py --commit    # write

`--added-by` names the utilisateurice recorded in « Qui a ajouté » and
« Validé par »; they are created if absent.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "meetings.db")

# Provenance of every row below, unless --added-by says otherwise.
DEFAULT_ADDED_BY = "Hugo"

# Where this data came from, written into every row's notes.
MEDIA_NOTE = 'Pré-rempli le 11/09/2026 (connaissances publiques) — orientation indicative, à vérifier.'

# (name, media_type, orientation, stance, link) — the notes are MEDIA_NOTE for
# all of them, and the stance is what the old CRM held.
MEDIAS = [
    ('01net', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.01net.com'),
    ('20 Minutes', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.20minutes.fr'),
    ('Acteurs publics', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://acteurspublics.fr'),
    ('ActuIA', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.actuia.com'),
    ('AFP', 'Agence de presse', 'Centre', 'Inconnu', 'https://www.afp.com'),
    ('Alternatives économiques', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.alternatives-economiques.fr'),
    ('AOC', 'Site web / pure player', 'Centre gauche', 'Inconnu', 'https://aoc.media'),
    ('Arrêt sur images', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://www.arretsurimages.net'),
    ('Arte', 'Télévision', 'Centre gauche', 'Inconnu', 'https://www.arte.tv'),
    ('Atlantico', 'Site web / pure player', 'Droite', 'Inconnu', 'https://atlantico.fr'),
    ('Basta!', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://basta.media'),
    ('BFM Business', 'Télévision', 'Centre droit', 'Inconnu', 'https://www.bfmtv.com/economie/'),
    ('BFMTV', 'Télévision', 'Centre', 'Inconnu', 'https://www.bfmtv.com'),
    ('Binge Audio', 'Podcast', 'Inconnue', 'Inconnu', 'https://www.binge.audio'),
    ('Blast', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://www.blast-info.fr'),
    ('Blog du Modérateur', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.blogdumoderateur.com'),
    ('Boulevard Voltaire', 'Site web / pure player', 'Extrême droite', 'Inconnu', 'https://www.bvoltaire.fr'),
    ('Brut', 'Site web / pure player', 'Centre gauche', 'Inconnu', 'https://www.brut.media'),
    ('Canal+', 'Télévision', 'Inconnue', 'Inconnu', 'https://www.canalplus.com'),
    ('Capital', 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.capital.fr'),
    ('Causeur', 'Presse écrite', 'Droite', 'Inconnu', 'https://www.causeur.fr'),
    ('Challenges', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.challenges.fr'),
    ('Charente libre', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.charentelibre.fr'),
    ('Charlie Hebdo', 'Presse écrite', 'Gauche', 'Inconnu', 'https://charliehebdo.fr'),
    ('Clique', 'YouTube', 'Inconnue', 'Inconnu', None),
    ('Clubic', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.clubic.com'),
    ('CNews', 'Télévision', 'Extrême droite', 'Inconnu', 'https://www.cnews.fr'),
    ('Contexte', 'Site web / pure player', 'Centre', 'Inconnu', 'https://www.contexte.com'),
    ('Contrepoints', 'Site web / pure player', 'Droite', 'Inconnu', 'https://www.contrepoints.org'),
    ('Corse-Matin', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.corsematin.com'),
    ('Courrier international', 'Presse écrite', 'Centre gauche', 'Inconnu', 'https://www.courrierinternational.com'),
    ('Dans les algorithmes', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://danslesalgorithmes.net'),
    ('Defend Intelligence', 'YouTube', 'Inconnue', 'Inconnu', None),
    ("Dernières Nouvelles d'Alsace", 'Presse écrite', 'Centre', 'Inconnu', 'https://www.dna.fr'),
    ('Epsiloon', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.epsiloon.com'),
    ('Euractiv France', 'Site web / pure player', 'Centre', 'Inconnu', 'https://www.euractiv.fr'),
    ('Euronews', 'Télévision', 'Centre', 'Inconnu', 'https://fr.euronews.com'),
    ('Europe 1', 'Radio', 'Droite', 'Inconnu', 'https://www.europe1.fr'),
    ('Forbes France', 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.forbes.fr'),
    ('France 2', 'Télévision', 'Centre', 'Inconnu', 'https://www.france.tv/france-2/'),
    ('France 24', 'Télévision', 'Centre', 'Inconnu', 'https://www.france24.com'),
    ('France 3', 'Télévision', 'Centre', 'Inconnu', 'https://www.france.tv/france-3/'),
    ('France 5', 'Télévision', 'Centre', 'Inconnu', 'https://www.france.tv/france-5/'),
    ('France Culture', 'Radio', 'Centre gauche', 'Inconnu', 'https://www.radiofrance.fr/franceculture'),
    ('France Inter', 'Radio', 'Centre gauche', 'Inconnu', 'https://www.radiofrance.fr/franceinter'),
    ('franceinfo', 'Télévision', 'Centre', 'Inconnu', 'https://www.francetvinfo.fr'),
    ('Frandroid', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.frandroid.com'),
    ('FrenchWeb', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.frenchweb.fr'),
    ('Frontières', 'Presse écrite', 'Extrême droite', 'Inconnu', None),
    ('Frustration', 'Site web / pure player', 'Extrême gauche', 'Inconnu', 'https://www.frustrationmagazine.fr'),
    ('Génération Do It Yourself', 'Podcast', 'Inconnue', 'Inconnu', 'https://www.gdiy.fr'),
    ('HuffPost', 'Site web / pure player', 'Centre gauche', 'Inconnu', 'https://www.huffingtonpost.fr'),
    ('HugoDécrypte', 'YouTube', 'Inconnue', 'Inconnu', 'https://www.youtube.com/@HugoDecrypte'),
    ('ici (ex-France Bleu)', 'Radio', 'Centre', 'Inconnu', 'https://www.francebleu.fr'),
    ('Journal du Geek', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.journaldugeek.com'),
    ('Journal du Net', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.journaldunet.com'),
    ('Konbini', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.konbini.com'),
    ('Korben', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://korben.info'),
    ("L'ADN", 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.ladn.eu'),
    ("L'Alsace", 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lalsace.fr'),
    ("L'Est Républicain", 'Presse écrite', 'Centre', 'Inconnu', 'https://www.estrepublicain.fr'),
    ("L'Express", 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.lexpress.fr'),
    ("L'Humanité", 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.humanite.fr'),
    ("L'Indépendant", 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lindependant.fr'),
    ("L'Informé", 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.linforme.com'),
    ("L'Obs", 'Presse écrite', 'Centre gauche', 'Inconnu', 'https://www.nouvelobs.com'),
    ("L'Opinion", 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.lopinion.fr'),
    ("L'Union", 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lunion.fr'),
    ("L'Usine Digitale", 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.usine-digitale.fr'),
    ("L'Usine Nouvelle", 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.usinenouvelle.com'),
    ('La Croix', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.la-croix.com'),
    ('La Dépêche du Midi', 'Presse écrite', 'Centre gauche', 'Inconnu', 'https://www.ladepeche.fr'),
    ('La Marseillaise', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.lamarseillaise.fr'),
    ('La Montagne', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lamontagne.fr'),
    ('La Nouvelle République', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lanouvellerepublique.fr'),
    ('La Provence', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.laprovence.com'),
    ('La Tribune', 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.latribune.fr'),
    ('La Voix du Nord', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lavoixdunord.fr'),
    ('LCI', 'Télévision', 'Centre droit', 'Inconnu', 'https://www.tf1info.fr'),
    ('LCP', 'Télévision', 'Centre', 'Inconnu', 'https://lcp.fr'),
    ('Le 1', 'Presse écrite', 'Centre gauche', 'Inconnu', 'https://le1hebdo.fr'),
    ('Le Bien public', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.bienpublic.com'),
    ('Le Big Data', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.lebigdata.fr'),
    ('Le Canard enchaîné', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.lecanardenchaine.fr'),
    ('Le Code a changé', 'Podcast', 'Inconnue', 'Inconnu', 'https://www.radiofrance.fr/franceinter/podcasts/le-code-a-change'),
    ('Le Courrier picard', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.courrier-picard.fr'),
    ('Le Dauphiné libéré', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.ledauphine.com'),
    ('Le Figaro', 'Presse écrite', 'Droite', 'Inconnu', 'https://www.lefigaro.fr'),
    ('Le Grand Continent', 'Site web / pure player', 'Centre', 'Inconnu', 'https://legrandcontinent.eu'),
    ('Le Journal de Saône-et-Loire', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lejsl.com'),
    ('Le Journal du Dimanche', 'Presse écrite', 'Extrême droite', 'Inconnu', 'https://www.lejdd.fr'),
    ('Le Monde', 'Presse écrite', 'Centre gauche', 'Inconnu', 'https://www.lemonde.fr'),
    ('Le Monde diplomatique', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.monde-diplomatique.fr'),
    ('Le Média', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://www.lemediatv.fr'),
    ('Le Parisien', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.leparisien.fr'),
    ('Le Point', 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.lepoint.fr'),
    ('Le Populaire du Centre', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.lepopulaire.fr'),
    ('Le Progrès', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.leprogres.fr'),
    ('Le Précepteur', 'YouTube', 'Inconnue', 'Inconnu', None),
    ('Le Républicain lorrain', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.republicain-lorrain.fr'),
    ('Le Télégramme', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.letelegramme.fr'),
    ('Legend', 'YouTube', 'Inconnue', 'Inconnu', None),
    ('LeMagIT', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.lemagit.fr'),
    ('Les Inrockuptibles', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.lesinrocks.com'),
    ('Les Jours', 'Site web / pure player', 'Centre gauche', 'Inconnu', 'https://lesjours.fr'),
    ('Les Numériques', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.lesnumeriques.com'),
    ('Les Échos', 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.lesechos.fr'),
    ('Libération', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.liberation.fr'),
    ('Loopsider', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.loopsider.com'),
    ('Louie Media', 'Podcast', 'Inconnue', 'Inconnu', 'https://www.louiemedia.com'),
    ('Lundi matin', 'Site web / pure player', 'Extrême gauche', 'Inconnu', 'https://lundi.am'),
    ('M6', 'Télévision', 'Centre', 'Inconnu', 'https://www.m6.fr'),
    ('Maddyness', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.maddyness.com'),
    ('Mediapart', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://www.mediapart.fr'),
    ('Micode', 'YouTube', 'Inconnue', 'Inconnu', 'https://www.youtube.com/@Micode'),
    ('Midi Libre', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.midilibre.fr'),
    ('Monde Numérique', 'Podcast', 'Inconnue', 'Inconnu', 'https://mondenumerique.info'),
    ('Monsieur Phi', 'YouTube', 'Inconnue', 'Inconnu', 'https://www.youtube.com/@MonsieurPhi'),
    ('Next', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://next.ink'),
    ('Nice-Matin', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.nicematin.com'),
    ('Numerama', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.numerama.com'),
    ('Off Investigation', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://www.off-investigation.fr'),
    ('Ouest-France', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.ouest-france.fr'),
    ('Paris Match', 'Presse écrite', 'Centre droit', 'Inconnu', 'https://www.parismatch.com'),
    ('Paris-Normandie', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.paris-normandie.fr'),
    ('Philosophie magazine', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.philomag.com'),
    ('Politico Europe', 'Site web / pure player', 'Centre', 'Inconnu', 'https://www.politico.eu'),
    ('Politis', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.politis.fr'),
    ('Pour la Science', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.pourlascience.fr'),
    ('Presse-citron', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.presse-citron.net'),
    ('Public Sénat', 'Télévision', 'Centre', 'Inconnu', 'https://www.publicsenat.fr'),
    ('Radio Classique', 'Radio', 'Centre droit', 'Inconnu', 'https://www.radioclassique.fr'),
    ('Radio Nova', 'Radio', 'Inconnue', 'Inconnu', 'https://www.nova.fr'),
    ('Reporterre', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://reporterre.net'),
    ('Reuters (bureau de Paris)', 'Agence de presse', 'Centre', 'Inconnu', 'https://www.reuters.com'),
    ('RFI', 'Radio', 'Centre', 'Inconnu', 'https://www.rfi.fr'),
    ('RMC', 'Radio', 'Centre', 'Inconnu', 'https://rmc.bfmtv.com'),
    ('RTL', 'Radio', 'Centre', 'Inconnu', 'https://www.rtl.fr'),
    ('Révolution permanente', 'Site web / pure player', 'Extrême gauche', 'Inconnu', 'https://www.revolutionpermanente.fr'),
    ('Science & Vie', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.science-et-vie.com'),
    ('Science4All', 'YouTube', 'Inconnue', 'Inconnu', None),
    ('ScienceEtonnante', 'YouTube', 'Inconnue', 'Inconnu', 'https://www.youtube.com/@ScienceEtonnante'),
    ('Sciences et Avenir', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.sciencesetavenir.fr'),
    ('Silicon.fr', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.silicon.fr'),
    ('Siècle Digital', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://siecledigital.fr'),
    ('Slate', 'Site web / pure player', 'Centre gauche', 'Inconnu', 'https://www.slate.fr'),
    ('Socialter', 'Presse écrite', 'Gauche', 'Inconnu', 'https://www.socialter.fr'),
    ('Society', 'Presse écrite', 'Inconnue', 'Inconnu', None),
    ('StreetPress', 'Site web / pure player', 'Gauche', 'Inconnu', 'https://www.streetpress.com'),
    ('Sud Ouest', 'Presse écrite', 'Centre', 'Inconnu', 'https://www.sudouest.fr'),
    ('Sud Radio', 'Radio', 'Droite', 'Inconnu', 'https://www.sudradio.fr'),
    ('Tech Café', 'Podcast', 'Inconnue', 'Inconnu', 'https://techcafe.fr'),
    ('TF1', 'Télévision', 'Centre', 'Inconnu', 'https://www.tf1info.fr'),
    ('The Conversation France', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://theconversation.com/fr'),
    ('Thinkerview', 'YouTube', 'Inconnue', 'Inconnu', 'https://www.youtube.com/@thinkerview'),
    ('TMC', 'Télévision', 'Centre', 'Inconnu', 'https://www.tf1.fr/tmc'),
    ('Trench Tech', 'Podcast', 'Inconnue', 'Inconnu', None),
    ('TV5Monde', 'Télévision', 'Centre', 'Inconnu', 'https://www.tv5monde.com'),
    ('Télérama', 'Presse écrite', 'Centre gauche', 'Inconnu', 'https://www.telerama.fr'),
    ('Underscore_', 'YouTube', 'Inconnue', 'Inconnu', 'https://www.youtube.com/@Underscore_'),
    ('Usbek & Rica', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://usbeketrica.com'),
    ('Valeurs actuelles', 'Presse écrite', 'Extrême droite', 'Inconnu', 'https://www.valeursactuelles.com'),
    ('Var-Matin', 'Presse écrite', 'Inconnue', 'Inconnu', 'https://www.varmatin.com'),
    ('ZDNet France', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://www.zdnet.fr'),
    ('Élucid', 'Site web / pure player', 'Inconnue', 'Inconnu', 'https://elucid.media'),
]

# (name, role, stance, notes, [médias they write for])
JOURNALISTES = [
    ('Adrien Gindre', 'Chef·fe de rubrique / Chef·fe de service, Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['LCI', 'TF1']),
    ('Alba Ventura', 'Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['RTL']),
    ('Alexandre Piquard', 'Journaliste spécialisé·e', 'Inconnu', 'Tech, médias, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Alexis Brézet', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Figaro']),
    ('Alice Vitard', 'Journaliste spécialisé·e', 'Inconnu', 'IA, cybersécurité.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Usine Digitale"]),
    ('Amaelle Guiton', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique, cybersécurité.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Libération']),
    ('Amandine Bégot', 'Présentateur·ice', 'Inconnu', 'Matinale.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['RTL']),
    ('Anis Ayari', 'Youtubeur·euse', 'Inconnu', 'IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Defend Intelligence']),
    ('Anne Cagan', 'Journaliste spécialisé·e', 'Inconnu', 'Tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Express"]),
    ('Anne Rosencher', 'Directeur·ice de la rédaction', 'Inconnu', 'Directrice déléguée.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Express"]),
    ('Anne-Claire Coudray', 'Présentateur·ice', 'Inconnu', 'Journal télévisé.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['TF1']),
    ('Anne-Élisabeth Lemoine', 'Présentateur·ice', 'Inconnu', 'C à vous.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 5']),
    ('Anthony Morel', 'Chroniqueur·euse', 'Inconnu', 'Chroniques tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['BFMTV']),
    ('Apolline de Malherbe', 'Présentateur·ice', 'Inconnu', 'Interview politique matinale.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['BFMTV', 'RMC']),
    ('Ariane Chemin', 'Grand·e reporter', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Arnaud Leparmentier', 'Correspondant·e', 'Inconnu', 'Correspondant à New York — économie, tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Asma Mhalla', 'Expert·e invité·e, Chroniqueur·euse', 'Inconnu', 'Géopolitique de la tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Inter']),
    ('Aurélie Jean', 'Expert·e invité·e, Chroniqueur·euse', 'Inconnu', 'Scientifique numéricienne, chroniqueuse.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Point']),
    ('Benjamin Duhamel', 'Présentateur·ice, Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['BFMTV', 'France Inter']),
    ('Benjamin Polge', 'Journaliste spécialisé·e', 'Inconnu', 'IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Journal du Net']),
    ('Benoît Bréville', 'Directeur·ice de publication', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde diplomatique']),
    ('Carine Fouteau', 'Directeur·ice de publication', 'Inconnu', 'Présidente.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Mediapart']),
    ('Caroline Roux', 'Présentateur·ice', 'Inconnu', "C dans l'air, Les 4 vérités.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.",
     ['France 2', 'France 5']),
    ('Chloé Woitier', 'Journaliste spécialisé·e', 'Inconnu', 'Tech, médias.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Figaro']),
    ('Christine Kelly', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['CNews']),
    ('Claire Legros', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique et société.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Corine Lesnes', 'Correspondant·e', 'Inconnu', 'Correspondante à San Francisco.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Damien Leloup', 'Journaliste spécialisé·e', 'Inconnu', 'Pixels — numérique, plateformes.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Forbes France', 'Le Monde']),
    ('Daniel Schneidermann', 'Directeur·ice de publication', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Arrêt sur images']),
    ('Darius Rochebin', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['LCI']),
    ('David Larousserie', 'Journaliste spécialisé·e', 'Inconnu', 'Sciences — recherche, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('David Louapre', 'Youtubeur·euse', 'Inconnu', 'Vulgarisation scientifique, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['ScienceEtonnante']),
    ('David Pujadas', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['LCI']),
    ('Delphine Sabattier', 'Présentateur·ice, Journaliste spécialisé·e', 'Inconnu', 'Émissions tech (Smart Tech) — média actuel à vérifier.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     []),
    ('Denis Robert', 'Directeur·ice de la rédaction', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Blast']),
    ('Dimitri Pavlenko', 'Présentateur·ice', 'Inconnu', 'Matinale.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Europe 1']),
    ('Dominique Seux', 'Éditorialiste, Chroniqueur·euse', 'Inconnu', 'Économie.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Inter', 'Les Échos']),
    ('Dov Alfon', 'Directeur·ice de publication', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Libération']),
    ('Edwy Plenel', 'Éditorialiste', 'Inconnu', 'Cofondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Mediapart']),
    ('Ellen Salvi', 'Journaliste spécialisé·e', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Mediapart']),
    ('Elsa Bembaron', 'Journaliste spécialisé·e', 'Inconnu', 'Tech, télécoms.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Figaro']),
    ('Eugénie Bastié', 'Journaliste généraliste, Éditorialiste', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['CNews', 'Le Figaro']),
    ('Fabrice Arfi', 'Chef·fe de rubrique / Chef·fe de service', 'Inconnu', 'Enquêtes.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Mediapart']),
    ('Fabrice Lhomme', 'Grand·e reporter', 'Inconnu', 'Enquêtes.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Florian Dèbes', 'Journaliste spécialisé·e', 'Inconnu', 'Tech, télécoms.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Florian Reynaud', 'Journaliste spécialisé·e', 'Inconnu', 'Pixels — numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Franz-Olivier Giesbert', 'Éditorialiste', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Point']),
    ('Françoise Fressoz', 'Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Frédéric Simottel', 'Présentateur·ice', 'Inconnu', 'Tech&Co.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['BFM Business']),
    ('Gaspard Koenig', 'Expert·e invité·e, Chroniqueur·euse', 'Inconnu', 'Philosophe, chroniqueur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Geoffroy Lejeune', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Journal du Dimanche']),
    ('Gilles Bornstein', 'Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['franceinfo']),
    ('Gilles Bouleau', 'Présentateur·ice', 'Inconnu', 'Journal télévisé.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['TF1']),
    ('Gilles Gressani', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Grand Continent']),
    ('Guillaume Erner', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Culture']),
    ('Guillaume Grallet', 'Journaliste spécialisé·e', 'Inconnu', 'Tech et IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Point']),
    ('Guillaume Pley', 'Youtubeur·euse, Présentateur·ice', 'Inconnu', 'Interviews.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Legend']),
    ('Guillaume Tabard', 'Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Figaro']),
    ('Gurvan Kristanadjaja', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Libération']),
    ('Gérard Davet', 'Grand·e reporter', 'Inconnu', 'Enquêtes.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Hervé Kempf', 'Rédacteur·ice en chef', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Reporterre']),
    ('Hubert Guillaud', 'Journaliste spécialisé·e', 'Inconnu', 'Algorithmes, IA et société.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Dans les algorithmes']),
    ('Hugo Travers', 'Youtubeur·euse, Présentateur·ice', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['HugoDécrypte']),
    ('Ingrid Vergara', 'Journaliste spécialisé·e', 'Inconnu', 'Tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Figaro']),
    ('Jean-Jacques Bourdin', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Sud Radio']),
    ('Jean-Marc Manach', 'Journaliste spécialisé·e', 'Inconnu', 'Surveillance, numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Next']),
    ('Julien Lausson', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Numerama']),
    ('Jérôme Colombain', 'Présentateur·ice, Journaliste spécialisé·e', 'Inconnu', 'Podcast tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Monde Numérique']),
    ('Jérôme Fenoglio', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Jérôme Hourdeaux', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique, libertés.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Mediapart']),
    ('Kamel Daoud', 'Chroniqueur·euse', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Point']),
    ('Karim Rissouli', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 5']),
    ('Laurence Ferrari', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['CNews']),
    ('Laurent Alexandre', 'Expert·e invité·e, Chroniqueur·euse', 'Inconnu', 'Essayiste IA, chroniqueur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Express"]),
    ('Laurent Delahousse', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 2']),
    ('Leïla Marchand', 'Journaliste spécialisé·e', 'Inconnu', 'IA, tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Louis de Raguenel', 'Chef·fe de rubrique / Chef·fe de service', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['CNews', 'Europe 1']),
    ('Léa Salamé', 'Présentateur·ice', 'Inconnu', 'Journal de 20h.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 2', 'Sud Radio']),
    ('Lénaïg Bredoux', 'Rédacteur·ice en chef', 'Inconnu', 'Codirectrice éditoriale.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Mediapart']),
    ('Lê Nguyên Hoang', 'Youtubeur·euse, Expert·e invité·e', 'Inconnu', 'Sécurité et éthique des algorithmes.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Science4All']),
    ('Manuel Dorne (Korben)', 'Journaliste spécialisé·e', 'Inconnu', 'Blog tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Korben']),
    ('Marie-Sophie Lacarrau', 'Présentateur·ice', 'Inconnu', 'Journal de 13h.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['TF1']),
    ('Marina Alcaraz', 'Journaliste spécialisé·e', 'Inconnu', 'Médias, tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Martin Clavey', 'Journaliste spécialisé·e', 'Inconnu', 'Sciences, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Next']),
    ('Martin Untersinger', 'Journaliste spécialisé·e', 'Inconnu', 'Cybersécurité, numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Mathieu Vidard', 'Présentateur·ice', 'Inconnu', 'Sciences — La Terre au carré.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Inter']),
    ('Mathilde Saliou', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique ; autrice de « Technoféminisme ».\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Next']),
    ('Matthieu Stefani', 'Présentateur·ice', 'Inconnu', 'Podcast entrepreneuriat/tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Génération Do It Yourself']),
    ('Micode (Michaël de Marliave)', 'Youtubeur·euse, Présentateur·ice', 'Inconnu', 'Tech, cybersécurité.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Micode', 'Underscore_']),
    ('Morgane Tual', 'Journaliste spécialisé·e', 'Inconnu', 'Pixels — tech, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Mouloud Achour', 'Présentateur·ice', 'Inconnu', 'Clique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Canal+', 'Clique']),
    ('Nastasia Hadjadji', 'Pigiste, Journaliste spécialisé·e', 'Inconnu', 'Tech ; autrice de « Apocalypse Nerds ».\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     []),
    ('Nathalie Saint-Cricq', 'Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 2']),
    ('Nicolas Beytout', 'Directeur·ice de publication', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Opinion"]),
    ('Nicolas Celnik', 'Journaliste spécialisé·e', 'Inconnu', 'Tech, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Libération']),
    ('Nicolas Demorand', 'Présentateur·ice', 'Inconnu', 'Matinale.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Inter']),
    ('Nicolas Lellouche', 'Rédacteur·ice en chef', 'Inconnu', 'Tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Numerama']),
    ('Nicolas Rauline', 'Correspondant·e', 'Inconnu', 'États-Unis — tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Olivier Berruyer', 'Directeur·ice de publication', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Élucid']),
    ('Olivier Tesquet', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique, surveillance.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Télérama']),
    ('Pascal Praud', 'Présentateur·ice', 'Inconnu', "L'Heure des pros.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.",
     ['CNews', 'Europe 1']),
    ('Patrick Cohen', 'Éditorialiste, Chroniqueur·euse', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 5', 'France Inter']),
    ('Paul Quinio', 'Directeur·ice de la rédaction', 'Inconnu', 'Directeur délégué.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Libération']),
    ('Philippe Mabille', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['La Tribune']),
    ('Raphaël Balenieri', 'Journaliste spécialisé·e', 'Inconnu', 'Tech, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Riss (Laurent Sourisseau)', 'Directeur·ice de publication', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Charlie Hebdo']),
    ('Ruth Elkrief', 'Présentateur·ice, Éditorialiste', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['LCI']),
    ('Rémi Godeau', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Opinion"]),
    ('Rémy Buisine', 'Journaliste / Reporter', 'Inconnu', 'Reportages en direct.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Brut']),
    ('Samuel Étienne', 'Présentateur·ice, Youtubeur·euse', 'Inconnu', 'Revue de presse sur Twitch.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 3']),
    ('Solenn de Royer', 'Grand·e reporter', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Sonia Devillers', 'Présentateur·ice', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Inter']),
    ('Stéphane Foucart', 'Journaliste spécialisé·e', 'Inconnu', 'Sciences, environnement.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Sylvain Rolland', 'Rédacteur·ice en chef', 'Inconnu', 'Tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['La Tribune']),
    ('Sébastien Dumoulin', 'Chef·fe de rubrique / Chef·fe de service', 'Inconnu', 'Tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Les Échos']),
    ('Sébastien Gavois', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Next']),
    ('Thibault Prévost', 'Chroniqueur·euse, Journaliste spécialisé·e', 'Inconnu', "Auteur de « Les prophètes de l'IA ».\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.",
     ['Arrêt sur images']),
    ('Thibaut Giraud', 'Youtubeur·euse', 'Inconnu', 'Philosophie, IA.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Monsieur Phi']),
    ('Thierry Noisette', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique, logiciel libre.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Obs"]),
    ('Thomas Legrand', 'Chroniqueur·euse, Éditorialiste', 'Inconnu', 'Politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Libération']),
    ('Thomas Mahler', 'Journaliste spécialisé·e', 'Inconnu', 'Idées, sciences.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Point']),
    ('Thomas Snégaroff', 'Présentateur·ice', 'Inconnu', 'C politique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 5']),
    ('Tristan Waleckx', 'Présentateur·ice', 'Inconnu', "Complément d'enquête.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.",
     ['France 2']),
    ('Ulrich Rozier', 'Directeur·ice de publication', 'Inconnu', 'Fondateur.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Frandroid']),
    ('Vincent Fagot', 'Journaliste spécialisé·e', 'Inconnu', 'Économie — télécoms, tech.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Monde']),
    ('Vincent Hermann', 'Journaliste spécialisé·e', 'Inconnu', 'Numérique.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Next']),
    ('Vincent Trémolet de Villers', 'Rédacteur·ice en chef adjoint·e, Éditorialiste', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Figaro']),
    ('Xavier de La Porte', 'Chroniqueur·euse, Présentateur·ice', 'Inconnu', 'Numérique et société.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France Inter', 'Le Code a changé']),
    ('Yann Barthès', 'Présentateur·ice', 'Inconnu', 'Quotidien.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['TMC']),
    ('Élise Lucet', 'Présentateur·ice, Journaliste / Reporter', 'Inconnu', 'Cash Investigation, Envoyé spécial.\nPré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['France 2']),
    ('Élizabeth Martichoux', 'Présentateur·ice, Éditorialiste', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['LCI']),
    ('Éric Chol', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ["L'Express"]),
    ('Étienne Gernelle', 'Directeur·ice de la rédaction', 'Inconnu', 'Pré-rempli le 11/09/2026 (connaissances publiques) — média et fonction à vérifier.',
     ['Le Point']),
]

# The interventions recorded in the old CRM. Organisation and people are named,
# not numbered, so they resolve against whatever ids this database uses.
INTERVENTIONS = [
    {
        'organisation': 'Sud Radio',
        'date': '2026-09-11',
        'type': 'Interview',
        'link': 'https://tqtfrere',
        'summary': None,
        'personnes': ['Léa Salamé'],
        'pauseia': ['Hugo'],
    },
    {
        'organisation': 'Forbes France',
        'date': '2026-09-16',
        'type': 'Plateau TV',
        'link': 'https://tqt',
        'summary': None,
        'personnes': ['Damien Leloup'],
        'pauseia': ['Hugo'],
    },
]

def key(name):
    """Comparison key for a name: casefolded, whitespace-collapsed.

    Python's casefold rather than SQL's COLLATE NOCASE, which is ASCII-only in
    SQLite and would miss the accented names that make up most of this data.
    """
    return " ".join((name or "").split()).casefold()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def moderator_id(db, name):
    """The utilisateurice id for `name`, created if they don't exist yet."""
    for mid, existing in db.execute("SELECT id, name FROM moderators"):
        if key(existing) == key(name):
            return mid
    return db.execute("INSERT INTO moderators (name) VALUES (?)", (name,)).lastrowid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it, only report")
    parser.add_argument("--added-by", default=DEFAULT_ADDED_BY,
                        help=f"utilisateurice recorded as having added the rows "
                             f"(default: {DEFAULT_ADDED_BY})")
    args = parser.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"Base introuvable : {DB}")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    # The table this seeds only exists once the app has created the schema.
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='organisations'"
    ).fetchone():
        sys.exit("La table `organisations` n'existe pas encore : démarrez l'app "
                 "une fois pour créer le schéma, puis relancez ce script.")

    who = moderator_id(db, args.added_by)
    stamp = now()
    report = []

    # --- Médias ----------------------------------------------------------- #
    media_ids = {}
    existing = {
        key(r["name"]): r["id"] for r in db.execute(
            "SELECT id, name FROM organisations WHERE org_type = 'Média'")
    }
    added = 0
    for name, media_type, orientation, stance, link in MEDIAS:
        k = key(name)
        if k in existing:
            media_ids[name] = existing[k]
            continue
        media_ids[name] = existing[k] = db.execute(
            """
            INSERT INTO organisations (name, org_type, media_type, orientation,
                stance, link, notes, added_by, validated_by, created_at)
            VALUES (?, 'Média', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, media_type, orientation, stance, link, MEDIA_NOTE,
             who, who, stamp),
        ).lastrowid
        added += 1
    report.append(f"médias ajoutés            : {added} (sur {len(MEDIAS)})")

    # --- Journalistes ------------------------------------------------------ #
    # Matched among journalistes only, so a name shared with an élu·e stays two
    # separate fiches — see the module docstring.
    existing = {
        key(r["name"]): r["id"] for r in db.execute(
            "SELECT id, name FROM persons WHERE contact_type = 'Journaliste'")
    }
    person_ids, added, links = {}, 0, 0
    for name, role, stance, notes, medias in JOURNALISTES:
        k = key(name)
        if k in existing:
            person_ids[name] = existing[k]
        else:
            person_ids[name] = existing[k] = db.execute(
                """
                INSERT INTO persons (name, contact_type, role, stance, notes,
                    added_by, validated_by, created_at)
                VALUES (?, 'Journaliste', ?, ?, ?, ?, ?, ?)
                """,
                (name, role, stance, notes, who, who, stamp),
            ).lastrowid
            added += 1
        for media in medias:
            oid = media_ids.get(media)
            if oid is None:
                continue
            db.execute(
                "INSERT OR IGNORE INTO person_organisations "
                "(person_id, organisation_id) VALUES (?, ?)",
                (person_ids[name], oid),
            )
            links += 1
    report.append(f"journalistes ajoutés      : {added} (sur {len(JOURNALISTES)})")
    report.append(f"liens journaliste ↔ média : {links}")

    # --- Interventions ----------------------------------------------------- #
    added = 0
    for entry in INTERVENTIONS:
        oid = media_ids.get(entry["organisation"])
        if oid is None:
            report.append(f"  ! média inconnu, intervention ignorée : "
                          f"{entry['organisation']}")
            continue
        # Same link on the same date is the same intervention: a rerun skips it.
        if db.execute(
            "SELECT 1 FROM interventions WHERE link = ? AND intervention_date = ?",
            (entry["link"], entry["date"]),
        ).fetchone():
            continue
        rec_id = db.execute(
            """
            INSERT INTO interventions (organisation_id, intervention_date,
                intervention_type, link, summary, recorded_by, validated_by,
                created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (oid, entry["date"], entry["type"], entry["link"], entry["summary"],
             who, who, stamp),
        ).lastrowid
        for person in entry["personnes"]:
            pid = person_ids.get(person)
            if pid:
                db.execute(
                    "INSERT OR IGNORE INTO intervention_persons "
                    "(intervention_id, person_id) VALUES (?, ?)", (rec_id, pid))
        for person in entry["pauseia"]:
            db.execute(
                "INSERT OR IGNORE INTO intervention_moderators "
                "(intervention_id, moderator_id) VALUES (?, ?)",
                (rec_id, moderator_id(db, person)))
        added += 1
    report.append(f"interventions ajoutées    : {added} (sur {len(INTERVENTIONS)})")

    # A journaliste has no groupe politique; the mirror column must stay NULL.
    stray = db.execute(
        "SELECT COUNT(*) FROM persons WHERE contact_type = 'Journaliste' "
        "AND political_group IS NOT NULL"
    ).fetchone()[0]
    report.append(f"journalistes avec un groupe politique résiduel : {stray}")

    print("\n".join(report))
    totals = db.execute(
        "SELECT (SELECT COUNT(*) FROM organisations WHERE org_type='Média') m, "
        "(SELECT COUNT(*) FROM persons WHERE contact_type='Journaliste') j"
    ).fetchone()
    print(f"\nla base contient maintenant : {totals['m']} médias, "
          f"{totals['j']} journalistes")
    if args.commit:
        db.commit()
        print("→ écrit dans", DB)
    else:
        db.rollback()
        print("→ simulation (relancer avec --commit pour écrire)")
    db.close()


if __name__ == "__main__":
    main()
