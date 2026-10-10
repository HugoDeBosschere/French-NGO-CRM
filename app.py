"""Website Meeting — a simple, ergonomic app for logging meetings and mails.

PauseIA members record the people they meet (politicians / officials), the
meetings they have with them and the mails they exchange. A person has a name,
a political group, a stance on PauseAI, when they were first contacted and an
optional follow-up date (when to send them a new mail / "relance"). A meeting
has a date, a short summary and an optional attached full report. A mail has a
date, a direction (sent / received), an importance flag, a short summary and an
optional attached document. Persons link many-to-many to both meetings and
mails: a meeting/mail involves one or more persons, and a person may appear in
zero or more meetings and mails.

Two lists sit on /todo beside the rencontres: the relances that have come due,
each naming the utilisateurice who wrote to that person last (« quiconque »
when nobody has), and « À contacter », a hand-built list of people to write to,
ticked off the same way. An intervention and a contenu each carry an optional
« Genre » (see GENRES), which narrows the people their form offers.

Run with:  uv run flask --app app run --debug
"""

import calendar as pycalendar
import contextlib
import email
import fcntl
import math
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import magic
from flask import (
    Flask,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
# Overridable so tests (and a throwaway instance) can point somewhere else; the
# utils/ scripts already take IMAP_DB_PATH the same way. Unset, it is the file
# next to app.py — which is /app/meetings.db inside the container.
DB_PATH = Path(os.environ.get("CRM_DB_PATH") or (BASE_DIR / "meetings.db"))
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".odt", ".txt"}
# MIME types (detected from file content via libmagic) accepted per extension.
# docx/odt are zip containers; libmagic may only see "application/zip" on
# some variants, which still rules out executables and other disguised types.
ALLOWED_MIMES = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    ".odt": {"application/vnd.oasis.opendocument.text", "application/zip"},
    ".txt": None,  # any text/* — checked by prefix below
}
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB per upload

# Shared password gating access to the whole site. Override in production with
# the APP_PASSWORD environment variable.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "pauseia")
# Secret key for signing the session cookie. Override in production.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Political groups offered in the dropdown, grouped by institution.
# Edit these lists freely — they are the single source of truth for the form.
POLITICAL_GROUPS = {
    "Assemblée nationale": [
        "Rassemblement National (RN)",
        "Ensemble pour la République (EPR)",
        "La France Insoumise (LFI-NFP)",
        "Socialistes et apparentés",
        "Droite Républicaine (DR)",
        "Écologiste et Social",
        "Les Démocrates (MoDem)",
        "Horizons & Indépendants",
        "Gauche Démocrate et Républicaine (GDR)",
        "Libertés, Indépendants, Outre-mer et Territoires (LIOT)",
        "Union des droites pour la République (UDR)",
        "Non-inscrit",
        "Sans groupe déclaré (Assemblée nationale)",
    ],
    "Sénat": [
        "Les Républicains (Sénat)",
        "Socialiste, Écologiste et Républicain (Sénat)",
        "Union Centriste (Sénat)",
        "Rassemblement des démocrates, progressistes et indépendants (RDPI)",
        "Communiste, Républicain, Citoyen et Écologiste (CRCE-K)",
        "Les Indépendants – République et Territoires",
        "Écologiste – Solidarité et Territoires (Sénat)",
        "Rassemblement Démocratique et Social Européen (RDSE)",
        "Non-inscrit (Sénat)",
        "Sans groupe déclaré (Sénat)",
    ],
    "Parlement européen": [
        "Parti populaire européen (PPE)",
        "Alliance progressiste des Socialistes et Démocrates (S&D)",
        "Renew Europe",
        "Verts/ALE",
        "Conservateurs et Réformistes européens (CRE)",
        "The Left (GUE/NGL)",
        "Patriotes pour l'Europe",
        "Europe des Nations Souveraines (ESN)",
        "Non-inscrit (Parlement européen)",
        "Sans groupe déclaré (Parlement européen)",
    ],
    "Autre": [
        "Gouvernement / Administration",
        "Collectivité territoriale",
        "Autre / Non applicable",
    ],
}

# --------------------------------------------------------------------------- #
# Type de contact
# --------------------------------------------------------------------------- #
# Every person in the CRM is one of these. The value drives three things: which
# functions the form offers (ROLES_BY_CONTACT_TYPE), which kind of organisation
# they can belong to (ORG_TYPE_BY_CONTACT_TYPE), and which extra mandate fields
# are shown. Adding a fourth type means one entry here plus its role list and
# its organisation type — that is exactly how « Religieux·se » was added.
CONTACT_TYPES = [
    "Journaliste",
    "Politique",
    "Religieux·se",
    "Membre d'une ONG",
    "Membre d'une entreprise",
    "Autre",
]

# Organisations come in the same flavours, one per contact type: a journalist
# works for a média, a politician sits in a groupe politique, a religious figure
# belongs to a culte (a church, a diocèse, a consistoire, a mosque…).
ORG_TYPES = ["Média", "Groupe politique", "Culte", "ONG", "Entreprise", "Autre"]

ORG_TYPE_BY_CONTACT_TYPE = {
    "Journaliste": "Média",
    "Politique": "Groupe politique",
    "Religieux·se": "Culte",
    "Membre d'une ONG": "ONG",
    "Membre d'une entreprise": "Entreprise",
    "Autre": "Autre",
}
CONTACT_TYPE_BY_ORG_TYPE = {v: k for k, v in ORG_TYPE_BY_CONTACT_TYPE.items()}

# ONG, Entreprise and Autre are the organisation types that belong to no type
# de contact. Anybody can be part of one — a journaliste sits on an NGO board,
# an élu·e chairs an association — so they are offered to every type de contact
# on top of that type's own organisation, and CONTACT_TYPE_BY_ORG_TYPE knows
# nothing about them: which is exactly what stops _link_persons_to_organisation
# from concluding that someone quoted by an ONG works for it.
#
# They also carry no type-specific field. Name, position sur PauseIA, lien and
# notes are all they have, so the organisation form renders no block for them.
NEUTRAL_ORG_TYPES = ["ONG", "Entreprise", "Autre"]


def allowed_org_types(contact_type):
    """The organisation types someone of this type de contact can belong to."""
    own = ORG_TYPE_BY_CONTACT_TYPE.get(contact_type)
    return ([own] if own else []) + NEUTRAL_ORG_TYPES


# --------------------------------------------------------------------------- #
# Genre d'une intervention / d'un contenu
# --------------------------------------------------------------------------- #
# Optional on both. It says what kind of exchange this was, and its only effect
# is to narrow the people the form offers: choosing « Politique » stops a list
# of several hundred people from proposing every journaliste in the base.
#
# A person's genres follow from what they already are — their type de contact,
# plus « Autre » for anyone attached to an ONG or an entreprise — so nothing
# new has to be filled in on a fiche for the filter to work. See person_genres.
GENRES = ["Politique", "Journalistique", "Religieux", "Autre"]

GENRE_BY_CONTACT_TYPE = {
    "Journaliste": "Journalistique",
    "Politique": "Politique",
    "Religieux·se": "Religieux",
    "Membre d'une ONG": "Autre",
    "Membre d'une entreprise": "Autre",
    "Autre": "Autre",
}

GENRE_BY_ORG_TYPE = {
    "Média": "Journalistique",
    "Groupe politique": "Politique",
    "Culte": "Religieux",
    "ONG": "Autre",
    "Entreprise": "Autre",
    "Autre": "Autre",
}


def person_genres(contact_type, org_types):
    """The genres a person answers to, as a « | »-joined string for the form.

    Their type de contact, plus the genre of every organisation they belong to
    — which is what gives « Autre » to the journaliste who also sits on an NGO
    board, without taking « Journalistique » away from them.
    """
    seen = set()
    if contact_type in GENRE_BY_CONTACT_TYPE:
        seen.add(GENRE_BY_CONTACT_TYPE[contact_type])
    for t in (org_types or "").split("|"):
        if t in GENRE_BY_ORG_TYPE:
            seen.add(GENRE_BY_ORG_TYPE[t])
    return "|".join(g for g in GENRES if g in seen)

# The person's actual function(s)/role(s) — single source of truth for the form.
# A person can hold several at once (a minister is usually also a député·e), so
# `persons.role` stores them joined by ROLE_SEP. A legacy single value is simply
# a one-element list, which is why no data migration was needed.
#
# The list a form offers depends on the type de contact: « Rédacteur·ice en
# chef » is meaningless for a député·e and « Sénateur·ice » for a pigiste. ROLES
# below is the union of both, used where no type is known (the public
# declaration form); a save that knows the type validates against that type's
# list alone. See _roles_from_form.
POLITICAL_ROLES = [
    "Président·e de la République",
    "Premier·e ministre",
    "Ministre",
    "Ministre délégué·e",
    "Secrétaire d'État",
    "Membre d'un cabinet gouvernemental",
    "Secrétaire général·e",
    "Autre fonction gouvernementale",
    "Sénateur·ice",
    "Député·e",
    "Député·e européen·ne",
    "Maire·sse",
    "Conseiller·ère municipal·e",
    "Conseiller·ère départemental·e",
    "Conseiller·ère régional·e",
    "Collaborateur·ice",
    "Groupe de travail",
    "Personnalité publique",
]

# Functions offered when the type de contact is « Journaliste ».
JOURNALIST_ROLES = [
    "Journaliste / Reporter",
    "Journaliste généraliste",
    "Journaliste spécialisé·e",
    "Pigiste",
    "Correspondant·e",
    "Grand·e reporter",
    "Localier·ère",
    "Chef·fe de rubrique / Chef·fe de service",
    "Rédacteur·ice en chef adjoint·e",
    "Rédacteur·ice en chef",
    "Directeur·ice de la rédaction",
    "Directeur·ice de publication",
    "Secrétaire de rédaction (SR)",
    "Chef·fe d'édition",
    "Photojournaliste / Reporter-photographe",
    "Présentateur·ice",
    "Chroniqueur·euse",
    "Éditorialiste",
    "Expert·e invité·e",
    "Youtubeur·euse",
]

# --------------------------------------------------------------------------- #
# Religions and their functions
# --------------------------------------------------------------------------- #
# A religieux·se needs a second level of conditioning that the other two types
# do not: « Cardinal » makes no sense for an imam and « Rabbin » none for a
# pasteur·e, and the seven lists together run to some sixty labels — far too
# many for the single checklist Journaliste and Politique each get. So the
# person carries a `religion` (see RELIGIONS) and the form offers only that
# religion's functions, exactly the way the type de contact narrows the lists
# one level up. The mechanism is the same too: every block is rendered and all
# but one hidden, and the hidden ones are emptied so a stale box cannot post.
#
# Recognised cultes, in the order the select offers them. « Autre culte /
# Interreligieux » is the bucket for cross-culte bodies (the Conférence des
# responsables de culte en France, say) and for anything the list misses, so
# that nobody has to be filed under a religion that is not theirs.
RELIGIONS = [
    "Catholicisme",
    "Protestantisme",
    "Christianisme orthodoxe",
    "Judaïsme",
    "Islam",
    "Bouddhisme",
    "Autre culte / Interreligieux",
]

# Functions that exist in every culte, appended to each religion's own list.
# Aumônier·ère lives here rather than under Catholicisme: hospital, prison and
# army chaplains are appointed by all of them.
RELIGIOUS_COMMON_ROLES = [
    "Aumônier·ère",
    "Théologien·ne",
    "Porte-parole",
    "Responsable d'instance représentative",
    "Personnalité religieuse",
]

# Each culte's own functions, roughly in order of seniority. Kept a plain
# literal on purpose: utils/insert_*.py reads app.py's constants with
# ast.literal_eval (see app_constant there), so a comprehension or a `+` here
# would break the religious importers the way it once broke the élu·e ones.
#
# Labels are deliberately shared between religions — Archevêque, Évêque,
# Diacre, Prêtre and Moine are catholic *and* orthodox titles. That is safe
# because the religion disambiguates them; it only means ROLES has to be
# de-duplicated before it is used as a whitelist.
RELIGIOUS_ROLES_BY_RELIGION = {
    "Catholicisme": [
        "Pape",
        "Cardinal",
        "Nonce apostolique",
        "Archevêque",
        "Évêque",
        "Évêque auxiliaire",
        "Vicaire général / épiscopal",
        "Curé",
        "Prêtre",
        "Diacre",
        "Abbé·esse",
        "Supérieur·e",
        "Provincial·e",
        "Recteur·ice",
        "Moine",
        "Sœur / Religieuse",
        "Séminariste",
        "Président·e de la Conférence des évêques de France",
        "Secrétaire général·e de la Conférence des évêques de France",
    ],
    "Protestantisme": [
        "Pasteur·e",
        "Président·e de la Fédération protestante de France",
        "Secrétaire général·e de la Fédération protestante de France",
        "Président·e d'union d'Églises",
        "Ancien·ne / Presbytre",
        "Diacre",
        "Évangéliste",
        "Président·e du CNEF",
    ],
    "Christianisme orthodoxe": [
        "Patriarche",
        "Métropolite",
        "Archevêque",
        "Évêque",
        "Archimandrite",
        "Prêtre / Pope",
        "Diacre",
        "Higoumène",
        "Moine / Moniale",
        "Président·e de l'Assemblée des évêques orthodoxes de France",
    ],
    "Judaïsme": [
        "Grand Rabbin de France",
        "Grand Rabbin",
        "Rabbin",
        "Hazzan / Chantre",
        "Dayan",
        "Sofer",
        "Mohel",
        "Shohet",
        "Président·e du Consistoire",
        "Président·e du CRIF",
    ],
    "Islam": [
        "Grand Imam / Recteur·ice",
        "Imam",
        "Mufti",
        "Cheikh",
        "Cadi",
        "Ouléma",
        "Muezzin",
        "Président·e de fédération musulmane",
        "Membre du FORIF",
    ],
    "Bouddhisme": [
        "Moine / Bhikkhu",
        "Nonne / Bhikkhuni",
        "Lama",
        "Rinpoché",
        "Vénérable",
        "Président·e de l'Union bouddhiste de France",
    ],
    "Autre culte / Interreligieux": [
        "Responsable de culte",
        "Membre de la Conférence des responsables de culte en France (CRCF)",
        "Responsable interreligieux·se",
        "Autre fonction religieuse",
    ],
}


def _dedup(labels):
    """The labels, first occurrence kept, order preserved.

    Needed because a role label may belong to several religions (an Évêque is
    catholic or orthodox) while ROLES has to list each label exactly once: it
    is a whitelist, and `_roles_from_form` writes the column in its order.
    """
    seen, out = set(), []
    for label in labels:
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


# What the form offers for a given religion: that culte's functions, then the
# ones every culte shares.
ROLES_BY_RELIGION = {
    religion: roles + RELIGIOUS_COMMON_ROLES
    for religion, roles in RELIGIOUS_ROLES_BY_RELIGION.items()
}

# Every religious function, whatever the culte. Used when the religion is not
# known — a row imported before the field existed, or a fiche where nobody has
# filled it in yet — so that such a person's functions are still accepted.
RELIGIOUS_ROLES = _dedup(
    role for religion in RELIGIONS for role in ROLES_BY_RELIGION[religion]
)

# What someone does inside an ONG or an entreprise. An ONG employs people as
# well as it recruits volunteers, so the only difference between the two lists
# is that « Bénévole » has no meaning in a company.
NGO_COMPANY_COMMON_ROLES = [
    "Directeur·ice général·e",
    "Cadre",
    "Chercheur·euse",
    "Chargé·e de communication",
    "Chargé·e de relations",
    "Employé·e",
]
NGO_ROLES = NGO_COMPANY_COMMON_ROLES + ["Bénévole"]
COMPANY_ROLES = list(NGO_COMPANY_COMMON_ROLES)

# Holding one of these reveals the « Préciser » field: both labels say what
# somebody is rather than what they do, so the useful part is what comes next
# — « bénévole sur la campagne courriers », « employé·e au service juridique ».
# Same arrangement as PORTFOLIO_ROLES and the portefeuille field.
ROLE_DETAIL_ROLES = ["Bénévole", "Employé·e"]

ROLES_BY_CONTACT_TYPE = {
    "Journaliste": JOURNALIST_ROLES,
    "Politique": POLITICAL_ROLES,
    "Religieux·se": RELIGIOUS_ROLES,
    "Membre d'une ONG": NGO_ROLES,
    "Membre d'une entreprise": COMPANY_ROLES,
    # « Autre » is the type for someone none of the others fits, so there is no
    # list of functions to offer: whatever they do goes in the notes.
    "Autre": [],
}

# Every known function, in a stable order: politiques, then journalistes, then
# religieux·ses. This is the whitelist `_roles_from_form` validates against and
# the order the stored column is written in. No label appears in both the
# political and the journalistic list; the religious lists share a few labels
# with each other, which is what _dedup is for.
ROLES = _dedup(POLITICAL_ROLES + JOURNALIST_ROLES + RELIGIOUS_ROLES
               + NGO_ROLES + COMPANY_ROLES)

# How several roles are joined inside the single `role` TEXT column. No label in
# ROLES contains a comma, so this round-trips safely.
ROLE_SEP = ", "

# Holding one of these means the person has a government portfolio, so the
# "Portefeuille" field is revealed on the form (see static/form-masks.js, which
# reads this same list from `data-portfolio-roles`).
PORTFOLIO_ROLES = [
    "Ministre",
    "Ministre délégué·e",
    "Secrétaire d'État",
]


def name_sort_key(value):
    """Sort key putting a person under their nom de famille, not their prénom.

    Names are stored as a single « Prénom Nom » string, so ordering the column
    raw files everybody under their first name. The nom is taken to be the last
    word, which is what French names do — « Apolline de Malherbe » files under
    Malherbe — and a parenthesised nickname is dropped, since « Manuel Dorne
    (Korben) » is a Dorne and a bracket would sort ahead of every letter.
    Accents are folded so É files with E rather than after Z, and the prénoms
    are kept as a tie-breaker.

    Registered on every connection as the SQL function name_key(), so it can be
    used straight from an ORDER BY.
    """
    # Everything from the first comma is a suffix, not part of the name:
    # « Jean-Paul Vesco, o.p. » is a Vesco, and « o.p. » would otherwise be
    # read as his nom de famille.
    plain = re.sub(r"\([^)]*\)", " ", (value or "").split(",")[0])
    parts = plain.split()
    if not parts:
        return ""
    key = " ".join([parts[-1], *parts[:-1]]).lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", key)
        if not unicodedata.combining(c)
    )


def split_roles(value):
    """`persons.role` -> list of role labels (empty list when NULL/blank)."""
    return [r.strip() for r in (value or "").split(",") if r.strip()]


def _roles_from_form(contact_type=None, religion=None):
    """Checked roles, whitelisted against the known labels, in ROLES order.

    Whitelisting keeps the separator meaningful: a value that isn't a known
    label can never smuggle a comma into the column.

    `contact_type` narrows the whitelist to that type's functions, which is
    what makes changing someone's type clean: the form hides the other type's
    boxes but a hidden checked box still posts, so a journaliste corrected from
    « Politique » would otherwise keep « Député·e ». Omit it (the public
    declaration form, which asks for no type) to accept any list.

    `religion` narrows it once more, for a religieux·se only: the religion
    select hides the other cultes' checklists, so the same reasoning applies a
    level down — a rabbin corrected from « Catholicisme » must not keep
    « Cardinal ». An unknown or missing religion falls back to every religious
    function rather than to none, so a fiche whose religion nobody has filled
    in yet (or a row imported before the field existed) keeps its functions.
    """
    if contact_type == "Religieux·se":
        allowed = ROLES_BY_RELIGION.get(religion, RELIGIOUS_ROLES)
    else:
        allowed = ROLES_BY_CONTACT_TYPE.get(contact_type, ROLES)
    checked = set(request.form.getlist("role"))
    return ROLE_SEP.join(r for r in allowed if r in checked)


def has_portfolio(value):
    """True when any of the person's roles is a government portfolio."""
    return any(r in PORTFOLIO_ROLES for r in split_roles(value))


def has_role_detail(value):
    """True when a role asks to be spelled out — see ROLE_DETAIL_ROLES."""
    return any(r in ROLE_DETAIL_ROLES for r in split_roles(value))


def selected_roles(form):
    """Which role boxes the form should render as checked.

    `form` is either a submitted MultiDict (re-render after a validation error,
    where `role` repeats once per checked box — .get() would return only the
    first) or a plain dict built from a DB row by _form_from_row (where `role`
    is the comma-joined column).
    """
    getlist = getattr(form, "getlist", None)
    if getlist is not None:
        return [r for r in getlist("role") if r in ROLES]
    return split_roles(form.get("role"))

# How a person feels about PauseAI — single source of truth for the dropdown.
STANCES = [
    "Favorable",
    "Plutôt favorable",
    "Neutre / indécis",
    "Plutôt opposé",
    "Opposé",
    "Inconnu",
]

# --------------------------------------------------------------------------- #
# Organisations
# --------------------------------------------------------------------------- #
# Fields below are per org_type: a média has a type de média and an orientation
# politique, a groupe politique has a chambre, a culte has a religion (see
# RELIGIONS above — the same list the person form offers). Name, position sur
# PauseIA, lien and notes are shared, and are the only fields a groupe
# politique or a culte carries beyond that one column — an orientation
# politique on a group was judged too fuzzy to be worth recording.

MEDIA_TYPES = [
    "Presse écrite",
    "Télévision",
    "Radio",
    "Site web / pure player",
    "YouTube",
    "Podcast",
    "Agence de presse",
    "Autre",
]

# Political orientation of a média.
ORIENTATIONS = [
    "Extrême gauche",
    "Gauche",
    "Centre gauche",
    "Centre",
    "Centre droit",
    "Droite",
    "Extrême droite",
    "Inconnue",
]

# Where a groupe politique sits. Deliberately the keys of POLITICAL_GROUPS, so
# the institution a group was already filed under is exactly its chambre.
CHAMBERS = list(POLITICAL_GROUPS)

# Every staging table the /moderation page reviews, and the badge counts.
PENDING_TABLES = (
    "pending_persons",
    "pending_organisations",
    "pending_meetings",
    "pending_mails",
    "pending_interventions",
    "pending_contents",
)

CONTENT_TYPES = ["Article", "Interview", "Reportage", "Vidéo", "Autre"]

INTERVENTION_TYPES = ["Interview", "Plateau TV", "Radio", "Tribune", "Autre"]

# Shown as the "who added this person" value for rows imported in bulk from the
# official lists of elected officials (they have no moderator in `added_by`).
AUTO_IMPORT_LABEL = (
    "Ajout automatique à partir des listes officielles d'élus au 26-07-2026."
)

# Mail directions: stored value -> French label shown in the UI.
# Combien de courriels chaque page en lit. Le suivi des échanges regroupe en
# fils, donc il lit une fenêtre de courriels ; /mails pagine simplement.
EXCHANGES_WINDOW = 500
EXCHANGES_WINDOW_MAX = 20000
MAILS_PER_PAGE = 100
# Les listes de fiches : 1 780 personnes et 451 organisations aujourd'hui, et
# rien ne les fait décroître. Une page en rend cent.
PEOPLE_PER_PAGE = 100


def page_window(per_page):
    """(page, limite, décalage) depuis la requête, pour une liste paginée.

    On demande un élément de plus que la page : c'est ce qui dit s'il existe une
    suite, sans payer un COUNT(*) sur toute la table à chaque affichage.
    """
    page = max(1, request.args.get("page", type=int) or 1)
    return page, per_page + 1, (page - 1) * per_page

MAIL_DIRECTIONS = {
    "sent": "Envoyé",
    "received": "Reçu",
}

# French names for the calendar (month index 1-12, weekdays Monday-first).
FRENCH_MONTHS = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
FRENCH_WEEKDAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    # Lax blocks the session cookie on cross-site POSTs, so a malicious page
    # cannot fire authenticated actions (deletes, moderation) with a logged-in
    # member's session (CSRF).
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    # Only send the session cookie over HTTPS. Gated on an env var because it
    # would break login on plain-http local dev; set PRODUCTION=1 on the server.
    SESSION_COOKIE_SECURE=bool(os.environ.get("PRODUCTION")),
)

# In production the app sits behind Caddy (reverse proxy), so every request's
# remote_addr is Caddy's internal IP. Trust one hop of X-Forwarded-For/-Proto
# to recover the real client IP (rate limiting) and scheme. Gated on
# PRODUCTION because without a proxy in front, clients could spoof the header.
if os.environ.get("PRODUCTION"):
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.template_filter("fr_date")
def fr_date(value):
    """Render an ISO date (YYYY-MM-DD) as the French DD/MM/YYYY."""
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value


@app.template_filter("days_until")
def days_until(value):
    """Whole days from today to an ISO date. Negative if already past."""
    if not value:
        return None
    try:
        target = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (target - date.today()).days


@app.template_filter("fr_slot")
def fr_slot(slot):
    """Render a candidate slot: « 11/09/2026 à 14:00 », or just the date."""
    on_date, at_time = split_slot(slot)
    return f"{fr_date(on_date)} à {at_time}" if at_time else fr_date(on_date)


@app.template_filter("safe_link")
def safe_link(value):
    """True when `value` is an http(s) URL, i.e. safe to put in an href.

    Stored links are validated on save, but free-text fields such as a
    person's social links are not: rendering a `javascript:` value as a link
    would run it on click, so templates only link what passes this.
    """
    return (value or "").strip().lower().startswith(("http://", "https://"))


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def get_db():
    """Return a request-scoped SQLite connection with foreign keys enabled."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # The utils/ importers write to this same file, and a long import used
        # to make every request that touched the database fail outright with
        # "database is locked". Wait for the writer instead of 500-ing at the
        # first contention.
        g.db.execute("PRAGMA busy_timeout = 15000")
        g.db.create_function("name_key", 1, name_sort_key, deterministic=True)
    return g.db


@app.teardown_appcontext
def close_db(exception):  # noqa: ARG001
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_role_helpers():
    """`role` holds a comma-joined list, so templates need to split it."""
    return {
        "split_roles": split_roles,
        "selected_roles": selected_roles,
        "portfolio_roles": PORTFOLIO_ROLES,
        # Lets each form pre-fill « Relance prévue » from its own date field
        # without every route having to pass the value in.
        "default_follow_up": default_follow_up,
        "follow_up_days": FOLLOW_UP_DEFAULT_DAYS,
        "meeting_formats": MEETING_FORMATS,
        "format_label": format_label,
        "place_label": place_label,
        "max_alt_dates": MAX_ALT_DATES,
        "parse_alt_dates": parse_alt_dates,
        # Candidate slots: a date with an optional time, so a rencontre can
        # offer two créneaux on the same day. See make_slot.
        "split_slot": split_slot,
        "slot_date": slot_date,
        "main_slot": main_slot,
        # Type de contact, and everything it conditions. The form renders every
        # type's block and shows one (see static/form-masks.js); these let it do
        # that from the same single source of truth the server validates on.
        "contact_types": CONTACT_TYPES,
        "roles_by_contact_type": ROLES_BY_CONTACT_TYPE,
        "religions": RELIGIONS,
        "roles_by_religion": ROLES_BY_RELIGION,
        "org_type_by_contact_type": ORG_TYPE_BY_CONTACT_TYPE,
        "org_types": ORG_TYPES,
        "neutral_org_types": NEUTRAL_ORG_TYPES,
        "genres": GENRES,
        "political_only_roles": POLITICAL_ROLES,
        "role_detail_roles": ROLE_DETAIL_ROLES,
        # The vocabularies the organisation and press forms pick from. Exposed
        # globally so the public declaration pages, which share one generic
        # view function, don't each have to be handed their own list.
        "stances": STANCES,
        "media_types": MEDIA_TYPES,
        "orientations": ORIENTATIONS,
        "chambers": CHAMBERS,
        "content_types": CONTENT_TYPES,
        "intervention_types": INTERVENTION_TYPES,
    }


@app.context_processor
def inject_pending_count():
    """Expose the number of drafts awaiting moderation to every template, so the
    nav can show a badge. Only computed for authenticated (certified) users."""
    if not session.get("authenticated"):
        return {"pending_count": 0}
    db = get_db()
    total = sum(
        db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in PENDING_TABLES
    )
    return {"pending_count": total}


@app.context_processor
def inject_import_state():
    """Expose the freshness of the mail import to the templates that show it.

    A context processor rather than three route arguments: the banner belongs on
    every page built from the import, and the query is one indexed row. Skipped
    for anonymous visitors, who see the public declaration forms only.
    """
    if not session.get("authenticated"):
        return {"import_state": None, "unlinked_count": 0}
    db = get_db()
    try:
        waiting = db.execute(
            "SELECT COUNT(*) FROM civicrm_pending WHERE status IN ('pending', "
            "'absent')").fetchone()[0]
    except sqlite3.Error:
        waiting = 0
    return {"import_state": import_status(db), "unlinked_count": waiting}


# The member import runs every 10 minutes. Beyond this, three runs in a row have
# failed to happen and the interface should say so rather than look normal.
IMPORT_STALE_AFTER = timedelta(minutes=30)


def import_status(db, script="member_mails"):
    """How fresh the imported mail is — for the banner on the exchange pages.

    Returns None when nothing has ever run (a fresh install: there is nothing to
    reassure anyone about yet). Otherwise a dict the template renders as-is:
    `state` is 'ok', 'stale' or 'error', `label` a ready-made French phrase.

    Why this exists: a member who had just written to a journalist could not tell
    "not imported yet" from "not recognised" from "broken since Tuesday". And an
    expired IMAP password left every page looking perfectly normal while nothing
    arrived any more.
    """
    try:
        row = db.execute(
            "SELECT started_at, finished_at, status, imported, detail "
            "FROM import_runs WHERE script = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1", (script,)).fetchone()
    except sqlite3.Error:
        return None                      # table not created yet
    if row is None:
        return None

    started = _parse_iso(row["started_at"])
    age = datetime.now(timezone.utc) - started if started else None

    if row["status"] == "error":
        state = "error"
    elif age is not None and age > IMPORT_STALE_AFTER:
        state = "stale"
    else:
        state = "ok"

    return {
        "state": state,
        "when": _humanise_age(age),
        "imported": row["imported"],
        "detail": row["detail"],
        "status": row["status"],
    }


def _parse_iso(value):
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    # Rows written before the importers stored a timezone read as naive; treat
    # them as UTC, which is what every script has always written.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _humanise_age(age):
    """« il y a 4 minutes » — the phrase a human actually reads."""
    if age is None:
        return "à une date inconnue"
    seconds = max(0, int(age.total_seconds()))
    if seconds < 90:
        return "à l'instant"
    minutes = seconds // 60
    if minutes < 60:
        return f"il y a {minutes} minutes"
    hours = minutes // 60
    if hours < 24:
        return f"il y a {hours} heure{'s' if hours > 1 else ''}"
    days = hours // 24
    return f"il y a {days} jour{'s' if days > 1 else ''}"


@contextlib.contextmanager
def _migration_lock():
    """Let one process at a time run the migrations.

    `init_db()` runs on import, so under gunicorn all four workers reach it at
    once. That is how the deploy of 44b4311 crash-looped: one worker's
    `RENAME COLUMN details` landed while another was running `UPDATE … details`,
    and the second saw a column that no longer existed.

    A file lock rather than a SQLite transaction, because `executescript()`
    commits any open transaction before it runs — so `BEGIN IMMEDIATE` around
    this body would simply be dropped. `flock` also costs nothing and is held by
    the kernel, so a worker that dies mid-migration releases it instead of
    wedging the next boot.

    The workers that queue behind the lock still run the migrations afterwards;
    every one of them is guarded (`IF NOT EXISTS`, a `PRAGMA table_info` check),
    so by then they are no-ops. Serialising is what matters, not skipping.

    Yields True when the lock was taken. If it cannot be (a read-only directory,
    a platform without flock), it yields False and start-up carries on: a single
    process is the normal case in development, where there is nothing to race.
    """
    lock_path = f"{DB_PATH}.migrate.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        yield False
    finally:
        os.close(fd)


def init_db():
    """Create tables if they don't exist yet, and run lightweight migrations.

    Runs at import time in every gunicorn worker, hence the lock — see
    _migration_lock for what happens without it.
    """
    with _migration_lock():
        _init_db_locked()


def _init_db_locked():
    db = sqlite3.connect(DB_PATH)
    # Belt and braces behind the file lock: should two processes ever reach the
    # database at once anyway, wait for the writer rather than raising
    # "database is locked" and taking the worker down with it.
    db.execute("PRAGMA busy_timeout = 30000")
    db.executescript(
        """
        -- Certified users (password holders). Just an identity — name or Discord
        -- pseudo. Referenced by the provenance fields on the real records below.
        CREATE TABLE IF NOT EXISTS moderators (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        -- Every organisation a contact can belong to: a média for a
        -- journaliste, a groupe politique for a politique, a culte for a
        -- religieux·se. One table because they all answer the same question —
        -- who does this person speak for — and the columns that differ are
        -- simply NULL for the other types (`media_type`/`orientation` for a
        -- groupe or a culte, `chambre` for a média, `religion` for both).
        CREATE TABLE IF NOT EXISTS organisations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            org_type     TEXT NOT NULL,   -- see ORG_TYPES
            media_type   TEXT,            -- Média only, see MEDIA_TYPES
            orientation  TEXT,            -- Média only, see ORIENTATIONS
            chambre      TEXT,            -- Groupe politique only, see CHAMBERS
            religion     TEXT,            -- Culte only, see RELIGIONS
            stance       TEXT NOT NULL,
            link         TEXT,
            notes        TEXT,
            added_by     INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            validated_by INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            created_at   TEXT NOT NULL
        );

        -- A person belongs to 0..n organisations: a pigiste writes for several
        -- titles, and an élu·e who changes group keeps both while the change is
        -- being recorded. `persons.political_group` mirrors the groupe
        -- politique of a politique for backward compatibility — see init_db.
        CREATE TABLE IF NOT EXISTS person_organisations (
            person_id       INTEGER NOT NULL REFERENCES persons(id)       ON DELETE CASCADE,
            organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
            PRIMARY KEY (person_id, organisation_id)
        );

        CREATE TABLE IF NOT EXISTS persons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            contact_type    TEXT NOT NULL DEFAULT 'Politique',  -- see CONTACT_TYPES
            role            TEXT,
            political_group TEXT NOT NULL,
            phone           TEXT,
            social_links    TEXT,   -- free text, one link per line
            stance          TEXT NOT NULL,
            first_contacted TEXT,
            notes           TEXT,
            circonscription TEXT,
            email           TEXT,
            photo_url       TEXT,            -- portrait officiel (Sénat / AN)
            fiche_url       TEXT,            -- fiche parlementaire officielle
            portefeuille    TEXT,   -- government portfolio, see PORTFOLIO_ROLES
            role_detail     TEXT,   -- free text behind a ROLE_DETAIL_ROLES role
            religion        TEXT,   -- Religieux·se only, see RELIGIONS
            territoire      TEXT,   -- Religieux·se only: diocèse, paroisse…
            in_office       INTEGER NOT NULL DEFAULT 1,  -- 0 = mandate ended, kept for history
            signed_declaration INTEGER NOT NULL DEFAULT 0,  -- « A signé la déclaration de PauseIA »
            added_by        INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            validated_by    INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meetings (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date         TEXT NOT NULL,
            meeting_time         TEXT,
            summary              TEXT NOT NULL,
            follow_up_date       TEXT,   -- when to follow this meeting up
            done                 INTEGER NOT NULL DEFAULT 0,  -- ticked off on /todo
            done_at              TEXT,   -- local date it was ticked; see /todo
            follow_up_done       INTEGER NOT NULL DEFAULT 0,
            follow_up_done_at    TEXT,
            recorded_by          TEXT,
            validated_by         INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            document_stored_name TEXT,
            document_orig_name   TEXT,
            created_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meeting_persons (
            meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            person_id  INTEGER NOT NULL REFERENCES persons(id)  ON DELETE CASCADE,
            PRIMARY KEY (meeting_id, person_id)
        );

        -- Who (among the moderators) took part in a meeting: 1..n.
        CREATE TABLE IF NOT EXISTS meeting_moderators (
            meeting_id   INTEGER NOT NULL REFERENCES meetings(id)   ON DELETE CASCADE,
            moderator_id INTEGER NOT NULL REFERENCES moderators(id) ON DELETE CASCADE,
            PRIMARY KEY (meeting_id, moderator_id)
        );

        -- Who is free on which candidate date, while a rencontre is still
        -- being scheduled (see /repartition). These rows are temporary: once a
        -- date is chosen, that date's rows become the rencontre's participants
        -- and every row for the meeting is dropped, so `meeting_moderators`
        -- stays the single answer to "who goes" as soon as the date is settled.
        CREATE TABLE IF NOT EXISTS meeting_availability (
            meeting_id   INTEGER NOT NULL REFERENCES meetings(id)   ON DELETE CASCADE,
            moderator_id INTEGER NOT NULL REFERENCES moderators(id) ON DELETE CASCADE,
            on_date      TEXT NOT NULL,
            PRIMARY KEY (meeting_id, moderator_id, on_date)
        );

        -- An intervention is PauseIA speaking somewhere: an interview given, a
        -- plateau TV, a tribune. It names the organisation that carried it, the
        -- people on the other side (journalistes, or a politique when the
        -- exchange happened on a group's own channel) and the utilisateurices
        -- who spoke for PauseIA.
        CREATE TABLE IF NOT EXISTS interventions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id   INTEGER NOT NULL REFERENCES organisations(id),
            intervention_date TEXT NOT NULL,
            intervention_type TEXT NOT NULL,
            link              TEXT NOT NULL,
            summary           TEXT,
            recorded_by       INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            validated_by      INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            created_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS intervention_persons (
            intervention_id INTEGER NOT NULL REFERENCES interventions(id) ON DELETE CASCADE,
            person_id       INTEGER NOT NULL REFERENCES persons(id)       ON DELETE CASCADE,
            PRIMARY KEY (intervention_id, person_id)
        );

        -- Who spoke for PauseIA in an intervention: 1..n utilisateurices.
        CREATE TABLE IF NOT EXISTS intervention_moderators (
            intervention_id INTEGER NOT NULL REFERENCES interventions(id) ON DELETE CASCADE,
            moderator_id    INTEGER NOT NULL REFERENCES moderators(id)    ON DELETE CASCADE,
            PRIMARY KEY (intervention_id, moderator_id)
        );

        -- A contenu is something the organisation published about PauseIA or
        -- about AI risk, which PauseIA did not take part in producing. Hence no
        -- participants — only the people who signed it.
        CREATE TABLE IF NOT EXISTS contents (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            organisation_id INTEGER NOT NULL REFERENCES organisations(id),
            content_type    TEXT NOT NULL,
            link            TEXT NOT NULL,
            published_on    TEXT NOT NULL,
            summary         TEXT,
            recorded_by     INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            validated_by    INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS content_persons (
            content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
            person_id  INTEGER NOT NULL REFERENCES persons(id)  ON DELETE CASCADE,
            PRIMARY KEY (content_id, person_id)
        );

        -- « À contacter »: people someone has decided to write to, waiting on
        -- /todo until the box is ticked, after which they live on /fait. The
        -- list is filled by the répartition (/todo/a-contacter/repartition),
        -- and a person appears at most once (PRIMARY KEY). assigned_to is the
        -- utilisateurice who is to write; NULL means « Quiconque » (anyone).
        CREATE TABLE IF NOT EXISTS to_contact (
            person_id  INTEGER PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
            added_by   INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            assigned_to INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            done       INTEGER NOT NULL DEFAULT 0,
            done_at    TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mails (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_date            TEXT NOT NULL,
            direction            TEXT NOT NULL,   -- 'sent' or 'received'
            important            INTEGER NOT NULL DEFAULT 0,
            subject              TEXT,            -- "Objet" of the mail
            summary              TEXT NOT NULL,   -- "Corps du texte"
            follow_up_date       TEXT,            -- when to follow this mail up
            follow_up_done       INTEGER NOT NULL DEFAULT 0,  -- ticked off on /todo
            follow_up_done_at    TEXT,
            received_by          INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            validated_by         INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            document_stored_name TEXT,
            document_orig_name   TEXT,
            created_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mail_persons (
            mail_id   INTEGER NOT NULL REFERENCES mails(id)    ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES persons(id)  ON DELETE CASCADE,
            PRIMARY KEY (mail_id, person_id)
        );

        -- Association members (@pauseia.fr). Populated automatically by
        -- utils/import_member_mails.py from the members' correspondence with
        -- élu·es; a mail is linked to its member via mail_members. This is what
        -- distinguishes an *association member* exchange from an anonymous
        -- citizen's campaign mail.
        CREATE TABLE IF NOT EXISTS members (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT UNIQUE NOT NULL,
            name       TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mail_members (
            mail_id   INTEGER NOT NULL REFERENCES mails(id)    ON DELETE CASCADE,
            member_id INTEGER NOT NULL REFERENCES members(id)  ON DELETE CASCADE,
            PRIMARY KEY (mail_id, member_id)
        );

        -- Full body of member mails (see utils/import_member_mails.py) and the
        -- conversation grouping key, so successive exchanges collapse into one
        -- thread in the UI. Citizen campaign mails have no body stored.
        CREATE TABLE IF NOT EXISTS mail_bodies (
            mail_id INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
            body    TEXT
        );
        CREATE TABLE IF NOT EXISTS mail_thread (
            mail_id    INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
            thread_key TEXT NOT NULL,
            message_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mail_thread_key ON mail_thread(thread_key);

        -- Index de volume. Invisibles à 2 000 courriels, décisifs à 30 000 : la
        -- boîte d'audit ne perd jamais un message, donc ces tables ne font que
        -- croître. Chaque index correspond à un tri ou à une jointure que les
        -- pages font à chaque affichage.
        CREATE INDEX IF NOT EXISTS idx_mails_date ON mails(mail_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_mail_persons_person
            ON mail_persons(person_id);
        CREATE INDEX IF NOT EXISTS idx_mail_members_member
            ON mail_members(member_id);
        CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date DESC);
        -- /todo lit les relances en attente : sans index, un parcours complet
        -- des deux tables à chaque ouverture de la page d'accueil de l'équipe.
        CREATE INDEX IF NOT EXISTS idx_meetings_followup
            ON meetings(follow_up_done, follow_up_date);
        CREATE INDEX IF NOT EXISTS idx_mails_followup
            ON mails(follow_up_done, follow_up_date);

        -- What each import run did (utils/importruns.py). Written by the
        -- importers, read here so the interface can state how fresh the data is
        -- instead of asking people to trust it — and say so when a run fails.
        -- Declared here too because the app reads it before any import has run.
        -- Operational metadata only: a script name, timestamps, counts.
        CREATE TABLE IF NOT EXISTS import_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            script      TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT NOT NULL,
            imported    INTEGER NOT NULL DEFAULT 0,
            inspected   INTEGER NOT NULL DEFAULT 0,
            detail      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_import_runs_script
            ON import_runs (script, started_at DESC);

        -- The addresses the mail import could not attach to anyone, and the
        -- other addresses a person is known by. Canonical definitions live in
        -- utils/civicrm_lookup.ensure_civicrm_tables() (kept identical);
        -- declared here because /echanges/a-rattacher reads and writes them,
        -- and the app may well start before any script has run.
        --   pending  — waiting for a lookup      absent  — CiviCRM doesn't know it
        --   resolved — attached to a fiche       ignored — a human said "not a person"
        CREATE TABLE IF NOT EXISTS civicrm_pending (
            email       TEXT PRIMARY KEY,
            display     TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            seen_count  INTEGER NOT NULL DEFAULT 1,
            status      TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT,
            person_id   INTEGER REFERENCES persons(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_civicrm_pending_status
            ON civicrm_pending(status);
        CREATE TABLE IF NOT EXISTS person_emails (
            email      TEXT PRIMARY KEY,
            person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            source     TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_person_emails_person
            ON person_emails(person_id);

        -- Staging tables. Anonymous users (no password) submit drafts here via
        -- the "Déclarer une activité" forms. A certified user reviews them on the
        -- /moderation page and either promotes a draft into the real table above
        -- or deletes it. Nothing here is ever shown in the normal lists.
        CREATE TABLE IF NOT EXISTS pending_persons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            contact_type    TEXT,   -- nullable: a draft may not say
            role            TEXT,
            email           TEXT,
            phone           TEXT,
            proposed_organisation TEXT,  -- free-text média / groupe, matched at approval
            portefeuille    TEXT,
            religion        TEXT,
            territoire      TEXT,
            political_group TEXT,   -- nullable: an anonymous draft may omit it
            stance          TEXT,
            signed_declaration INTEGER NOT NULL DEFAULT 0,
            first_contacted TEXT,
            follow_up_date  TEXT,
            notes           TEXT,
            submitted_by    TEXT,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_organisations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            org_type     TEXT,
            media_type   TEXT,
            orientation  TEXT,
            chambre      TEXT,
            religion     TEXT,
            stance       TEXT,
            link         TEXT,
            notes        TEXT,
            submitted_by TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_interventions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            proposed_organisation TEXT NOT NULL,
            proposed_people   TEXT NOT NULL,
            intervention_date TEXT NOT NULL,
            intervention_type TEXT,
            link              TEXT NOT NULL,
            summary           TEXT,
            submitted_by      TEXT,
            created_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_contents (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            proposed_organisation TEXT NOT NULL,
            proposed_people TEXT NOT NULL,
            content_type    TEXT,
            link            TEXT NOT NULL,
            published_on    TEXT NOT NULL,
            summary         TEXT,
            submitted_by    TEXT,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_meetings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date    TEXT NOT NULL,
            meeting_time    TEXT,
            summary         TEXT NOT NULL,
            follow_up_date  TEXT,
            proposed_people TEXT,   -- free-text "personnes concernées"
            submitted_by    TEXT,
            document_stored_name TEXT,
            document_orig_name   TEXT,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_mails (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_date       TEXT NOT NULL,
            direction       TEXT NOT NULL,
            important       INTEGER NOT NULL DEFAULT 0,
            subject         TEXT,
            summary         TEXT NOT NULL,
            follow_up_date  TEXT,
            proposed_people TEXT,   -- free-text "personnes concernées"
            submitted_by    TEXT,
            document_stored_name TEXT,
            document_orig_name   TEXT,
            created_at      TEXT NOT NULL
        );
        """
    )
    # Migrations: add follow_up_date columns to tables created before they existed.
    person_cols = [r[1] for r in db.execute("PRAGMA table_info(persons)")]
    # « Relance prévue » used to be typed straight onto a person and copied down
    # from each sent mail. It is now derived (see person_follow_up_sql), so the
    # stored column is retired under a _legacy name rather than dropped: nothing
    # reads it, but a value entered under the old rules is still recoverable.
    if "follow_up_date" in person_cols and "follow_up_date_legacy" not in person_cols:
        db.execute("ALTER TABLE persons RENAME COLUMN follow_up_date "
                   "TO follow_up_date_legacy")
    if "role" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN role TEXT")
    if "in_office" not in person_cols:
        db.execute(
            "ALTER TABLE persons ADD COLUMN in_office INTEGER NOT NULL DEFAULT 1"
        )
    # Portrait et fiche officielle. Les deux existent déjà dans l'elus.json de
    # l'outil « Écrire à mes élus » du site, d'où ils sont repris tels quels
    # par utils/sync_emails_from_elus.py : pas de nouvelle source à maintenir,
    # et le CRM montre exactement ce que le site montre.
    if "photo_url" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN photo_url TEXT")
    if "fiche_url" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN fiche_url TEXT")
    mail_cols = [r[1] for r in db.execute("PRAGMA table_info(mails)")]
    if "follow_up_date" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN follow_up_date TEXT")
    if "follow_up_done" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN follow_up_done "
                   "INTEGER NOT NULL DEFAULT 0")
    if "follow_up_done_at" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN follow_up_done_at TEXT")
    if "subject" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN subject TEXT")
    meeting_cols = [r[1] for r in db.execute("PRAGMA table_info(meetings)")]
    if "meeting_time" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN meeting_time TEXT")
    if "follow_up_date" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN follow_up_date TEXT")
    # The /todo tick boxes. NOT NULL DEFAULT 0 backfills existing rows with 0,
    # so no record ever arrives already ticked off.
    if "done" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
    if "follow_up_done" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN follow_up_done "
                   "INTEGER NOT NULL DEFAULT 0")
    # The date a box was ticked, so /todo can drop it once the day is over
    # without anything having to run at midnight.
    if "done_at" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN done_at TEXT")
    if "follow_up_done_at" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN follow_up_done_at TEXT")
    # Visio or présentiel, and where. Added nullable on purpose: SQLite cannot
    # add a NOT NULL column without a default, and any default here would be an
    # assertion nobody made — a rencontre recorded before the field existed has
    # no known format. Legacy rows stay NULL and read « Non renseigné »; the
    # form requires the field, so touching an old rencontre fills the gap.
    if "meeting_format" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN meeting_format TEXT")
    # Free text: the address in présentiel (required), the link or platform in
    # visio (optional). One column, because it answers one question — where.
    if "meeting_place" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN meeting_place TEXT")
    # Candidate dates still being arbitrated, as a comma-joined ISO list. NULL
    # means the date is settled: that is exactly what puts a rencontre on
    # /repartition rather than in the "à venir" list, and choosing a date
    # clears this back to NULL. See MAX_ALT_DATES.
    if "alt_dates" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN alt_dates TEXT")
    pmeeting_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_meetings)")]
    # The public form asks for these but does not insist (it is lenient
    # everywhere else too); the moderator supplies them at approval, where the
    # shared rencontre form does require them.
    if "meeting_format" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN meeting_format TEXT")
    if "meeting_place" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN meeting_place TEXT")
    if "document_stored_name" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN document_stored_name TEXT")
    if "document_orig_name" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN document_orig_name TEXT")
    if "follow_up_date" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN follow_up_date TEXT")
    pmail_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_mails)")]
    if "subject" not in pmail_cols:
        db.execute("ALTER TABLE pending_mails ADD COLUMN subject TEXT")
    pmail_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_mails)")]
    if "document_stored_name" not in pmail_cols:
        db.execute("ALTER TABLE pending_mails ADD COLUMN document_stored_name TEXT")
    if "document_orig_name" not in pmail_cols:
        db.execute("ALTER TABLE pending_mails ADD COLUMN document_orig_name TEXT")
    # Optional contact/mandate details on a person.
    if "circonscription" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN circonscription TEXT")
    if "email" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN email TEXT")
    # Government portfolio ("chargé·e de l'énergie"), only meaningful for the
    # PORTFOLIO_ROLES. The form hides the field for everyone else.
    if "portefeuille" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN portefeuille TEXT")
    pperson_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_persons)")]
    if "portefeuille" not in pperson_cols:
        db.execute("ALTER TABLE pending_persons ADD COLUMN portefeuille TEXT")
    # Provenance fields referencing moderators(id). Added via ALTER with a NULL
    # default (SQLite requires that for a column carrying a REFERENCES clause);
    # legacy rows predate the feature and keep NULL.
    if "added_by" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN added_by INTEGER "
                   "REFERENCES moderators(id) ON DELETE SET NULL")
    if "validated_by" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN validated_by INTEGER "
                   "REFERENCES moderators(id) ON DELETE SET NULL")
    if "validated_by" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN validated_by INTEGER "
                   "REFERENCES moderators(id) ON DELETE SET NULL")
    if "received_by" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN received_by INTEGER "
                   "REFERENCES moderators(id) ON DELETE SET NULL")
    if "validated_by" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN validated_by INTEGER "
                   "REFERENCES moderators(id) ON DELETE SET NULL")
    # « Compte rendu détaillé » was a second free-text box next to « Points clés
    # de la rencontre ». One box is enough, so whatever people wrote in it is
    # folded into the summary — appended after a blank line, in the order it was
    # written — and the column is retired under a _legacy name rather than
    # dropped, the same treatment persons.follow_up_date got: nothing reads it,
    # but the original split stays recoverable if the merge ever needs undoing.
    # The rename is what makes this run exactly once.
    for table in ("meetings", "pending_meetings"):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        if "details" not in cols or "details_legacy" in cols:
            continue
        db.execute(
            f"""
            UPDATE {table}
               SET summary = trim(
                     COALESCE(NULLIF(trim(summary), '') || char(10) || char(10), '')
                     || trim(details))
             WHERE details IS NOT NULL AND trim(details) <> ''
            """
        )
        db.execute(f"ALTER TABLE {table} RENAME COLUMN details TO details_legacy")
    # --- The merge of the journalist CRM into this app ---------------------- #
    # A person is now a journaliste or a politique (`contact_type`), and belongs
    # to organisations rather than carrying a group name as text. Both arrive as
    # plain ADD COLUMNs; the reshaping that ALTER cannot express is below.
    person_cols = [r[1] for r in db.execute("PRAGMA table_info(persons)")]
    if "contact_type" not in person_cols:
        # Every row that predates the merge is a politician: that is all this
        # app tracked. Journalistes arrive with the value set explicitly.
        db.execute("ALTER TABLE persons ADD COLUMN contact_type TEXT "
                   "NOT NULL DEFAULT 'Politique'")
    # Contact details a journaliste needs and an élu·e may have too — the fields
    # below the type de contact are the same for everyone.
    if "phone" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN phone TEXT")
    if "social_links" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN social_links TEXT")
    pperson_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_persons)")]
    for col in ("contact_type", "email", "phone", "proposed_organisation"):
        if col not in pperson_cols:
            db.execute(f"ALTER TABLE pending_persons ADD COLUMN {col} TEXT")
    # --- « Religieux·se », the third type de contact ------------------------ #
    # Purely additive, unlike the merge above: a third CONTACT_TYPES entry, a
    # third ORG_TYPES entry and three nullable columns. No existing row changes
    # type, so every politique stays a politique and every média a média.
    #
    #   persons.religion    which culte, see RELIGIONS — also what narrows the
    #                       fonctions offered (see _roles_from_form)
    #   persons.territoire  « Territoire assigné »: the diocèse, paroisse or
    #                       circonscription rabbinique someone is responsible
    #                       for. The religious counterpart of circonscription,
    #                       and a separate column rather than a reuse of it so
    #                       that neither field's meaning has to stretch.
    #   organisations.religion  which culte a Culte organisation belongs to,
    #                       exactly as media_type qualifies a Média and chambre
    #                       a Groupe politique.
    #
    # The pending_* tables get the same columns so a public declaration keeps
    # them all the way to approval (_form_from_row copies whatever is there).
    for table, cols in (
        # role_detail: the free text behind « Bénévole » or « Employé·e ».
        # Nullable and added late, exactly like portefeuille: a fiche recorded
        # before the field existed simply has nothing to say here.
        ("persons", ("religion", "territoire", "role_detail")),
        ("pending_persons", ("religion", "territoire", "role_detail")),
        ("organisations", ("religion",)),
        ("pending_organisations", ("religion",)),
    ):
        existing = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        for col in cols:
            if col not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    # --- « Genre » on an intervention and a contenu ------------------------- #
    # Optional and purely additive: a nullable column on each, NULL meaning
    # « not stated », which is what every row recorded before the field existed
    # is. It narrows the people the form offers and nothing else, so no
    # existing record changes meaning. See GENRES.
    for table in ("interventions", "contents",
                  "pending_interventions", "pending_contents"):
        existing = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        if "genre" not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN genre TEXT")
    # --- « A signé la déclaration de PauseIA » ---------------------------- #
    # Oui (1) / Non (0), mandatory with Non by default: every person recorded
    # before the field existed reads Non, and so does anyone the utils/
    # importers add, since they do not name the column.
    for table in ("persons", "pending_persons"):
        existing = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        if "signed_declaration" not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN signed_declaration "
                       "INTEGER NOT NULL DEFAULT 0")
    # --- « Attribuée à » on « À contacter » ------------------------------- #
    # Who is to write to the person, set by the répartition. Lists built before
    # it existed have nobody assigned, which is what NULL says.
    to_contact_cols = [r[1] for r in db.execute("PRAGMA table_info(to_contact)")]
    if "assigned_to" not in to_contact_cols:
        db.execute("ALTER TABLE to_contact ADD COLUMN assigned_to INTEGER "
                   "REFERENCES moderators(id) ON DELETE SET NULL")
    db.commit()
    _relax_political_group(db)
    _seed_organisations_from_groups(db)
    _slotify_availability(db)
    # APRÈS _relax_political_group, et pas dans le script principal : cette
    # migration reconstruit `persons` (DROP TABLE puis RENAME), ce qui emporte
    # les index posés avant elle. Créés ici, ils survivent au premier démarrage
    # comme aux suivants — un test de démarrage concurrent l'a montré en
    # comparant le schéma après deux lancements.
    db.executescript(
        """
        -- Le rapprochement des courriels interroge `persons.email` pour chaque
        -- message traité : c'est la requête la plus répétée de tout l'outil.
        CREATE INDEX IF NOT EXISTS idx_persons_email ON persons(email);
        CREATE INDEX IF NOT EXISTS idx_persons_contact_type
            ON persons(contact_type);
        """
    )
    db.commit()
    db.close()


def _slotify_availability(db):
    """Move sign-ups onto the main slot for rencontres that carry a time.

    A candidate is now a slot — a date with an optional time (see make_slot) —
    so a rencontre whose own time is set has a main slot of
    "2026-09-11T10:00", while its availability rows were written as the bare
    "2026-09-11" before slots existed. Left alone, those sign-ups would stop
    matching and the main column would open empty on /repartition, quietly
    losing what people had ticked.

    Only touches rows that are a bare date equal to the rencontre's own date,
    on a rencontre that has a time. After the rewrite they carry a 'T' and no
    longer match, so this is a no-op on every later run.
    """
    rows = db.execute(
        """
        SELECT a.meeting_id, a.moderator_id, a.on_date,
               m.meeting_date || 'T' || m.meeting_time AS slot
          FROM meeting_availability a
          JOIN meetings m ON m.id = a.meeting_id
         WHERE m.alt_dates IS NOT NULL
           AND m.meeting_time IS NOT NULL AND TRIM(m.meeting_time) <> ''
           AND a.on_date = m.meeting_date
        """
    ).fetchall()
    for meeting_id, moderator_id, on_date, slot in rows:
        # OR IGNORE, then delete: the slot row may already exist if this ran
        # against a half-migrated database, and the primary key would refuse.
        db.execute(
            """
            INSERT OR IGNORE INTO meeting_availability
                (meeting_id, moderator_id, on_date) VALUES (?, ?, ?)
            """,
            (meeting_id, moderator_id, slot),
        )
        db.execute(
            "DELETE FROM meeting_availability WHERE meeting_id = ? "
            "AND moderator_id = ? AND on_date = ?",
            (meeting_id, moderator_id, on_date),
        )


# `persons` columns that exist only to be carried across a table rebuild, in the
# order the rebuilt table declares them. Anything else the live table has (a
# _legacy column from an earlier migration) is copied too — see _relax_political_group.
PERSONS_REBUILD_SQL = """
    CREATE TABLE persons_rebuilt (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        contact_type    TEXT NOT NULL DEFAULT 'Politique',
        role            TEXT,
        political_group TEXT,
        phone           TEXT,
        social_links    TEXT,
        stance          TEXT NOT NULL,
        first_contacted TEXT,
        notes           TEXT,
        circonscription TEXT,
        email           TEXT,
        portefeuille    TEXT,
        religion        TEXT,
        territoire      TEXT,
        in_office       INTEGER NOT NULL DEFAULT 1,
        signed_declaration INTEGER NOT NULL DEFAULT 0,
        added_by        INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
        validated_by    INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
        created_at      TEXT NOT NULL
    )
"""


def _relax_political_group(db):
    """Drop the NOT NULL on `persons.political_group`, once, by rebuilding.

    The column was mandatory because every person was a politician. A
    journaliste has no groupe politique, and storing an empty string to satisfy
    the constraint would make "no group" and "group not filled in"
    indistinguishable — so the constraint goes instead.

    The column itself stays: `utils/insert_*.py` and `sync_officials.py` write
    it directly, and a groupe politique's name is exactly what they know. The
    app keeps it in step with the organisation links (see _sync_group_mirror),
    so nothing that reads it has to change.

    Runs inside one BEGIN EXCLUSIVE, so it either happens completely or not at
    all: an interrupted start leaves `persons` exactly as it was, and the next
    start simply tries again. That is what makes restarting the app safe without
    stopping the importers first — a concurrent writer waits for the lock (or
    fails loudly with "database is locked"), it never lands a write in a table
    that is about to be dropped.
    """
    notnull = [
        r[3] for r in db.execute("PRAGMA table_info(persons)")
        if r[1] == "political_group"
    ]
    if not notnull or not notnull[0]:
        return  # already nullable, or the column is gone
    old_cols = [r[1] for r in db.execute("PRAGMA table_info(persons)")]
    # Both pragmas have to be set outside a transaction: `foreign_keys` is a
    # no-op inside one, and this is exactly why it is needed — DROP TABLE
    # persons would otherwise cascade and take meeting_persons / mail_persons
    # rows with it.
    db.execute("PRAGMA foreign_keys = OFF")
    # Legacy mode keeps the rename from rewriting the REFERENCES clauses of
    # meeting_persons / mail_persons / person_organisations: they already name
    # `persons`, which is what the rebuilt table is about to be called.
    db.execute("PRAGMA legacy_alter_table = ON")
    # Scratch table from an earlier interrupted attempt. It never holds the live
    # rows — `persons` does, until the rename — so dropping it loses nothing,
    # and without this a half-finished run would make every later start fail
    # with "table persons_rebuilt already exists".
    db.execute("DROP TABLE IF EXISTS persons_rebuilt")
    # Manual transaction control: SQLite DDL is transactional, but
    # `executescript` would COMMIT first and break the atomicity we want here.
    db.isolation_level = None
    db.execute("BEGIN EXCLUSIVE")
    try:
        db.execute(PERSONS_REBUILD_SQL)
        new_cols = [r[1] for r in db.execute("PRAGMA table_info(persons_rebuilt)")]
        # Carried columns are the intersection, so a column this version does
        # not know about (a retired _legacy one) is not silently dropped: it is
        # added to the rebuilt table first, then copied.
        extra = [c for c in old_cols if c not in new_cols]
        for col in extra:
            db.execute(f"ALTER TABLE persons_rebuilt ADD COLUMN {col} TEXT")
        carried = [c for c in old_cols if c in new_cols or c in extra]
        cols = ", ".join(carried)
        db.execute(f"INSERT INTO persons_rebuilt ({cols}) SELECT {cols} FROM persons")
        db.execute("DROP TABLE persons")
        db.execute("ALTER TABLE persons_rebuilt RENAME TO persons")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.isolation_level = ""
        db.execute("PRAGMA legacy_alter_table = OFF")


# Which chambre a groupe politique sits in, derived from the institution it is
# already filed under in POLITICAL_GROUPS. A group absent from that map (typed
# by hand, or imported before it was listed) simply gets no chambre.
CHAMBRE_OF_GROUP = {
    name: chambre for chambre, names in POLITICAL_GROUPS.items() for name in names
}


def _seed_organisations_from_groups(db):
    """Give every politique an organisation row for their groupe politique.

    `persons.political_group` is a name typed into a column; an organisation is
    a record with a chambre, a position on PauseIA and a link. This turns each
    distinct name into the latter and links its people, so the 31 groups already
    in the table arrive as organisations rather than having to be re-entered.

    Idempotent, and safe to run after the fact: it only ever adds what is
    missing, which is what makes it the reconciliation step for rows inserted
    straight into `persons` by the utils/insert_*.py importers.
    """
    rows = db.execute(
        """
        SELECT p.id, p.political_group AS grp
          FROM persons p
         WHERE p.contact_type = 'Politique'
           AND COALESCE(TRIM(p.political_group), '') <> ''
           AND NOT EXISTS (
                 SELECT 1 FROM person_organisations po
                   JOIN organisations o ON o.id = po.organisation_id
                  WHERE po.person_id = p.id AND o.org_type = 'Groupe politique')
        """
    ).fetchall()
    if not rows:
        return
    known = {
        r[0]: r[1] for r in db.execute(
            "SELECT name, id FROM organisations WHERE org_type = 'Groupe politique'")
    }
    now = datetime.utcnow().isoformat(timespec="seconds")
    for person_id, grp in rows:
        grp = grp.strip()
        if grp not in known:
            cur = db.execute(
                """
                INSERT INTO organisations (name, org_type, chambre, stance, created_at)
                VALUES (?, 'Groupe politique', ?, 'Inconnu', ?)
                """,
                (grp, CHAMBRE_OF_GROUP.get(grp), now),
            )
            known[grp] = cur.lastrowid
        db.execute(
            "INSERT OR IGNORE INTO person_organisations (person_id, organisation_id) "
            "VALUES (?, ?)",
            (person_id, known[grp]),
        )


# --------------------------------------------------------------------------- #
# Auth (single shared password)
# --------------------------------------------------------------------------- #

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/favicon.ico")
def favicon():
    """Rediriger vers l'icône SVG.

    Les navigateurs qui ne lisent pas `<link rel="icon">` demandent
    `/favicon.ico` d'eux-mêmes, et prenaient un 404 à chaque page. Pas de
    `login_required` : l'icône s'affiche aussi sur l'écran de connexion.
    """
    return redirect(url_for("static", filename="favicon.svg"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Mot de passe incorrect.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Vous avez été déconnecté.", "success")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Shared form helpers (used by both the "new" and "edit" routes)
# --------------------------------------------------------------------------- #

def _to_iso(value):
    """Parse a date typed in French DD/MM/YYYY into ISO YYYY-MM-DD for storage.

    Returns (value, ok): an empty input is valid and yields ("", True). ISO
    input is also accepted, so the native picker keeps working. On a bad input
    the original text is returned with ok=False so it can be shown back.
    """
    value = (value or "").strip()
    if not value:
        return "", True
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d"), True
        except ValueError:
            continue
    return value, False


def _to_time(value):
    """Parse an optional time typed as HH:MM (24-hour). Returns (value, ok)."""
    value = (value or "").strip()
    if not value:
        return "", True
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M"), True
    except ValueError:
        return value, False


def _stage_upload(errors):
    """Validate an optional uploaded document without writing it to disk yet.

    Returns (file, stored_name, orig_name): the caller saves `file` to
    `stored_name` once all validation has passed. When no file was provided,
    or it is invalid, `stored_name`/`orig_name` are None.
    """
    file = request.files.get("document")
    if not (file and file.filename):
        return None, None, None
    orig_name = secure_filename(file.filename)
    ext = Path(orig_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        errors.append(
            "Le document doit être au format : "
            + ", ".join(sorted(ALLOWED_EXTENSIONS))
            + "."
        )
        return None, None, None
    # The extension alone is declarative; verify the actual content matches it
    # so a renamed executable (or any other disguised type) is rejected.
    head = file.stream.read(2048)
    file.stream.seek(0)
    mime = magic.from_buffer(head, mime=True)
    allowed = ALLOWED_MIMES[ext]
    if (allowed is None and not mime.startswith("text/")) or (
        allowed is not None and mime not in allowed
    ):
        errors.append("Le contenu du document ne correspond pas à son format.")
        return None, None, None
    return file, f"{uuid.uuid4().hex}{ext}", orig_name


def _delete_upload(stored_name):
    """Remove a stored upload from disk, ignoring a missing file."""
    if stored_name:
        (UPLOAD_DIR / stored_name).unlink(missing_ok=True)


# How far ahead the « Relance prévue » field is pre-filled on each form,
# counted from the date of the exchange itself (a mail dated the 1st proposes
# the 11th). Pre-filling is the point: an empty relance must mean someone
# decided against a follow-up, not that they forgot the field was there.
FOLLOW_UP_DEFAULT_DAYS = {"mail": 10, "meeting": 5}

# A rencontre should not be attended alone: fewer than this many utilisateurices
# signed up is flagged on /todo as a gap to fill.
MIN_PARTICIPANTS = 2

# How a rencontre took place. Stored as the key, displayed as the label.
MEETING_FORMATS = {"presentiel": "Présentiel", "visio": "Visio"}

# « Autres dates » offers this many alternatives on top of the main date, so a
# rencontre being scheduled has at most MAX_ALT_DATES + 1 candidate dates.
MAX_ALT_DATES = 3


def format_label(value):
    """« Présentiel » / « Visio », or « Non renseigné » for a pre-field row."""
    return MEETING_FORMATS.get(value or "", "Non renseigné")


def place_label(value):
    """What the free-text place field is called, which depends on the format.

    Présentiel asks where you went and insists on an answer; visio reuses the
    same column for the link or platform, which is a convenience, not a fact
    the record needs.
    """
    return "Lien / plateforme" if value == "visio" else "Lieu"


# A candidate slot — « créneau » — is a date, optionally with a time: either
# "2026-09-11" or "2026-09-11T14:00". One string, so `meetings.alt_dates` and
# `meeting_availability.on_date` keep exactly the shape they had, and every row
# written before times existed is simply a slot with no time. 'T' separates
# them because it makes the string sort in the same order as the moment does.
#
# Two slots on the same day is the point of the time: a rencontre can offer
# 11/09 at 10:00 and 11/09 at 14:00 and have people sign up for one or the
# other, which a bare date could not express.
SLOT_SEP = "T"


def make_slot(on_date, at_time=None):
    """A slot string from a date and an optional time."""
    return f"{on_date}{SLOT_SEP}{at_time}" if at_time else on_date


def split_slot(slot):
    """(date, time-or-None) for a slot. A bare date yields a None time."""
    on_date, _, at_time = (slot or "").partition(SLOT_SEP)
    return on_date, (at_time or None)


def slot_date(slot):
    """Just the day a slot falls on — for « is it past », and the calendar."""
    return split_slot(slot)[0]


def parse_alt_dates(value):
    """The stored comma-joined « Autres dates » back into a list of slots."""
    return [d for d in (value or "").split(",") if d.strip()]


def main_slot(meeting):
    """The rencontre's own date and time, as a slot."""
    return make_slot(meeting["meeting_date"], meeting["meeting_time"])


def candidate_slots(meeting):
    """Every slot a rencontre could land on, earliest first.

    The main date is candidate number one: it is NOT NULL and every other page
    orders by it, so a rencontre under arbitration keeps a real date throughout
    and validating one simply overwrites it.
    """
    return sorted({main_slot(meeting), *parse_alt_dates(meeting["alt_dates"])})


def _alt_dates_from_form(errors, meeting_date, meeting_time):
    """Read the « Autres dates » inputs. Returns the stored string, or None.

    Each alternative is a date plus an optional time, submitted as two parallel
    lists — so the same day can appear twice at different times, which is how a
    rencontre offers two slots on one date. Blanks, exact duplicates and a
    repeat of the main slot drop out: the field is a set of *other*
    possibilities, and re-listing the main one would show it twice on
    /repartition. The same date at a *different* time is not a duplicate.
    """
    main = make_slot(meeting_date, meeting_time)
    raw_dates = request.form.getlist("alt_dates")[:MAX_ALT_DATES]
    raw_times = request.form.getlist("alt_times")[:MAX_ALT_DATES]
    slots = []
    for i, raw in enumerate(raw_dates):
        iso, ok = _to_iso(raw)
        if not iso:
            continue
        if not ok:
            errors.append(f"La date alternative « {raw} » est invalide (format JJ/MM/AAAA).")
            continue
        at_time, time_ok = _to_time(raw_times[i] if i < len(raw_times) else "")
        if not time_ok:
            errors.append(
                f"L'heure de la date alternative « {raw} » est invalide "
                "(format HH:MM)."
            )
            continue
        slot = make_slot(iso, at_time)
        if slot != main and slot not in slots:
            slots.append(slot)
    return ",".join(sorted(slots)) or None


def _is_autosave():
    """True when the request came from the page's auto-save rather than a click.

    The availability grid and the sign-up checklist save themselves as soon as a
    box is ticked (see static/form-masks.js). Those requests want no redirect
    and no flash — the page is already showing the new state, having computed
    the counts itself. A plain form submit (no JavaScript) is unaffected and
    still redirects with a confirmation.
    """
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _set_meeting_availability(db, meeting_id, rows):
    """Replace a meeting's availability rows with `rows` — (moderator_id, date)."""
    db.execute("DELETE FROM meeting_availability WHERE meeting_id = ?", (meeting_id,))
    db.executemany(
        "INSERT INTO meeting_availability (meeting_id, moderator_id, on_date) "
        "VALUES (?, ?, ?)",
        [(meeting_id, mid, on_date) for mid, on_date in rows],
    )


def _prune_availability(db, meeting_id, kept_dates):
    """Drop availability for dates a rencontre no longer offers.

    Editing a rencontre can remove a candidate date; the sign-ups made for it
    would otherwise linger invisibly and come back if the date were re-added.
    """
    rows = db.execute(
        "SELECT on_date FROM meeting_availability WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()
    stale = {r["on_date"] for r in rows} - set(kept_dates)
    for on_date in stale:
        db.execute(
            "DELETE FROM meeting_availability WHERE meeting_id = ? AND on_date = ?",
            (meeting_id, on_date),
        )


def person_follow_up_sql(alias="p"):
    """SQL scalar subquery giving a person's « Relance prévue ».

    The value is never stored on `persons`: it mirrors the follow-up of that
    person's most recent interaction — the linked Rencontre or Courriel with
    the latest date, ties broken by whichever was entered last. So logging a
    newer exchange replaces the relance, and a follow-up attached to an old
    interaction stops driving the person once it has been superseded.

    If that latest interaction has no relance of its own the person has none:
    the most recent contact deliberately scheduled no follow-up, and reaching
    further back would resurrect a date the newer exchange already answered.

    `alias` is the outer query's name for the persons table. It is always a
    literal from our own call sites, never user input.
    """
    return f"""(
        SELECT i.follow_up_date FROM (
            SELECT me.meeting_date AS on_date,
                   me.created_at   AS entered_at,
                   me.follow_up_date
              FROM meetings me
              JOIN meeting_persons mp ON mp.meeting_id = me.id
             WHERE mp.person_id = {alias}.id
            UNION ALL
            SELECT ma.mail_date, ma.created_at, ma.follow_up_date
              FROM mails ma
              JOIN mail_persons xp ON xp.mail_id = ma.id
             WHERE xp.person_id = {alias}.id
        ) AS i
        ORDER BY i.on_date DESC, i.entered_at DESC
        LIMIT 1
    )"""


def default_follow_up(kind, on_date):
    """The pre-filled relance for a new record: `on_date` + the kind's delay.

    Returns "" when the date is unusable, so the form simply renders empty
    rather than guessing from today — the caller's date field is the anchor.
    """
    try:
        base = date.fromisoformat(on_date)
    except (TypeError, ValueError):
        return ""
    return (base + timedelta(days=FOLLOW_UP_DEFAULT_DAYS[kind])).isoformat()


def _set_person_links(db, table, key_col, key_id, person_ids):
    """Replace the person links for a meeting/mail with `person_ids`."""
    db.execute(f"DELETE FROM {table} WHERE {key_col} = ?", (key_id,))
    db.executemany(
        f"INSERT INTO {table} ({key_col}, person_id) VALUES (?, ?)",
        [(key_id, pid) for pid in person_ids],
    )


def _form_from_row(row):
    """Turn a DB row into a form dict, mapping NULLs to empty strings so the
    template's value="{{ ... }}" attributes don't render the literal 'None'."""
    return {k: ("" if v is None else v) for k, v in dict(row).items()}


def _moderators(db):
    """All certified users, for the provenance dropdowns on the real forms."""
    return db.execute(
        "SELECT id, name FROM moderators ORDER BY name COLLATE NOCASE"
    ).fetchall()


def _valid_moderator(db, value):
    """Return the int id if `value` names an existing moderator, else None."""
    value = (value or "").strip()
    if value.isdigit() and db.execute(
        "SELECT 1 FROM moderators WHERE id = ?", (int(value),)
    ).fetchone():
        return int(value)
    return None


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def _to_url(value):
    """Normalise a link typed into a form. Returns (value, ok).

    An empty input is valid and yields ("", True). A bare « www.lemonde.fr/… »
    gets https:// in front, since that is what people paste. Any other scheme
    (javascript:, data:, …) is refused: the value ends up in an href.
    """
    value = (value or "").strip()
    if not value:
        return "", True
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return value, True
    if "://" not in value and ":" not in value.split("/", 1)[0] and "." in value:
        return "https://" + value, True
    return value, False


def _set_links(db, table, key_col, key_id, other_col, other_ids):
    """Replace the rows of a join table for one record with `other_ids`.

    `table` and the column names are always literals from our own call sites.
    """
    db.execute(f"DELETE FROM {table} WHERE {key_col} = ?", (key_id,))
    db.executemany(
        f"INSERT INTO {table} ({key_col}, {other_col}) VALUES (?, ?)",
        [(key_id, oid) for oid in other_ids],
    )


def _ids_from_form(db, field, table):
    """The ids ticked in a checkbox group, kept only if they exist in `table`."""
    valid = {str(r[0]) for r in db.execute(f"SELECT id FROM {table}")}
    return [int(v) for v in dict.fromkeys(request.form.getlist(field)) if v in valid]


def _moderator_name(db, moderator_id):
    row = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (moderator_id,)
    ).fetchone()
    return row["name"] if row else None


def _valid_organisation(db, value):
    """Return the int id if `value` names an existing organisation, else None."""
    value = (value or "").strip()
    if value.isdigit() and db.execute(
        "SELECT 1 FROM organisations WHERE id = ?", (int(value),)
    ).fetchone():
        return int(value)
    return None


def _organisation_choices(db, org_type=None):
    """Organisations for a picker, optionally of one type only.

    Rows carry `org_type` so a template rendering all of them can group or
    filter client-side — which is how the person form switches its picker when
    the type de contact changes without a round trip.
    """
    sql = ("SELECT id, name, org_type, media_type, chambre FROM organisations")
    params = ()
    if org_type is not None:
        sql += " WHERE org_type = ?"
        params = (org_type,)
    return db.execute(sql + " ORDER BY name COLLATE NOCASE", params).fetchall()


def _person_choices(db):
    """Every person with their organisations and genres, for the pickers.

    `genres` is what the « Genre » select on the intervention and contenu forms
    filters on — see person_genres.
    """
    rows = db.execute(
        """
        SELECT p.id, p.name, p.contact_type,
               (SELECT GROUP_CONCAT(o.name, ', ')
                  FROM person_organisations po
                  JOIN organisations o ON o.id = po.organisation_id
                 WHERE po.person_id = p.id) AS organisation_names,
               (SELECT GROUP_CONCAT(o.org_type, '|')
                  FROM person_organisations po
                  JOIN organisations o ON o.id = po.organisation_id
                 WHERE po.person_id = p.id) AS org_types
        FROM persons p
        ORDER BY name_key(p.name)
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "contact_type": r["contact_type"],
            "organisation_names": r["organisation_names"],
            "genres": person_genres(r["contact_type"], r["org_types"]),
        }
        for r in rows
    ]


def _persons_of(db, join_table, key_col, key_id):
    """The people linked to one record, for its detail page."""
    return db.execute(
        f"""
        SELECT p.id, p.name, p.contact_type, p.role, p.stance,
               (SELECT GROUP_CONCAT(o.name, ', ')
                  FROM person_organisations po
                  JOIN organisations o ON o.id = po.organisation_id
                 WHERE po.person_id = p.id) AS organisation_names
        FROM persons p
        JOIN {join_table} l ON l.person_id = p.id
        WHERE l.{key_col} = ?
        ORDER BY name_key(p.name)
        """,
        (key_id,),
    ).fetchall()


def _moderator_names_of(db, join_table, key_col, key_id):
    """The utilisateurices linked to one record, for its detail page."""
    return [
        r["name"] for r in db.execute(
            f"""
            SELECT mo.name FROM moderators mo
            JOIN {join_table} l ON l.moderator_id = mo.id
            WHERE l.{key_col} = ?
            ORDER BY mo.name COLLATE NOCASE
            """,
            (key_id,),
        )
    ]


def _link_persons_to_organisation(db, person_ids, organisation_id):
    """Record that these people belong to this organisation, where they can.

    A contenu or an intervention naming a person and an organisation is
    evidence the two are linked, so saving one adds the missing rows — but only
    for the people whose type matches the organisation's. A journaliste
    interviewed on a party's own channel is not thereby a member of that party,
    and an élu·e quoted in Le Monde does not work there; inferring either would
    quietly rewrite the fiche. Those links stay for someone to make by hand.

    Existing links are left alone.
    """
    row = db.execute(
        "SELECT org_type FROM organisations WHERE id = ?", (organisation_id,)
    ).fetchone()
    if row is None:
        return
    wanted = CONTACT_TYPE_BY_ORG_TYPE.get(row["org_type"])
    matching = [
        pid for pid in person_ids
        if db.execute("SELECT contact_type FROM persons WHERE id = ?",
                      (pid,)).fetchone()["contact_type"] == wanted
    ]
    db.executemany(
        "INSERT OR IGNORE INTO person_organisations (person_id, organisation_id) "
        "VALUES (?, ?)",
        [(pid, organisation_id) for pid in matching],
    )
    _sync_group_mirror(db, matching)


def _sync_group_mirror(db, person_ids):
    """Keep `persons.political_group` in step with the linked groupe politique.

    The column is the backward-compatible face of the organisation link: the
    élu·e importers in utils/ write it, and export_contacts_xlsx and any ad-hoc
    SQL still read it. So whenever the links of a politique change, the name of
    their groupe politique is written back here — alphabetically first when
    somebody belongs to two, since the column holds one value and any choice
    beyond "a group they are in" would be arbitrary.

    A journaliste's mirror is cleared: they have no groupe politique, and a
    leftover value would put them in one on every page still reading the column.
    """
    for person_id in person_ids:
        # Only a politique has a groupe politique. Restricting the lookup by
        # contact_type is what keeps a journaliste's mirror NULL even if they
        # end up linked to a group some other way — by an intervention on a
        # party's channel, say, or by a link made before their type was fixed.
        row = db.execute(
            """
            SELECT o.name FROM persons p
              JOIN person_organisations po ON po.person_id = p.id
              JOIN organisations o         ON o.id = po.organisation_id
             WHERE p.id = ? AND p.contact_type = 'Politique'
               AND o.org_type = 'Groupe politique'
             ORDER BY o.name COLLATE NOCASE LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        db.execute(
            "UPDATE persons SET political_group = ? WHERE id = ?",
            (row["name"] if row else None, person_id),
        )


def _match_proposed_people(db, proposed):
    """Map a declarant's free-text « Personnes concernées » onto persons rows."""
    return _match_names(db, "persons", proposed)


def _match_names(db, table, proposed):
    """Map a declarant's free-text, comma-separated names onto rows of `table`.

    Returns (ids, unmatched): the string ids to tick in the approval form, and
    the names that matched nothing so the moderator can be told rather than
    left to spot the gap. A name the anonymous picker inserted matches exactly;
    one typed by hand may not, which is why the leftovers are surfaced.

    Comparison is on the casefolded, whitespace-collapsed name. Python's
    casefold is used rather than SQL COLLATE NOCASE because the latter is
    ASCII-only in SQLite and would miss accented names — most of this table.

    `table` is always a literal from our own call sites, never user input.
    """
    def key(name):
        return " ".join(name.split()).casefold()

    by_name = {}
    for row in db.execute(f"SELECT id, name FROM {table}"):
        # First row wins: two people sharing a name can't be told apart from a
        # bare string, so the moderator confirms that case by hand.
        by_name.setdefault(key(row["name"]), str(row["id"]))

    ids, unmatched = set(), []
    for raw in (proposed or "").split(","):
        name = raw.strip()
        if not name:
            continue
        found = by_name.get(key(name))
        if found:
            ids.add(found)
        else:
            unmatched.append(name)
    return ids, unmatched


def _submitted_by_moderator(db, submitted_by):
    """The declarant as an utilisateurice id, when their name matches one.

    « Saisi par » (and « Qui a ajouté la personne ? », « Qui a reçu / envoyé le
    mail ? ») name the utilisateurice the record came from — the declarant —
    not whoever is validating it. So they are filled from the declaration
    itself. « Validé par » is the one field that belongs to the person
    validating, and is left alone here.

    Returns the id as a string, or None when the declaration was signed with a
    pseudo, with « anonyme », or by someone outside the team — in which case
    the moderator picks the right utilisateurice by hand.
    """
    name = " ".join((submitted_by or "").split())
    if not name or name.casefold() == "anonyme":
        return None
    for row in db.execute("SELECT id, name FROM moderators"):
        if " ".join(row["name"].split()).casefold() == name.casefold():
            return str(row["id"])
    return None


def _set_meeting_moderators(db, meeting_id, moderator_ids):
    """Replace a meeting's participant links with `moderator_ids`."""
    db.execute("DELETE FROM meeting_moderators WHERE meeting_id = ?", (meeting_id,))
    db.executemany(
        "INSERT INTO meeting_moderators (meeting_id, moderator_id) VALUES (?, ?)",
        [(meeting_id, mid) for mid in moderator_ids],
    )


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #

@app.route("/")
@login_required
def index():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    base = """
        SELECT m.*,
               GROUP_CONCAT(p.name, ', ')            AS person_names,
               -- The organisations of everyone met: médias and groupes
               -- politiques alike, deduplicated, '|' so a name containing a
               -- comma still splits correctly in the template.
               (SELECT GROUP_CONCAT(name, '|') FROM (
                   SELECT DISTINCT o.name
                     FROM meeting_persons mp2
                     JOIN person_organisations po ON po.person_id = mp2.person_id
                     JOIN organisations o         ON o.id = po.organisation_id
                    WHERE mp2.meeting_id = m.id)) AS organisation_names,
               -- Emails of the people met, so the list can offer them directly.
               -- NULLIF keeps a person with no address from contributing an
               -- empty entry that would render as a stray separator.
               GROUP_CONCAT(NULLIF(p.email, ''), ', ') AS person_emails
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
    """
    if q:
        like = f"%{q}%"
        meetings = db.execute(
            base
            + """
            WHERE m.id IN (
                SELECT m2.id FROM meetings m2
                LEFT JOIN meeting_persons mp2 ON mp2.meeting_id = m2.id
                LEFT JOIN persons p2          ON p2.id = mp2.person_id
                WHERE p2.name LIKE ? OR m2.summary LIKE ?
                   OR p2.id IN (SELECT po.person_id FROM person_organisations po
                                JOIN organisations o ON o.id = po.organisation_id
                                WHERE o.name LIKE ?)
            )
            GROUP BY m.id
            ORDER BY m.meeting_date DESC, m.id DESC
            """,
            (like, like, like),
        ).fetchall()
    else:
        meetings = db.execute(
            base + " GROUP BY m.id ORDER BY m.meeting_date DESC, m.id DESC"
        ).fetchall()
    # Split into upcoming (today included) and past, each in natural reading
    # order: soonest-first for what's coming, most-recent-first for history.
    today_iso = date.today().isoformat()
    upcoming = sorted(
        (m for m in meetings if m["meeting_date"] >= today_iso),
        key=lambda m: (m["meeting_date"], m["id"]),
    )
    past = sorted(
        (m for m in meetings if m["meeting_date"] < today_iso),
        key=lambda m: (m["meeting_date"], m["id"]),
        reverse=True,
    )
    return render_template("index.html", upcoming=upcoming, past=past, q=q)


def _save_meeting(db, meeting):
    """Validate the meeting form and insert/update. Returns (meeting_id, errors).

    `meeting` is the existing row when editing, or None when creating.
    """
    people = db.execute(
        "SELECT id FROM persons ORDER BY name_key(name)"
    ).fetchall()
    valid_ids = {str(p["id"]) for p in people}

    mod_ids = {str(m["id"]) for m in _moderators(db)}

    meeting_date, date_ok = _to_iso(request.form.get("meeting_date"))
    meeting_time, time_ok = _to_time(request.form.get("meeting_time"))
    meeting_format = (request.form.get("meeting_format") or "").strip()
    meeting_place = (request.form.get("meeting_place") or "").strip()
    summary = (request.form.get("summary") or "").strip()
    follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
    recorded_by = _valid_moderator(db, request.form.get("recorded_by"))
    person_ids = [pid for pid in request.form.getlist("person_ids") if pid in valid_ids]
    participant_ids = [m for m in request.form.getlist("participant_ids") if m in mod_ids]
    validated_by = _valid_moderator(db, request.form.get("validated_by"))
    remove_doc = bool(request.form.get("remove_document"))

    errors = []
    if not person_ids:
        errors.append("Sélectionnez au moins une personne.")
    if not participant_ids:
        errors.append("Indiquez qui a/va participé.er à la rencontre.")
    if validated_by is None:
        errors.append("Indiquez qui a validé la rencontre.")
    if recorded_by is None:
        errors.append("Indiquez qui a saisi la rencontre.")
    if not meeting_date:
        errors.append("La date de la rencontre est obligatoire.")
    elif not date_ok:
        errors.append("La date de la rencontre est invalide (format JJ/MM/AAAA).")
    if not time_ok:
        errors.append("L'heure de la rencontre est invalide (format HH:MM).")
    # Required on this form even though the column is nullable: a rencontre
    # saved from here always states its format, while the rows that predate the
    # field keep their honest NULL rather than being backfilled with a guess.
    if meeting_format not in MEETING_FORMATS:
        errors.append("Indiquez si la rencontre est en présentiel ou en visio.")
    elif meeting_format == "presentiel" and not meeting_place:
        errors.append("Indiquez le lieu de la rencontre en présentiel.")
    if not summary:
        errors.append("Un bref résumé est obligatoire.")
    if not fu_ok:
        errors.append("La date de relance est invalide (format JJ/MM/AAAA).")
    alt_dates = _alt_dates_from_form(errors, meeting_date, meeting_time)
    # A répartition is a rencontre whose date is still open, so it needs at
    # least one alternative — without one it would be an ordinary rencontre and
    # would never appear on /repartition, which is not what was asked for.
    if request.form.get("repartition") and not alt_dates:
        errors.append(
            "Une répartition doit proposer au moins une autre date possible, "
            "en plus de la date principale."
        )
    file, stored_name, orig_name = _stage_upload(errors)

    if errors:
        return None, errors

    person_id_ints = [int(pid) for pid in person_ids]
    participant_id_ints = [int(m) for m in participant_ids]

    if meeting is None:
        cur = db.execute(
            """
            INSERT INTO meetings (
                meeting_date, meeting_time, meeting_format, meeting_place,
                alt_dates, summary, follow_up_date,
                recorded_by, validated_by,
                document_stored_name, document_orig_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_date, meeting_time or None, meeting_format,
             meeting_place or None, alt_dates, summary,
             follow_up_date or None,
             recorded_by, validated_by, stored_name, orig_name,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        meeting_id = cur.lastrowid
    else:
        meeting_id = meeting["id"]
        # Decide what happens to the attached document.
        new_stored, new_orig = meeting["document_stored_name"], meeting["document_orig_name"]
        if stored_name:                       # a new file replaces the old one
            _delete_upload(meeting["document_stored_name"])
            new_stored, new_orig = stored_name, orig_name
        elif remove_doc:                      # explicit removal, no replacement
            _delete_upload(meeting["document_stored_name"])
            new_stored, new_orig = None, None
        db.execute(
            """
            UPDATE meetings SET meeting_date = ?, meeting_time = ?,
                meeting_format = ?, meeting_place = ?, alt_dates = ?, summary = ?,
                follow_up_date = ?, recorded_by = ?, validated_by = ?,
                document_stored_name = ?, document_orig_name = ? WHERE id = ?
            """,
            (meeting_date, meeting_time or None, meeting_format,
             meeting_place or None, alt_dates, summary,
             follow_up_date or None,
             recorded_by, validated_by, new_stored, new_orig, meeting_id),
        )
        # The candidate slots may have changed under the sign-ups made for them.
        _prune_availability(
            db, meeting_id,
            [make_slot(meeting_date, meeting_time), *parse_alt_dates(alt_dates)]
        )

    if file and stored_name:
        file.save(UPLOAD_DIR / stored_name)
    _set_person_links(db, "meeting_persons", "meeting_id", meeting_id, person_id_ints)
    _set_meeting_moderators(db, meeting_id, participant_id_ints)
    # A rencontre entering arbitration starts with the people already on it
    # pencilled in for every candidate date. The form insists on at least one
    # participant, so without this /repartition would open showing nobody
    # signed up anywhere — red on every date — while the rencontre does in fact
    # have someone attached. It is a starting point, not a claim: the grid is
    # there to be corrected. Only ever seeded once, so a later edit of the
    # rencontre never overwrites what people have since ticked.
    if alt_dates and not db.execute(
        "SELECT 1 FROM meeting_availability WHERE meeting_id = ?", (meeting_id,)
    ).fetchone():
        _set_meeting_availability(
            db,
            meeting_id,
            [(mid, slot)
             for mid in participant_id_ints
             for slot in [make_slot(meeting_date, meeting_time),
                          *parse_alt_dates(alt_dates)]],
        )
    db.commit()
    return meeting_id, []


@app.route("/meetings/new", methods=["GET", "POST"])
@login_required
def new_meeting():
    db = get_db()
    people = _person_choices(db)
    mods = _moderators(db)
    # « + Nouvelle répartition » on /repartition opens this same form in
    # répartition mode: what makes a rencontre a répartition is simply that its
    # date is not settled, i.e. that it carries « Autres dates possibles ». So
    # the mode asks for at least one of those, names itself accordingly, and
    # sends you back to /repartition rather than to the rencontre's own page.
    # Carried through the POST in a hidden field, so a validation error keeps it.
    mode = request.values.get("repartition")
    heading = "Nouvelle répartition" if mode else "Nouvelle rencontre"
    cancel_url = url_for("repartition") if mode else url_for("index")
    ctx = dict(people=people, moderators=mods, current=None,
               today=date.today().isoformat(), action_url=url_for("new_meeting"),
               heading=heading, cancel_url=cancel_url, repartition=bool(mode))

    if request.method == "POST":
        meeting_id, errors = _save_meeting(db, None)
        if not errors:
            if mode:
                flash("Répartition ajoutée : renseignez les disponibilités.", "success")
                return redirect(url_for("repartition"))
            flash("Rencontre ajoutée.", "success")
            return redirect(url_for("meeting_detail", meeting_id=meeting_id))
        for e in errors:
            flash(e, "error")
        return (
            render_template(
                "new_meeting.html", form=request.form,
                selected_ids=set(request.form.getlist("person_ids")),
                selected_mods=set(request.form.getlist("participant_ids")),
                **ctx,
            ),
            400,
        )

    # Arriving from a person's page pre-ticks them, the same way the contenu
    # and intervention forms already do.
    selected = {request.args["person_id"]} if request.args.get("person_id") else set()
    return render_template(
        "new_meeting.html", form={}, selected_ids=selected, selected_mods=set(), **ctx,
    )


@app.route("/meetings/<int:meeting_id>/edit", methods=["GET", "POST"])
@login_required
def edit_meeting(meeting_id):
    db = get_db()
    meeting = db.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        abort(404)
    people = _person_choices(db)
    mods = _moderators(db)
    linked = {
        str(r["person_id"])
        for r in db.execute(
            "SELECT person_id FROM meeting_persons WHERE meeting_id = ?", (meeting_id,)
        )
    }
    linked_mods = {
        str(r["moderator_id"])
        for r in db.execute(
            "SELECT moderator_id FROM meeting_moderators WHERE meeting_id = ?", (meeting_id,)
        )
    }

    if request.method == "POST":
        _, errors = _save_meeting(db, meeting)
        if not errors:
            flash("Rencontre mise à jour.", "success")
            return redirect(url_for("meeting_detail", meeting_id=meeting_id))
        for e in errors:
            flash(e, "error")
        return (
            render_template(
                "new_meeting.html", people=people, moderators=mods, form=request.form,
                selected_ids=set(request.form.getlist("person_ids")),
                selected_mods=set(request.form.getlist("participant_ids")),
                current=meeting, today=date.today().isoformat(),
                action_url=url_for("edit_meeting", meeting_id=meeting_id),
                heading="Modifier la rencontre",
                cancel_url=url_for("meeting_detail", meeting_id=meeting_id),
            ),
            400,
        )

    return render_template(
        "new_meeting.html", people=people, moderators=mods,
        form=_form_from_row(meeting), selected_ids=linked, selected_mods=linked_mods,
        current=meeting, today=date.today().isoformat(),
        action_url=url_for("edit_meeting", meeting_id=meeting_id),
        heading="Modifier la rencontre",
        cancel_url=url_for("meeting_detail", meeting_id=meeting_id),
    )


@app.route("/meetings/<int:meeting_id>/delete", methods=["POST"])
@login_required
def delete_meeting(meeting_id):
    db = get_db()
    meeting = db.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        abort(404)
    _delete_upload(meeting["document_stored_name"])
    # meeting_persons / meeting_moderators are ON DELETE CASCADE.
    db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    db.commit()
    flash("Rencontre supprimée.", "success")
    return redirect(url_for("index"))


@app.route("/meetings/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):
    db = get_db()
    meeting = db.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        abort(404)
    people = _persons_of(db, "meeting_persons", "meeting_id", meeting_id)
    participants = db.execute(
        """
        SELECT mo.name FROM moderators mo
        JOIN meeting_moderators mm ON mm.moderator_id = mo.id
        WHERE mm.meeting_id = ?
        ORDER BY mo.name COLLATE NOCASE
        """,
        (meeting_id,),
    ).fetchall()
    validator = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (meeting["validated_by"],)
    ).fetchone()
    recorder = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (meeting["recorded_by"],)
    ).fetchone()
    return render_template(
        "detail.html", m=meeting, people=people,
        participants=[r["name"] for r in participants],
        validated_by=validator["name"] if validator else None,
        recorded_by=recorder["name"] if recorder else None,
        candidates=candidate_slots(meeting) if meeting["alt_dates"] else [],
        availability=_availability(db, [meeting]).get(meeting["id"], {}),
    )


@app.route("/uploads/<int:meeting_id>")
@login_required
def download(meeting_id):
    db = get_db()
    meeting = db.execute(
        "SELECT document_stored_name, document_orig_name FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if meeting is None or not meeting["document_stored_name"]:
        abort(404)
    return send_from_directory(
        UPLOAD_DIR,
        meeting["document_stored_name"],
        as_attachment=True,
        download_name=meeting["document_orig_name"],
    )


# --------------------------------------------------------------------------- #
# TODO — what has to happen today
# --------------------------------------------------------------------------- #

# One person's latest interaction, with the relance it carries and whether that
# relance has been ticked off. Same rule as person_follow_up_sql, but it also
# returns *which* record the relance came from, because /todo ticks the box on
# that record. The window function picks one row per person.
LATEST_INTERACTION_SQL = """
    SELECT * FROM (
        SELECT i.*, ROW_NUMBER() OVER (
                   PARTITION BY i.person_id
                   ORDER BY i.on_date DESC, i.entered_at DESC
               ) AS rn
        FROM (
            SELECT mp.person_id, 'meeting' AS kind, me.id AS rec_id,
                   me.meeting_date AS on_date, me.created_at AS entered_at,
                   me.follow_up_date, me.follow_up_done, me.follow_up_done_at,
                   me.summary
              FROM meetings me
              JOIN meeting_persons mp ON mp.meeting_id = me.id
            UNION ALL
            SELECT xp.person_id, 'mail', ma.id, ma.mail_date, ma.created_at,
                   ma.follow_up_date, ma.follow_up_done, ma.follow_up_done_at,
                   ma.summary
              FROM mails ma
              JOIN mail_persons xp ON xp.mail_id = ma.id
        ) AS i
    ) WHERE rn = 1
"""


def last_sender_sql(alias="p"):
    """SQL scalar subquery: who last wrote to this person on PauseIA's behalf.

    The « Qui a reçu / envoyé le courriel » of the most recent courriel envoyé
    linked to them. A courriel citoyen imported from the campagne mailbox has
    that field blank (and « Validé par » too), so requiring it is what keeps a
    citizen's mail from nominating anyone. NULL when nobody here has ever
    written to them, which /todo shows as « quiconque ».

    `alias` is always a literal from our own call sites, never user input.
    """
    return f"""(
        SELECT mo.name
          FROM mails ma
          JOIN mail_persons xp ON xp.mail_id = ma.id
          JOIN moderators mo ON mo.id = ma.received_by
         WHERE xp.person_id = {alias}.id AND ma.direction = 'sent'
         ORDER BY ma.mail_date DESC, ma.id DESC
         LIMIT 1
    )"""


# How many courriels PauseIA has sent to a person. Only the ones an
# utilisateurice stands behind count: a courriel citoyen imported from the
# campagne mailbox has no « Qui a reçu / envoyé » and no « Validé par », so it
# says nothing about how much *we* have written to them, which is what the
# « À contacter » list shows next to each person.
PAUSEIA_MAIL_COUNT_SQL = """(
    SELECT COUNT(*)
      FROM mails ma
      JOIN mail_persons xp ON xp.mail_id = ma.id
     WHERE xp.person_id = p.id AND ma.direction = 'sent'
       AND ma.received_by IS NOT NULL
)"""


@app.route("/todo")
@login_required
def todo():
    """Everything that needs doing today, in three blocks.

    Rencontres happening today; relances that have come due (today or earlier);
    and upcoming rencontres that still need people signed up for them.
    """
    db = get_db()
    today_iso = date.today().isoformat()

    meetings_today = db.execute(
        """
        SELECT m.*,
               GROUP_CONCAT(p.name, ', ')             AS person_names,
               GROUP_CONCAT(NULLIF(p.email, ''), ', ') AS person_emails,
               (SELECT GROUP_CONCAT(mo.name, ', ')
                  FROM meeting_moderators mm
                  JOIN moderators mo ON mo.id = mm.moderator_id
                 WHERE mm.meeting_id = m.id)          AS participant_names
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
        -- alt_dates IS NULL means the date is settled. A rencontre still under
        -- arbitration is on /repartition and nowhere else: it has no date yet
        -- worth calling "today", and taking sign-ups in two places would make
        -- the same checkbox mean two different things.
        WHERE m.meeting_date = ? AND m.done = 0 AND m.alt_dates IS NULL
        GROUP BY m.id
        ORDER BY COALESCE(m.meeting_time, '99:99'), m.id
        """,
        (today_iso,),
    ).fetchall()

    # Relances that have come due. A relance belongs to a record (the person's
    # latest interaction), and several people can share that record, so the
    # rows are grouped by record: one task, ticked once, not once per person.
    due = db.execute(
        f"""
        SELECT l.kind, l.rec_id, l.on_date, l.follow_up_date, l.follow_up_done,
               l.follow_up_done_at, l.summary,
               GROUP_CONCAT(p.name, ', ')             AS person_names,
               GROUP_CONCAT(NULLIF(p.email, ''), ', ') AS person_emails,
               -- Whoever wrote to them last is the one to relaunch them; the
               -- template says « quiconque » when this comes back empty.
               GROUP_CONCAT(DISTINCT {last_sender_sql("p")}) AS relance_by
        FROM ({LATEST_INTERACTION_SQL}) AS l
        JOIN persons p ON p.id = l.person_id
        WHERE l.follow_up_date IS NOT NULL AND l.follow_up_date <= ?
          -- Ticked means done: it leaves this page immediately and lives on
          -- /fait from then on. `follow_up_done_at` is still recorded, so /fait
          -- can say when it was done.
          AND l.follow_up_done = 0
        GROUP BY l.kind, l.rec_id
        ORDER BY l.follow_up_date, l.kind, l.rec_id
        """,
        (today_iso,),
    ).fetchall()

    # Upcoming rencontres and who is signed up. Two people is the target, so the
    # template colours anything short of that as a gap to fill.
    upcoming = db.execute(
        """
        SELECT m.*,
               GROUP_CONCAT(p.name, ', ') AS person_names,
               (SELECT COUNT(*) FROM meeting_moderators mm
                 WHERE mm.meeting_id = m.id) AS participant_count
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
        WHERE m.meeting_date > ? AND m.done = 0 AND m.alt_dates IS NULL
        GROUP BY m.id
        ORDER BY m.meeting_date, COALESCE(m.meeting_time, '99:99'), m.id
        """,
        (today_iso,),
    ).fetchall()
    signed_up = {
        m["id"]: {
            str(r[0])
            for r in db.execute(
                "SELECT moderator_id FROM meeting_moderators WHERE meeting_id = ?",
                (m["id"],),
            )
        }
        for m in upcoming
    }

    # One block per utilisateurice; the rows arrive sorted that way, with the
    # « Quiconque » (NULL) last (see _to_contact_rows).
    to_contact = _to_contact_rows(db, done=0)
    to_contact_blocks = []
    for r in to_contact:
        if not to_contact_blocks or to_contact_blocks[-1][0] != r["assigned_to_name"]:
            to_contact_blocks.append((r["assigned_to_name"], []))
        to_contact_blocks[-1][1].append(r)

    return render_template(
        "todo.html",
        meetings_today=meetings_today,
        due=due,
        to_contact=to_contact,
        to_contact_blocks=to_contact_blocks,
        upcoming=upcoming,
        signed_up=signed_up,
        moderators=_moderators(db),
        today=today_iso,
        min_participants=MIN_PARTICIPANTS,
        to_schedule=db.execute(
            "SELECT COUNT(*) FROM meetings WHERE alt_dates IS NOT NULL AND done = 0"
        ).fetchone()[0],
    )


@app.route("/fait")
@login_required
def fait():
    """Everything ticked off, newest first.

    /todo only keeps a ticked item until the end of the day it was ticked; this
    is where it goes afterwards, so nothing is lost — the day's list stays short
    without the record of what was done disappearing with it.
    """
    db = get_db()

    meetings_done = db.execute(
        """
        SELECT m.*, GROUP_CONCAT(p.name, ', ') AS person_names
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
        WHERE m.done = 1
        GROUP BY m.id
        ORDER BY COALESCE(m.done_at, m.meeting_date) DESC, m.id DESC
        """
    ).fetchall()

    # Ticked relances, listed from the record that carries them. Unlike /todo
    # this does not filter on "still the person's latest interaction": once a
    # relance has been dealt with, that it was later superseded is beside the
    # point — it still happened.
    relances_done = db.execute(
        """
        SELECT * FROM (
            SELECT 'meeting' AS kind, me.id AS rec_id, me.meeting_date AS on_date,
                   me.follow_up_date, me.follow_up_done_at AS done_at, me.summary,
                   (SELECT GROUP_CONCAT(p.name, ', ')
                      FROM meeting_persons mp JOIN persons p ON p.id = mp.person_id
                     WHERE mp.meeting_id = me.id) AS person_names
              FROM meetings me WHERE me.follow_up_done = 1
            UNION ALL
            SELECT 'mail', ma.id, ma.mail_date, ma.follow_up_date,
                   ma.follow_up_done_at, COALESCE(ma.subject, ma.summary),
                   (SELECT GROUP_CONCAT(p.name, ', ')
                      FROM mail_persons xp JOIN persons p ON p.id = xp.person_id
                     WHERE xp.mail_id = ma.id)
              FROM mails ma WHERE ma.follow_up_done = 1
        )
        ORDER BY COALESCE(done_at, follow_up_date) DESC, kind, rec_id DESC
        """
    ).fetchall()

    return render_template(
        "fait.html",
        meetings_done=meetings_done,
        relances_done=relances_done,
        contacted=_to_contact_rows(db, done=1),
        today=date.today().isoformat(),
    )


@app.route("/todo/rencontre/<int:meeting_id>/done", methods=["POST"])
@login_required
def toggle_meeting_done(meeting_id):
    db = get_db()
    db.execute(
        "UPDATE meetings SET done = 1 - done, "
        "done_at = CASE WHEN done = 0 THEN ? ELSE NULL END WHERE id = ?",
        (date.today().isoformat(), meeting_id),
    )
    db.commit()
    return redirect(request.referrer or url_for("todo"))


@app.route("/todo/relance/<kind>/<int:rec_id>/done", methods=["POST"])
@login_required
def toggle_follow_up_done(kind, rec_id):
    if kind not in ("meeting", "mail"):
        abort(404)
    table = "meetings" if kind == "meeting" else "mails"
    db = get_db()
    db.execute(
        f"UPDATE {table} SET follow_up_done = 1 - follow_up_done, "
        f"follow_up_done_at = CASE WHEN follow_up_done = 0 THEN ? ELSE NULL END "
        f"WHERE id = ?",
        (date.today().isoformat(), rec_id),
    )
    db.commit()
    return redirect(request.referrer or url_for("todo"))


# --------------------------------------------------------------------------- #
# « À contacter » — people someone has decided to write to
# --------------------------------------------------------------------------- #
# A to-do list of people, filled by the répartition below and ticked off like
# a relance: ticking moves the person to /fait, unticking brings them back.

def _to_contact_rows(db, done):
    """The « À contacter » list, pending (done=0) or dealt with (done=1)."""
    return db.execute(
        f"""
        SELECT p.id, p.name, p.email, p.role, p.contact_type,
               t.created_at, t.done_at,
               (SELECT GROUP_CONCAT(o.name, ', ')
                  FROM person_organisations po
                  JOIN organisations o ON o.id = po.organisation_id
                 WHERE po.person_id = p.id)     AS organisation_names,
               (SELECT mo.name FROM moderators mo
                 WHERE mo.id = t.added_by)      AS added_by_name,
               t.assigned_to,
               (SELECT mo.name FROM moderators mo
                 WHERE mo.id = t.assigned_to)   AS assigned_to_name,
               {PAUSEIA_MAIL_COUNT_SQL}         AS pauseia_mails
        FROM to_contact t
        JOIN persons p ON p.id = t.person_id
        WHERE t.done = ?
        -- Pending: one block per utilisateurice, « Quiconque » (NULL) last.
        ORDER BY {"assigned_to_name IS NULL, assigned_to_name COLLATE NOCASE, "
                  "name_key(p.name)" if not done
                  else "COALESCE(t.done_at, t.created_at) DESC, name_key(p.name)"}
        """,
        (done,),
    ).fetchall()


def _to_int(value):
    """A whole number from a form field, or None when blank or unusable.

    A field nobody filled in must not silently become 0.
    """
    value = (value or "").strip()
    try:
        return int(value)
    except ValueError:
        return None


@app.route("/todo/a-contacter/<int:person_id>/done", methods=["POST"])
@login_required
def toggle_to_contact_done(person_id):
    db = get_db()
    if db.execute("SELECT 1 FROM to_contact WHERE person_id = ?",
                  (person_id,)).fetchone() is None:
        abort(404)
    db.execute(
        "UPDATE to_contact SET done = 1 - done, "
        "done_at = CASE WHEN done = 0 THEN ? ELSE NULL END WHERE person_id = ?",
        (date.today().isoformat(), person_id),
    )
    db.commit()
    return redirect(request.referrer or url_for("todo"))


@app.route("/todo/a-contacter/<int:person_id>/retirer", methods=["POST"])
@login_required
def remove_to_contact(person_id):
    """Drop someone from the list for good. Only their place on the list goes:
    the fiche and everything attached to it are untouched."""
    db = get_db()
    db.execute("DELETE FROM to_contact WHERE person_id = ?", (person_id,))
    db.commit()
    flash("Personne retirée de « À contacter ».", "success")
    return redirect(request.referrer or url_for("fait"))


# --------------------------------------------------------------------------- #
# Répartition of « À contacter » between utilisateurices
# --------------------------------------------------------------------------- #
# Several organisations, a target for each (a number of people or a share of
# the organisation, rounded up), several utilisateurices who may each refuse
# some of those organisations. People are drawn at random and handed out so
# that everyone gets as close to the same number as the refusals allow. Nothing
# is saved until the preview is confirmed.

def _split_to_contact(groups, moderator_ids, exclusions, rng=random):
    """Draw people from each group and share them out between moderators.

    `groups` is a list of dicts {id, name, size, members, target, unit}: `members`
    the ids that may be drawn, `size` the whole organisation a percentage is
    taken of, `unit` "n" or "pct". An organisation with fewer people to draw
    than asked simply gives what it has.
    `exclusions` maps a moderator id to the set of group ids they refuse.

    Returns (assignments, errors): assignments are
    (person_id, group_id, moderator_id, allowed_moderator_ids) tuples.
    """
    errors, drawn = [], []
    taken = set()
    for g in groups:
        if g["unit"] == "pct":
            wanted = math.ceil(g["target"] * g["size"] / 100)
        else:
            wanted = g["target"]
        # Someone in two of the chosen organisations is drawn at most once.
        pool = [pid for pid in g["members"] if pid not in taken]
        picked = rng.sample(pool, min(wanted, len(pool)))
        taken.update(picked)
        allowed = [m for m in moderator_ids
                   if g["id"] not in exclusions.get(m, set())]
        if picked and not allowed:
            errors.append("Toutes les utilisateurices sélectionnées excluent "
                          f"« {g['name']} ».")
        drawn.extend((pid, g["id"], allowed) for pid in picked)
    if errors or not drawn:
        return [], errors

    # Most constrained first, each to whoever has least so far (ties at random).
    rng.shuffle(drawn)
    drawn.sort(key=lambda d: len(d[2]))
    load = {m: 0 for m in moderator_ids}
    owner = {}
    for pid, _, allowed in drawn:
        low = min(load[m] for m in allowed)
        owner[pid] = rng.choice([m for m in allowed if load[m] == low])
        load[owner[pid]] += 1

    # Greedy can still leave A with 2 more than C when A's people may only go
    # to B and B's only to C. Look for such a chain of moves from a busy
    # moderator to one with at least 2 fewer, and shift one person along it;
    # each shift lowers the spread, so this ends.
    allowed_of = {pid: allowed for pid, _, allowed in drawn}
    while True:
        for src in sorted(moderator_ids, key=lambda m: -load[m]):
            prev = {src: None}          # moderator -> (from, person moved)
            queue, dst = [src], None
            while queue and dst is None:
                u = queue.pop(0)
                for pid, m in owner.items():
                    if m != u:
                        continue
                    for v in allowed_of[pid]:
                        if v not in prev:
                            prev[v] = (u, pid)
                            if load[v] <= load[src] - 2:
                                dst = v
                                break
                            queue.append(v)
                    if dst is not None:
                        break
            if dst is not None:
                v = dst
                while prev[v] is not None:
                    u, pid = prev[v]
                    owner[pid] = v
                    v = u
                load[src] -= 1
                load[dst] += 1
                break
        else:
            break

    assignments = [(pid, gid, owner[pid], allowed) for pid, gid, allowed in drawn]
    return assignments, errors


# « Quiconque »: a share of the list left to whoever takes it. Offered next to
# the utilisateurices whatever the moderators table holds, and stored as
# assigned_to = NULL — which is also what entries made before the répartition
# carry, and they are indeed anyone's.
ANYONE = {"id": 0, "name": "Quiconque"}


def _pending_to_contact(db):
    return {r[0] for r in db.execute(
        "SELECT person_id FROM to_contact WHERE done = 0")}


def _split_organisations(db):
    """Organisations with how many people are in them, for the setup form.

    A person whose mandate has ended (in_office = 0) is not counted: they are
    kept for history, not to be written to.
    """
    return db.execute(
        """
        SELECT o.id, o.name, o.org_type,
               COUNT(p.id) AS size,
               COUNT(t.person_id) AS pending
          FROM organisations o
          JOIN person_organisations po ON po.organisation_id = o.id
          JOIN persons p ON p.id = po.person_id AND p.in_office = 1
          LEFT JOIN to_contact t ON t.person_id = p.id AND t.done = 0
         GROUP BY o.id
         ORDER BY o.name COLLATE NOCASE
        """
    ).fetchall()


def _split_member_stats(db):
    """Per organisation, one [courriels PauseIA, déjà en attente, a signé la
    déclaration] triple per in-office member: what the page needs to count, as
    the filters are typed, who they would leave. Same rules as the draw in
    to_contact_split."""
    stats = defaultdict(list)
    for r in db.execute(
        f"""
        SELECT po.organisation_id AS oid, {PAUSEIA_MAIL_COUNT_SQL} AS n,
               EXISTS (SELECT 1 FROM to_contact t
                        WHERE t.person_id = p.id AND t.done = 0) AS pending,
               p.signed_declaration AS signed
          FROM person_organisations po
          JOIN persons p ON p.id = po.person_id AND p.in_office = 1
        """
    ):
        stats[r["oid"]].append([r["n"], r["pending"], r["signed"]])
    return stats


@app.route("/todo/a-contacter/repartition", methods=["GET", "POST"])
@login_required
def to_contact_split():
    """Set up a répartition (GET) or draw one and show it for checking (POST).

    Posting again with the same settings is « Relancer le tirage »: the draw is
    random, so it simply comes out different.
    """
    db = get_db()
    organisations = _split_organisations(db)
    moderators = list(_moderators(db)) + [ANYONE]
    orgs_by_id = {o["id"]: o for o in organisations}
    mods_by_id = {m["id"]: m for m in moderators}

    form = {"org_ids": [], "targets": {}, "units": {},
            "type_targets": {}, "type_units": {},
            "min_mails": "", "max_mails": "", "skip_pending": False,
            "declaration": "",
            "moderator_ids": [], "exclusions": {}}
    preview = None
    if request.method == "POST":
        org_ids = [int(v) for v in dict.fromkeys(request.form.getlist("org_ids"))
                   if v.isdigit() and int(v) in orgs_by_id]
        moderator_ids = [int(v) for v in
                         dict.fromkeys(request.form.getlist("moderator_ids"))
                         if v.isdigit() and int(v) in mods_by_id]
        form["org_ids"], form["moderator_ids"] = org_ids, moderator_ids
        problems = []
        groups = []
        # Off by default: drawing someone already in « À contacter » is how a
        # redraw hands them to a different utilisateurice.
        form["skip_pending"] = bool(request.form.get("skip_pending"))
        pending = _pending_to_contact(db) if form["skip_pending"] else set()
        for oid in orgs_by_id:
            form["targets"][oid] = (request.form.get(f"target-{oid}") or "").strip()
            form["units"][oid] = ("pct" if request.form.get(f"unit-{oid}") == "pct"
                                  else "n")
        # Only people PauseIA has written to between these bounds may be drawn
        # (PAUSEIA_MAIL_COUNT_SQL: courriels citoyens do not count).
        min_mails = _to_int(request.form.get("min_mails"))
        max_mails = _to_int(request.form.get("max_mails"))
        form["min_mails"] = "" if min_mails is None else min_mails
        form["max_mails"] = "" if max_mails is None else max_mails
        # « Déclaration de PauseIA »: everyone, only signatories, or only not.
        declaration = request.form.get("declaration")
        form["declaration"] = declaration if declaration in ("oui", "non") else ""
        wanted_signed = {"oui": 1, "non": 0}.get(form["declaration"])
        # A type's value stands for each of its organisations left blank.
        for t in ORG_TYPES:
            form["type_targets"][t] = (request.form.get(f"type-target-{t}")
                                       or "").strip()
            form["type_units"][t] = ("pct" if request.form.get(f"type-unit-{t}")
                                     == "pct" else "n")
        for oid in org_ids:
            o = orgs_by_id[oid]
            if form["targets"][oid]:
                target, unit = _to_int(form["targets"][oid]), form["units"][oid]
            else:
                target = _to_int(form["type_targets"].get(o["org_type"]))
                unit = form["type_units"].get(o["org_type"], "n")
            if target is None or target <= 0 or (unit == "pct" and target > 100):
                problems.append(f"« {o['name']} » : indiquez un nombre de "
                                "personnes, ou un pourcentage entre 1 et 100, "
                                "pour l'organisation ou pour son type.")
                continue
            members = db.execute(
                f"SELECT p.id, p.signed_declaration AS signed, "
                f"{PAUSEIA_MAIL_COUNT_SQL} AS n FROM persons p "
                "JOIN person_organisations po ON po.person_id = p.id "
                "WHERE po.organisation_id = ? AND p.in_office = 1",
                (oid,)).fetchall()
            eligible = [m["id"] for m in members
                        if m["id"] not in pending
                        and (min_mails is None or m["n"] >= min_mails)
                        and (max_mails is None or m["n"] <= max_mails)
                        and (wanted_signed is None or m["signed"] == wanted_signed)]
            groups.append({"id": oid, "name": o["name"], "size": len(members),
                           "members": eligible,
                           "target": target, "unit": unit})
        for mid in mods_by_id:
            form["exclusions"][mid] = {
                int(v) for v in request.form.getlist(f"exclude-{mid}")
                if v.isdigit() and int(v) in orgs_by_id
            }
        if not org_ids:
            problems.append("Choisissez au moins une organisation.")
        if not moderator_ids:
            problems.append("Choisissez au moins une utilisateurice.")

        if problems:
            for p in problems:
                flash(p, "error")
        else:
            assignments, errors = _split_to_contact(
                groups, moderator_ids, form["exclusions"])
            for e in errors:
                flash(e, "error")
            if not errors:
                if not assignments:
                    flash("Personne à tirer avec ces réglages.", "error")
                else:
                    preview = _split_preview(db, assignments,
                                             moderator_ids, mods_by_id, orgs_by_id)

    return render_template(
        "to_contact_split.html",
        organisations=organisations,
        member_stats=_split_member_stats(db),
        org_types=ORG_TYPES,
        moderators=moderators,
        form=form,
        preview=preview,
    )


def _split_preview(db, assignments, moderator_ids, mods_by_id, orgs_by_id):
    """What the preview page needs: people grouped by who is to write."""
    names = {r["id"]: r for r in db.execute(
        f"SELECT id, name, role FROM persons WHERE id IN "
        f"({','.join('?' * len(assignments))})",
        [a[0] for a in assignments],
    )}
    by_mod = {mid: [] for mid in moderator_ids}
    for pid, gid, mid, allowed in assignments:
        by_mod[mid].append({
            "id": pid, "name": names[pid]["name"], "role": names[pid]["role"],
            "group": orgs_by_id[gid]["name"],
            "allowed": [mods_by_id[m] for m in allowed],
            "assigned": mid,
        })
    for rows in by_mod.values():
        rows.sort(key=lambda r: (r["group"].lower(), r["name"].lower()))
    return {
        "blocks": [(mods_by_id[mid], by_mod[mid]) for mid in moderator_ids],
        "total": len(assignments),
    }


@app.route("/todo/a-contacter/repartition/valider", methods=["POST"])
@login_required
def confirm_to_contact_split():
    """Save a checked répartition into « À contacter ».

    Someone already on the list, waiting or on /fait, keeps their one entry
    and takes the new utilisateurice: that is what a redraw is for.
    """
    db = get_db()
    person_ids = _ids_from_form(db, "person_ids", "persons")
    rows, skipped = [], 0
    for pid in person_ids:
        raw = request.form.get(f"assign-{pid}")
        mid = _valid_moderator(db, raw)
        if mid is None and raw != str(ANYONE["id"]):
            skipped += 1
            continue
        rows.append((pid, mid, _now()))
    if not rows:
        flash("Rien à enregistrer.", "error")
        return redirect(url_for("to_contact_split"))
    db.executemany(
        """
        INSERT INTO to_contact (person_id, assigned_to, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            assigned_to = excluded.assigned_to, done = 0, done_at = NULL,
            created_at = excluded.created_at
        """,
        rows,
    )
    db.commit()
    n = len(rows)
    flash(f"{n} personne{'' if n == 1 else 's'} réparti{'e' if n == 1 else 'es'} "
          "dans « À contacter ».", "success")
    if skipped:
        flash(f"{skipped} ignorée{'' if skipped == 1 else 's'} : "
              "utilisateurice introuvable.", "info")
    return redirect(url_for("todo"))


@app.route("/todo/rencontre/<int:meeting_id>/inscriptions", methods=["POST"])
@login_required
def sign_up_meeting(meeting_id):
    """Set who is going to an upcoming rencontre.

    These are the meeting's participants — the same `meeting_moderators` rows
    the rencontre form edits — so a sign-up made here is simply part of the
    rencontre, with nothing to carry over later.
    """
    db = get_db()
    if db.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone() is None:
        abort(404)
    mod_ids = {str(m["id"]) for m in _moderators(db)}
    chosen = [int(m) for m in request.form.getlist("participant_ids") if m in mod_ids]
    _set_meeting_moderators(db, meeting_id, chosen)
    db.commit()
    if _is_autosave():
        return "", 204
    flash("Inscriptions mises à jour.", "success")
    return redirect(url_for("todo"))


# --------------------------------------------------------------------------- #
# Répartition — choosing the date of a rencontre, and who goes on which date
# --------------------------------------------------------------------------- #

def _availability(db, meetings):
    """Per meeting, per candidate date: who is down for it.

    Returns {meeting_id: {iso_date: {"ids": {"3", "7"}, "names": ["Alice", …]}}},
    with an entry for every candidate date, including the ones nobody picked.
    """
    out = {}
    for m in meetings:
        by_date = {d: {"ids": set(), "names": []} for d in candidate_slots(m)}
        rows = db.execute(
            """
            SELECT a.on_date, mo.id, mo.name
            FROM meeting_availability a
            JOIN moderators mo ON mo.id = a.moderator_id
            WHERE a.meeting_id = ?
            ORDER BY mo.name COLLATE NOCASE
            """,
            (m["id"],),
        ).fetchall()
        for r in rows:
            slot = by_date.get(r["on_date"])
            if slot is None:      # a date dropped from the rencontre since
                continue
            slot["ids"].add(str(r["id"]))
            slot["names"].append(r["name"])
        out[m["id"]] = by_date
    return out


@app.route("/repartition")
@login_required
def repartition():
    """Rencontres whose date is not settled yet.

    A rencontre lands here as soon as it carries « Autres dates », and leaves
    the moment one of its candidate dates is validated — at which point it
    becomes an ordinary upcoming rencontre on /todo, with the people who said
    they were free that day already signed up.
    """
    db = get_db()
    meetings = db.execute(
        """
        SELECT m.*, GROUP_CONCAT(p.name, ', ') AS person_names
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
        WHERE m.alt_dates IS NOT NULL AND m.done = 0
        GROUP BY m.id
        ORDER BY m.meeting_date, m.id
        """
    ).fetchall()

    return render_template(
        "repartition.html",
        meetings=meetings,
        candidates={m["id"]: candidate_slots(m) for m in meetings},
        availability=_availability(db, meetings),
        moderators=_moderators(db),
        today=date.today().isoformat(),
        min_participants=MIN_PARTICIPANTS,
    )


@app.route("/repartition/<int:meeting_id>/disponibilites", methods=["POST"])
@login_required
def set_availability(meeting_id):
    """Record who is free on which candidate date.

    The whole grid is submitted at once, so what arrives is the complete answer
    and simply replaces what was there — same contract as the /todo sign-ups.
    """
    db = get_db()
    meeting = db.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None or meeting["alt_dates"] is None:
        abort(404)
    mod_ids = {str(m["id"]) for m in _moderators(db)}
    rows = [
        (int(mid), slot)
        for slot in candidate_slots(meeting)
        for mid in request.form.getlist(f"dispo_{slot}")
        if mid in mod_ids
    ]
    _set_meeting_availability(db, meeting_id, rows)
    db.commit()
    if _is_autosave():
        return "", 204
    flash("Disponibilités mises à jour.", "success")
    return redirect(url_for("repartition"))


@app.route("/repartition/<int:meeting_id>/valider", methods=["POST"])
@login_required
def confirm_meeting_date(meeting_id):
    """Settle a rencontre on one of its candidate dates.

    Voiding « Autres dates » is what moves it off this page, and the people
    down for the chosen date become its participants — the availability rows
    for the other dates have served their purpose and go.

    A date with fewer than MIN_PARTICIPANTS people free is refused. Settling a
    rencontre is the one irreversible step here — it discards every other
    candidate date — so it is the wrong moment to discover the date was
    understaffed. Fill the gap first, or pick another date.
    """
    db = get_db()
    meeting = db.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None or meeting["alt_dates"] is None:
        abort(404)
    slot = (request.form.get("on_date") or "").strip()
    if slot not in candidate_slots(meeting):
        abort(400)
    on_date, at_time = split_slot(slot)

    going = [
        r["moderator_id"]
        for r in db.execute(
            "SELECT moderator_id FROM meeting_availability "
            "WHERE meeting_id = ? AND on_date = ?",
            (meeting_id, slot),
        )
    ]
    # Checked here and not only in the page: the button is disabled client-side,
    # but the rule is what matters, not the styling that advertises it.
    if len(going) < MIN_PARTICIPANTS:
        flash(
            f"Impossible de fixer la rencontre au {fr_slot(slot)} : "
            # French puts zero in the singular: « 0 personne disponible ».
            f"{len(going)} personne{'s' if len(going) > 1 else ''} "
            f"disponible{'s' if len(going) > 1 else ''} ce jour-là, "
            f"il en faut {MIN_PARTICIPANTS}. Complétez les disponibilités, "
            "ou choisissez une autre date.",
            "error",
        )
        return redirect(url_for("repartition"))

    # The chosen slot carries the time too: picking « 11/09 à 14:00 » settles
    # the hour as well as the day. A slot with no time clears the hour, which is
    # the honest reading — nobody agreed one.
    db.execute(
        "UPDATE meetings SET meeting_date = ?, meeting_time = ?, alt_dates = NULL "
        "WHERE id = ?",
        (on_date, at_time, meeting_id),
    )
    _set_meeting_moderators(db, meeting_id, going)
    db.execute("DELETE FROM meeting_availability WHERE meeting_id = ?", (meeting_id,))
    db.commit()

    flash(f"Rencontre fixée au {fr_slot(slot)}.", "success")
    return redirect(url_for("todo"))


@app.route("/calendar")
@login_required
def calendar_view():
    db = get_db()
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if not 1 <= month <= 12:
        year, month = today.year, today.month

    lo = date(year, month, 1).isoformat()
    hi = date(year, month, pycalendar.monthrange(year, month)[1]).isoformat()

    # Meetings and follow-up ("relance") dates falling inside the shown month.
    meetings = db.execute(
        """
        SELECT m.id, m.meeting_date, m.meeting_time, m.summary, m.alt_dates,
               GROUP_CONCAT(p.name, ', ') AS person_names
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
        -- Rencontres under arbitration are pulled in whatever their main date,
        -- because any of their candidate dates may fall inside the month shown.
        WHERE m.meeting_date BETWEEN ? AND ? OR m.alt_dates IS NOT NULL
        GROUP BY m.id
        """,
        (lo, hi),
    ).fetchall()
    followups = db.execute(
        """
        SELECT * FROM (
            SELECT p.id, p.name, """ + person_follow_up_sql("p") + """ AS follow_up_date
            FROM persons p
        )
        WHERE follow_up_date BETWEEN ? AND ?
        """,
        (lo, hi),
    ).fetchall()

    # Map ISO date -> list of events for that day.
    events = {}
    for m in meetings:
        who = m["person_names"] or m["summary"]
        label = f"{m['meeting_time']} {who}" if m["meeting_time"] else who
        # An unsettled rencontre shows on *every* date it might happen on, each
        # marked as tentative — showing it once, at a date nobody has agreed to,
        # would read as settled and hide the other options entirely.
        if m["alt_dates"]:
            for slot in candidate_slots(m):
                on_date, at_time = split_slot(slot)
                if not (lo <= on_date <= hi):
                    continue
                # Each slot carries its own hour, so two créneaux on the same
                # day appear as two entries rather than one ambiguous one.
                events.setdefault(on_date, []).append({
                    "type": "tentative",
                    "label": "? " + (f"{at_time} {who}" if at_time else who),
                    "url": url_for("repartition"),
                })
            continue
        events.setdefault(m["meeting_date"], []).append({
            "type": "meeting",
            "label": label,
            "url": url_for("meeting_detail", meeting_id=m["id"]),
        })
    for f in followups:
        events.setdefault(f["follow_up_date"], []).append({
            "type": "followup",
            "label": "Relance " + f["name"],
            "url": url_for("person_detail", person_id=f["id"]),
        })

    weeks = pycalendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    return render_template(
        "calendar.html",
        weeks=weeks, events=events, today=today,
        year=year, month=month, month_name=FRENCH_MONTHS[month],
        weekdays=FRENCH_WEEKDAYS,
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
    )


# --------------------------------------------------------------------------- #
# Interventions & contenus lists (shared by their own pages and by the
# person / organisation detail pages)
# --------------------------------------------------------------------------- #

def _intervention_rows(db, where="", params=()):
    """Interventions with their organisation and people, newest first.

    `where` is a literal SQL fragment from our own call sites (never user
    input); values always travel through `params`.
    """
    return db.execute(
        f"""
        SELECT i.*, o.name AS organisation_name, o.org_type AS organisation_type,
               (SELECT GROUP_CONCAT(p.name, ', ')
                  FROM intervention_persons ip
                  JOIN persons p ON p.id = ip.person_id
                 WHERE ip.intervention_id = i.id) AS person_names
        FROM interventions i
        JOIN organisations o ON o.id = i.organisation_id
        {where}
        ORDER BY i.intervention_date DESC, i.id DESC
        """,
        params,
    ).fetchall()


def _content_rows(db, where="", params=()):
    """Contenus with their organisation and people, most recently published first."""
    return db.execute(
        f"""
        SELECT c.*, o.name AS organisation_name, o.org_type AS organisation_type,
               (SELECT GROUP_CONCAT(p.name, ', ')
                  FROM content_persons cp
                  JOIN persons p ON p.id = cp.person_id
                 WHERE cp.content_id = c.id) AS person_names
        FROM contents c
        JOIN organisations o ON o.id = c.organisation_id
        {where}
        ORDER BY c.published_on DESC, c.id DESC
        """,
        params,
    ).fetchall()


def _search_clause(alias, join_table, key_col, fields):
    """WHERE fragment searching `fields` of `alias`, its organisation and people.

    Every argument is a literal from our own call sites; the search term goes
    through the parameters (one per `?`, returned as a count).
    """
    own = " OR ".join(f"{alias}.{f} LIKE ?" for f in fields)
    sql = f"""
        WHERE {own} OR o.name LIKE ? OR {alias}.id IN (
            SELECT l.{key_col} FROM {join_table} l
            JOIN persons p ON p.id = l.person_id
            WHERE p.name LIKE ?)
    """
    return sql, len(fields) + 2


# --------------------------------------------------------------------------- #
# Persons
# --------------------------------------------------------------------------- #

@app.route("/people")
@login_required
def people():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    # « Type de contact » narrows the list. Optional on purpose: the default is
    # everybody, and only a value we know narrows anything — a stale or hand-
    # typed one falls back to showing all rather than an empty page.
    contact_type = (request.args.get("contact_type") or "").strip()
    if contact_type not in CONTACT_TYPES:
        contact_type = ""
    sql = """
        SELECT p.*,
            (SELECT GROUP_CONCAT(o.name, '|')
               FROM person_organisations po
               JOIN organisations o ON o.id = po.organisation_id
              WHERE po.person_id = p.id) AS organisation_names
        FROM persons p
    """
    where, params = [], []
    if q:
        like = f"%{q}%"
        where.append(
            """(p.name LIKE ? OR p.role LIKE ? OR p.stance LIKE ?
                OR p.id IN (SELECT po.person_id FROM person_organisations po
                            JOIN organisations o ON o.id = po.organisation_id
                            WHERE o.name LIKE ?))"""
        )
        params += [like] * 4
    if contact_type:
        where.append("p.contact_type = ?")
        params.append(contact_type)
    if where:
        sql += " WHERE " + " AND ".join(where)
    page, limite, decalage = page_window(PEOPLE_PER_PAGE)
    persons = db.execute(sql + " ORDER BY name_key(p.name) LIMIT ? OFFSET ?",
                         [*params, limite, decalage]).fetchall()
    has_next = len(persons) > PEOPLE_PER_PAGE
    persons = persons[:PEOPLE_PER_PAGE]
    # Per-type totals for the filter chips, so switching says how many there are
    # before you switch. Computed unfiltered: they are the sizes of the choices.
    counts = dict(
        db.execute("SELECT contact_type, COUNT(*) FROM persons GROUP BY 1").fetchall()
    )
    return render_template(
        "people.html", persons=persons, q=q, contact_type=contact_type,
        counts=counts, total=sum(counts.values()),
        page=page, has_next=has_next,
    )


def _signed_declaration_from_form():
    """« A signé la déclaration de PauseIA »: 1 (Oui), 0 (Non), or None when
    the form sent something else. Both forms preselect Non, so None only comes
    from a hand-made request."""
    return {"1": 1, "0": 0}.get((request.form.get("signed_declaration") or "").strip())


def _save_person(db, person):
    """Validate the person form and insert/update. Returns (person_id, errors).

    `person` is the existing row when editing, or None when creating.
    """
    name = (request.form.get("name") or "").strip()
    contact_type = (request.form.get("contact_type") or "").strip()
    # Everything below is read for whichever type was submitted, then the other
    # types' fields are discarded — the form renders every block and hides all
    # but one, so a browser that did not run the script can still post a stray
    # value.
    religion = (request.form.get("religion") or "").strip()
    if religion not in RELIGIONS:
        religion = ""
    # The religion is read before the roles because it narrows them: only the
    # functions of that culte are accepted for a religieux·se.
    role = _roles_from_form(contact_type, religion or None)
    # Only kept when at least one role justifies it, so clearing the roles can't
    # leave a stale portfolio behind on the record.
    portefeuille = (request.form.get("portefeuille") or "").strip()
    if not has_portfolio(role):
        portefeuille = ""
    # Same rule for « Préciser »: kept only while a role still asks for it, so
    # unticking « Bénévole » cannot leave its explanation behind on the record.
    role_detail = (request.form.get("role_detail") or "").strip()
    if not has_role_detail(role):
        role_detail = ""
    # Mandate details belong to an elected official; a journaliste has neither.
    circonscription = (request.form.get("circonscription") or "").strip()
    if contact_type != "Politique":
        portefeuille = ""
        circonscription = ""
    # « Territoire assigné » — the diocèse, paroisse or circonscription
    # rabbinique someone answers for. The religious counterpart of
    # circonscription, and cleared for everyone else for the same reason: a
    # person retyped away from Religieux·se must not keep a stale territory.
    territoire = (request.form.get("territoire") or "").strip()
    if contact_type != "Religieux·se":
        religion = ""
        territoire = ""
    stance = (request.form.get("stance") or "").strip()
    signed_declaration = _signed_declaration_from_form()
    first_contacted, fc_ok = _to_iso(request.form.get("first_contacted"))
    notes = (request.form.get("notes") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    social_links = (request.form.get("social_links") or "").strip()
    added_by = _valid_moderator(db, request.form.get("added_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))
    # The organisations offered depend on the type de contact, so only the ones
    # of the matching type are kept: a journaliste cannot end up in a groupe
    # politique by posting its id.
    wanted_org_types = allowed_org_types(contact_type)
    organisation_ids = [
        oid for oid in _ids_from_form(db, "organisation_ids", "organisations")
        if db.execute("SELECT org_type FROM organisations WHERE id = ?",
                      (oid,)).fetchone()["org_type"] in wanted_org_types
    ]

    errors = []
    if not name:
        errors.append("Le nom est obligatoire.")
    if contact_type not in CONTACT_TYPES:
        errors.append("Le type de contact est obligatoire.")
    elif contact_type == "Politique" and not any(
        db.execute("SELECT org_type FROM organisations WHERE id = ?",
                   (oid,)).fetchone()["org_type"] == "Groupe politique"
        for oid in organisation_ids
    ):
        # A politique's groupe politique was mandatory before the merge and
        # stays so: it is how /repartition and the lists group them.
        errors.append("Le groupe politique est obligatoire.")
    if not stance:
        errors.append("La position sur PauseIA est obligatoire.")
    if signed_declaration is None:
        errors.append("Indiquez si la personne a signé la déclaration de PauseIA.")
    if added_by is None:
        errors.append("Indiquez qui a ajouté la personne.")
    if validated_by is None:
        errors.append("Indiquez qui a validé la fiche.")
    if not fc_ok:
        errors.append("La date de contact est invalide (format JJ/MM/AAAA).")

    if errors:
        return None, errors

    values = (name, contact_type, role or None, portefeuille or None,
              role_detail or None, stance,
              first_contacted or None, notes or None, circonscription or None,
              email or None, phone or None, social_links or None,
              religion or None, territoire or None, signed_declaration,
              added_by, validated_by)
    if person is None:
        cur = db.execute(
            """
            INSERT INTO persons (
                name, contact_type, role, portefeuille, role_detail, stance,
                first_contacted, notes, circonscription, email, phone,
                social_links, religion, territoire, signed_declaration,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, _now()),
        )
        person_id = cur.lastrowid
    else:
        person_id = person["id"]
        db.execute(
            """
            UPDATE persons SET name = ?, contact_type = ?, role = ?,
                portefeuille = ?, role_detail = ?, stance = ?,
                first_contacted = ?, notes = ?,
                circonscription = ?, email = ?, phone = ?, social_links = ?,
                religion = ?, territoire = ?, signed_declaration = ?,
                added_by = ?, validated_by = ? WHERE id = ?
            """,
            (*values, person_id),
        )
    _set_links(db, "person_organisations", "person_id", person_id,
               "organisation_id", organisation_ids)
    _sync_group_mirror(db, [person_id])
    db.commit()
    return person_id, []


def _render_person_form(db, status=200, **ctx):
    """The shared person form. `organisations` carries every organisation with
    its type, so the picker can switch lists when the type de contact changes
    without a round trip (see static/form-masks.js)."""
    return render_template(
        "new_person.html", organisations=_organisation_choices(db),
        stances=STANCES, moderators=_moderators(db),
        today=date.today().isoformat(), **ctx,
    ), status


@app.route("/people/new", methods=["GET", "POST"])
@login_required
def new_person():
    db = get_db()
    ctx = dict(action_url=url_for("new_person"), heading="Nouvelle personne",
               cancel_url=url_for("people"))
    if request.method == "POST":
        person_id, errors = _save_person(db, None)
        if not errors:
            flash("Personne ajoutée.", "success")
            return redirect(url_for("person_detail", person_id=person_id))
        for e in errors:
            flash(e, "error")
        return _render_person_form(
            db, 400, form=request.form,
            selected_orgs=set(request.form.getlist("organisation_ids")), **ctx)
    # Arriving from an organisation page pre-selects it, and with it the type de
    # contact that organisation implies.
    form, selected = {}, set()
    org = _valid_organisation(db, request.args.get("organisation_id"))
    if org is not None:
        row = db.execute("SELECT org_type FROM organisations WHERE id = ?",
                         (org,)).fetchone()
        form = {"contact_type": CONTACT_TYPE_BY_ORG_TYPE.get(row["org_type"], "")}
        selected = {str(org)}
    return _render_person_form(db, form=form, selected_orgs=selected, **ctx)


@app.route("/people/<int:person_id>/edit", methods=["GET", "POST"])
@login_required
def edit_person(person_id):
    db = get_db()
    person = db.execute(
        "SELECT p.*, " + person_follow_up_sql("p") + " AS follow_up_date "
        "FROM persons p WHERE p.id = ?", (person_id,)
    ).fetchone()
    if person is None:
        abort(404)
    ctx = dict(action_url=url_for("edit_person", person_id=person_id),
               heading="Modifier la personne",
               cancel_url=url_for("person_detail", person_id=person_id))

    if request.method == "POST":
        _, errors = _save_person(db, person)
        if not errors:
            flash("Personne mise à jour.", "success")
            return redirect(url_for("person_detail", person_id=person_id))
        for e in errors:
            flash(e, "error")
        return _render_person_form(
            db, 400, form=request.form,
            selected_orgs=set(request.form.getlist("organisation_ids")), **ctx)

    linked = {
        str(r[0]) for r in db.execute(
            "SELECT organisation_id FROM person_organisations WHERE person_id = ?",
            (person_id,))
    }
    return _render_person_form(db, form=_form_from_row(person),
                               selected_orgs=linked, **ctx)


@app.route("/people/<int:person_id>/delete", methods=["POST"])
@login_required
def delete_person(person_id):
    db = get_db()
    person = db.execute(
        "SELECT id FROM persons WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None:
        abort(404)
    # meeting_persons / mail_persons links are ON DELETE CASCADE; the meetings
    # and mails themselves remain (they may involve other people).
    db.execute("DELETE FROM persons WHERE id = ?", (person_id,))
    db.commit()
    flash("Personne supprimée.", "success")
    return redirect(url_for("people"))


@app.route("/people/<int:person_id>")
@login_required
def person_detail(person_id):
    db = get_db()
    person = db.execute(
        "SELECT p.*, " + person_follow_up_sql("p") + " AS follow_up_date "
        "FROM persons p WHERE p.id = ?", (person_id,)
    ).fetchone()
    if person is None:
        abort(404)
    # Which interaction the derived relance comes from, so the read-only field
    # can say where it is set rather than showing a date from nowhere.
    follow_up_source = db.execute(
        """
        SELECT kind, id, on_date, follow_up_date FROM (
            SELECT 'meeting' AS kind, me.id AS id, me.meeting_date AS on_date,
                   me.created_at AS entered_at, me.follow_up_date
              FROM meetings me
              JOIN meeting_persons mp ON mp.meeting_id = me.id
             WHERE mp.person_id = ?
            UNION ALL
            SELECT 'mail', ma.id, ma.mail_date, ma.created_at, ma.follow_up_date
              FROM mails ma
              JOIN mail_persons xp ON xp.mail_id = ma.id
             WHERE xp.person_id = ?
        )
        ORDER BY on_date DESC, entered_at DESC
        LIMIT 1
        """,
        (person_id, person_id),
    ).fetchone()
    meetings = db.execute(
        """
        SELECT m.* FROM meetings m
        JOIN meeting_persons mp ON mp.meeting_id = m.id
        WHERE mp.person_id = ?
        ORDER BY m.meeting_date DESC, m.id DESC
        """,
        (person_id,),
    ).fetchall()
    mails = db.execute(
        """
        SELECT x.* FROM mails x
        JOIN mail_persons xp ON xp.mail_id = x.id
        WHERE xp.person_id = ?
        ORDER BY x.mail_date DESC, x.id DESC
        """,
        (person_id,),
    ).fetchall()
    mails_sent = sum(1 for x in mails if x["direction"] == "sent")
    mails_received = sum(1 for x in mails if x["direction"] == "received")
    conversations = _conversation_groups(db, mails)
    organisations = db.execute(
        """
        SELECT o.id, o.name, o.org_type, o.media_type, o.chambre
          FROM organisations o
          JOIN person_organisations po ON po.organisation_id = o.id
         WHERE po.person_id = ?
         ORDER BY o.name COLLATE NOCASE
        """,
        (person_id,),
    ).fetchall()
    added_by = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (person["added_by"],)
    ).fetchone()
    validator = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (person["validated_by"],)
    ).fetchone()
    return render_template(
        "person_detail.html",
        p=person,
        organisations=organisations,
        follow_up_source=follow_up_source,
        interventions=_intervention_rows(
            db, "WHERE i.id IN (SELECT intervention_id FROM intervention_persons "
                "WHERE person_id = ?)", (person_id,)),
        contents=_content_rows(
            db, "WHERE c.id IN (SELECT content_id FROM content_persons "
                "WHERE person_id = ?)", (person_id,)),
        meetings=meetings,
        conversations=conversations,
        mails_sent=mails_sent,
        mails_received=mails_received,
        directions=MAIL_DIRECTIONS,
        added_by=added_by["name"] if added_by else AUTO_IMPORT_LABEL,
        validated_by=validator["name"] if validator else None,
    )


# --------------------------------------------------------------------------- #
# Organisations (médias and groupes politiques)
# --------------------------------------------------------------------------- #

@app.route("/organisations")
@login_required
def organisations():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    org_type = (request.args.get("org_type") or "").strip()
    if org_type not in ORG_TYPES:
        org_type = ""
    sql = """
        SELECT o.*,
            (SELECT COUNT(*) FROM person_organisations po
              WHERE po.organisation_id = o.id) AS person_count,
            (SELECT COUNT(*) FROM contents c
              WHERE c.organisation_id = o.id)  AS content_count,
            (SELECT COUNT(*) FROM interventions i
              WHERE i.organisation_id = o.id)  AS intervention_count
        FROM organisations o
    """
    where, params = [], []
    if q:
        like = f"%{q}%"
        where.append("""(o.name LIKE ? OR o.media_type LIKE ? OR o.chambre LIKE ?
                         OR o.religion LIKE ? OR o.orientation LIKE ?
                         OR o.stance LIKE ?)""")
        params += [like] * 6
    if org_type:
        where.append("o.org_type = ?")
        params.append(org_type)
    if where:
        sql += " WHERE " + " AND ".join(where)
    page, limite, decalage = page_window(PEOPLE_PER_PAGE)
    rows = db.execute(sql + " ORDER BY o.name COLLATE NOCASE LIMIT ? OFFSET ?",
                      [*params, limite, decalage]).fetchall()
    has_next = len(rows) > PEOPLE_PER_PAGE
    rows = rows[:PEOPLE_PER_PAGE]
    counts = dict(
        db.execute("SELECT org_type, COUNT(*) FROM organisations GROUP BY 1").fetchall()
    )
    return render_template(
        "organisation_list.html", organisations=rows, q=q, org_type=org_type,
        counts=counts, total=sum(counts.values()),
        page=page, has_next=has_next,
    )


def _save_organisation(db, organisation):
    """Validate the organisation form and insert/update. Returns (id, errors)."""
    name = (request.form.get("name") or "").strip()
    org_type = (request.form.get("org_type") or "").strip()
    stance = (request.form.get("stance") or "").strip()
    link, link_ok = _to_url(request.form.get("link"))
    notes = (request.form.get("notes") or "").strip()
    added_by = _valid_moderator(db, request.form.get("added_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))
    # Type-specific fields. The form renders every block and hides all but one,
    # so the other types' values are dropped here rather than trusted.
    media_type = (request.form.get("media_type") or "").strip()
    orientation = (request.form.get("orientation") or "").strip()
    chambre = (request.form.get("chambre") or "").strip()
    religion = (request.form.get("religion") or "").strip()
    if org_type == "Média":
        chambre = religion = ""
    elif org_type == "Culte":
        media_type = orientation = chambre = ""
    elif org_type in NEUTRAL_ORG_TYPES:
        # An ONG or une entreprise has none of the four: position sur PauseIA,
        # lien and notes are the whole fiche.
        media_type = orientation = chambre = religion = ""
    else:
        media_type = orientation = religion = ""

    errors = []
    if not name:
        errors.append("Le nom de l'organisation est obligatoire.")
    if org_type not in ORG_TYPES:
        errors.append("Le type d'organisation est obligatoire.")
    elif org_type == "Média":
        if media_type not in MEDIA_TYPES:
            errors.append("Le type de média est obligatoire.")
        if orientation not in ORIENTATIONS:
            errors.append("L'orientation politique est obligatoire (« Inconnue » si besoin).")
    elif org_type == "Culte":
        # Optional, like a groupe politique's chambre: a culte can be recorded
        # before anyone decides which of the seven it is filed under.
        if religion and religion not in RELIGIONS:
            errors.append("La religion indiquée est inconnue.")
    elif org_type in NEUTRAL_ORG_TYPES:
        pass  # nothing else to ask; the fields above were already cleared
    elif chambre and chambre not in CHAMBERS:
        errors.append("La chambre indiquée est inconnue.")
    if stance not in STANCES:
        errors.append("La position sur PauseIA est obligatoire.")
    if not link_ok:
        errors.append("Le lien doit être une adresse web (https://…).")
    if added_by is None:
        errors.append("Indiquez qui a ajouté l'organisation.")
    if validated_by is None:
        errors.append("Indiquez qui a validé la fiche.")

    if errors:
        return None, errors

    values = (name, org_type, media_type or None, orientation or None,
              chambre or None, religion or None, stance, link or None,
              notes or None, added_by, validated_by)
    if organisation is None:
        cur = db.execute(
            """
            INSERT INTO organisations (name, org_type, media_type, orientation,
                chambre, religion, stance, link, notes, added_by, validated_by,
                created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, _now()),
        )
        organisation_id = cur.lastrowid
    else:
        organisation_id = organisation["id"]
        db.execute(
            """
            UPDATE organisations SET name = ?, org_type = ?, media_type = ?,
                orientation = ?, chambre = ?, religion = ?, stance = ?,
                link = ?, notes = ?,
                added_by = ?, validated_by = ? WHERE id = ?
            """,
            (*values, organisation_id),
        )
    # Renaming a groupe politique, or retyping one, moves its members' mirror
    # column with it — see _sync_group_mirror.
    _sync_group_mirror(db, [
        r[0] for r in db.execute(
            "SELECT person_id FROM person_organisations WHERE organisation_id = ?",
            (organisation_id,))
    ])
    db.commit()
    return organisation_id, []


def _render_organisation_form(db, status=200, **ctx):
    return render_template(
        "organisation_form.html", moderators=_moderators(db),
        media_types=MEDIA_TYPES, orientations=ORIENTATIONS, chambers=CHAMBERS,
        stances=STANCES, **ctx,
    ), status


@app.route("/organisations/new", methods=["GET", "POST"])
@login_required
def new_organisation():
    db = get_db()
    ctx = dict(action_url=url_for("new_organisation"),
               heading="Nouvelle organisation", cancel_url=url_for("organisations"))
    if request.method == "POST":
        organisation_id, errors = _save_organisation(db, None)
        if not errors:
            flash("Organisation ajoutée.", "success")
            return redirect(url_for("organisation_detail",
                                    organisation_id=organisation_id))
        for e in errors:
            flash(e, "error")
        return _render_organisation_form(db, 400, form=request.form, **ctx)
    # « + Média » / « + Groupe politique » land here with the type already set.
    wanted = request.args.get("org_type", "")
    return _render_organisation_form(
        db, form={"org_type": wanted if wanted in ORG_TYPES else ""}, **ctx)


@app.route("/organisations/<int:organisation_id>/edit", methods=["GET", "POST"])
@login_required
def edit_organisation(organisation_id):
    db = get_db()
    organisation = db.execute(
        "SELECT * FROM organisations WHERE id = ?", (organisation_id,)
    ).fetchone()
    if organisation is None:
        abort(404)
    ctx = dict(action_url=url_for("edit_organisation",
                                  organisation_id=organisation_id),
               heading="Modifier l'organisation",
               cancel_url=url_for("organisation_detail",
                                  organisation_id=organisation_id))
    if request.method == "POST":
        _, errors = _save_organisation(db, organisation)
        if not errors:
            flash("Organisation mise à jour.", "success")
            return redirect(url_for("organisation_detail",
                                    organisation_id=organisation_id))
        for e in errors:
            flash(e, "error")
        return _render_organisation_form(db, 400, form=request.form, **ctx)
    return _render_organisation_form(
        db, form=_form_from_row(organisation), **ctx)


@app.route("/organisations/<int:organisation_id>/delete", methods=["POST"])
@login_required
def delete_organisation(organisation_id):
    db = get_db()
    if db.execute("SELECT 1 FROM organisations WHERE id = ?",
                  (organisation_id,)).fetchone() is None:
        abort(404)
    # A contenu or an intervention cannot exist without its organisation, and
    # deleting them along with it would be an easy way to lose a lot of work by
    # mistake. So the organisation has to be emptied first.
    contents = db.execute("SELECT COUNT(*) FROM contents WHERE organisation_id = ?",
                          (organisation_id,)).fetchone()[0]
    interventions = db.execute(
        "SELECT COUNT(*) FROM interventions WHERE organisation_id = ?",
        (organisation_id,)).fetchone()[0]
    if contents or interventions:
        flash(
            f"Impossible de supprimer cette organisation : elle a encore {contents} "
            f"contenu(s) et {interventions} intervention(s). Supprimez-les ou "
            "rattachez-les à une autre organisation d'abord.",
            "error",
        )
        return redirect(url_for("organisation_detail",
                                organisation_id=organisation_id))
    members = [
        r[0] for r in db.execute(
            "SELECT person_id FROM person_organisations WHERE organisation_id = ?",
            (organisation_id,))
    ]
    # person_organisations links are ON DELETE CASCADE; the people remain.
    db.execute("DELETE FROM organisations WHERE id = ?", (organisation_id,))
    _sync_group_mirror(db, members)
    db.commit()
    flash("Organisation supprimée.", "success")
    return redirect(url_for("organisations"))


@app.route("/organisations/<int:organisation_id>")
@login_required
def organisation_detail(organisation_id):
    db = get_db()
    organisation = db.execute(
        "SELECT * FROM organisations WHERE id = ?", (organisation_id,)
    ).fetchone()
    if organisation is None:
        abort(404)
    people_rows = db.execute(
        """
        SELECT p.id, p.name, p.contact_type, p.role, p.stance FROM persons p
        JOIN person_organisations po ON po.person_id = p.id
        WHERE po.organisation_id = ?
        ORDER BY name_key(p.name)
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "organisation_detail.html",
        o=organisation,
        people=people_rows,
        interventions=_intervention_rows(db, "WHERE i.organisation_id = ?",
                                         (organisation_id,)),
        contents=_content_rows(db, "WHERE c.organisation_id = ?",
                               (organisation_id,)),
        added_by=_moderator_name(db, organisation["added_by"]),
        validated_by=_moderator_name(db, organisation["validated_by"]),
    )


# --------------------------------------------------------------------------- #
# Contenus
# --------------------------------------------------------------------------- #

@app.route("/contenus")
@login_required
def contents():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    if q:
        where, n = _search_clause("c", "content_persons", "content_id",
                                  ("summary", "link", "content_type"))
        rows = _content_rows(db, where, (f"%{q}%",) * n)
    else:
        rows = _content_rows(db)
    return render_template("contents.html", contents=rows, q=q)


def _save_content(db, content):
    """Validate the contenu form and insert/update. Returns (content_id, errors)."""
    organisation_id = _valid_organisation(db, request.form.get("organisation_id"))
    person_ids = _ids_from_form(db, "person_ids", "persons")
    content_type = (request.form.get("content_type") or "").strip()
    genre = (request.form.get("genre") or "").strip()
    link, link_ok = _to_url(request.form.get("link"))
    published_on, date_ok = _to_iso(request.form.get("published_on"))
    summary = (request.form.get("summary") or "").strip()
    recorded_by = _valid_moderator(db, request.form.get("recorded_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))

    errors = []
    if organisation_id is None:
        errors.append("Choisissez l'organisation du contenu.")
    if not person_ids:
        errors.append("Sélectionnez au moins une personne.")
    if content_type not in CONTENT_TYPES:
        errors.append("Le type de contenu est obligatoire.")
    if genre and genre not in GENRES:
        errors.append("Le genre du contenu est invalide.")
    if not link:
        errors.append("Le lien vers le contenu est obligatoire.")
    elif not link_ok:
        errors.append("Le lien doit être une adresse web (https://…).")
    if not published_on:
        errors.append("La date de publication est obligatoire.")
    elif not date_ok:
        errors.append("La date de publication est invalide (format JJ/MM/AAAA).")
    if recorded_by is None:
        errors.append("Indiquez qui a saisi le contenu.")
    if validated_by is None:
        errors.append("Indiquez qui a validé le contenu.")

    if errors:
        return None, errors

    values = (organisation_id, content_type, genre or None, link, published_on,
              summary or None, recorded_by, validated_by)
    if content is None:
        cur = db.execute(
            """
            INSERT INTO contents (organisation_id, content_type, genre, link,
                published_on, summary, recorded_by, validated_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, _now()),
        )
        content_id = cur.lastrowid
    else:
        content_id = content["id"]
        db.execute(
            """
            UPDATE contents SET organisation_id = ?, content_type = ?, genre = ?,
                link = ?, published_on = ?, summary = ?, recorded_by = ?,
                validated_by = ?
            WHERE id = ?
            """,
            (*values, content_id),
        )
    _set_links(db, "content_persons", "content_id", content_id,
               "person_id", person_ids)
    _link_persons_to_organisation(db, person_ids, organisation_id)
    db.commit()
    return content_id, []


def _render_content_form(db, status=200, **ctx):
    return render_template(
        "content_form.html", people=_person_choices(db),
        organisations=_organisation_choices(db), moderators=_moderators(db),
        content_types=CONTENT_TYPES, today=date.today().isoformat(), **ctx,
    ), status


@app.route("/contenus/new", methods=["GET", "POST"])
@login_required
def new_content():
    db = get_db()
    ctx = dict(action_url=url_for("new_content"), heading="Nouveau contenu",
               cancel_url=url_for("contents"))
    if request.method == "POST":
        content_id, errors = _save_content(db, None)
        if not errors:
            flash("Contenu ajouté.", "success")
            return redirect(url_for("content_detail", content_id=content_id))
        for e in errors:
            flash(e, "error")
        return _render_content_form(
            db, 400, form=request.form,
            selected_ids=set(request.form.getlist("person_ids")), **ctx)
    # Arriving from a person or organisation page pre-selects it.
    form = {"organisation_id": request.args.get("organisation_id", "")}
    selected = {request.args["person_id"]} if request.args.get("person_id") else set()
    return _render_content_form(db, form=form, selected_ids=selected, **ctx)


@app.route("/contenus/<int:content_id>/edit", methods=["GET", "POST"])
@login_required
def edit_content(content_id):
    db = get_db()
    content = db.execute("SELECT * FROM contents WHERE id = ?",
                         (content_id,)).fetchone()
    if content is None:
        abort(404)
    ctx = dict(action_url=url_for("edit_content", content_id=content_id),
               heading="Modifier le contenu",
               cancel_url=url_for("content_detail", content_id=content_id))
    if request.method == "POST":
        _, errors = _save_content(db, content)
        if not errors:
            flash("Contenu mis à jour.", "success")
            return redirect(url_for("content_detail", content_id=content_id))
        for e in errors:
            flash(e, "error")
        return _render_content_form(
            db, 400, form=request.form,
            selected_ids=set(request.form.getlist("person_ids")), **ctx)
    linked = {
        str(r[0]) for r in db.execute(
            "SELECT person_id FROM content_persons WHERE content_id = ?",
            (content_id,))
    }
    return _render_content_form(db, form=_form_from_row(content),
                                selected_ids=linked, **ctx)


@app.route("/contenus/<int:content_id>/delete", methods=["POST"])
@login_required
def delete_content(content_id):
    db = get_db()
    if db.execute("SELECT 1 FROM contents WHERE id = ?",
                  (content_id,)).fetchone() is None:
        abort(404)
    db.execute("DELETE FROM contents WHERE id = ?", (content_id,))
    db.commit()
    flash("Contenu supprimé.", "success")
    return redirect(url_for("contents"))


@app.route("/contenus/<int:content_id>")
@login_required
def content_detail(content_id):
    db = get_db()
    rows = _content_rows(db, "WHERE c.id = ?", (content_id,))
    if not rows:
        abort(404)
    content = rows[0]
    return render_template(
        "content_detail.html", c=content,
        people=_persons_of(db, "content_persons", "content_id", content_id),
        recorded_by=_moderator_name(db, content["recorded_by"]),
        validated_by=_moderator_name(db, content["validated_by"]),
    )


# --------------------------------------------------------------------------- #
# Interventions
# --------------------------------------------------------------------------- #

@app.route("/interventions")
@login_required
def interventions():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    if q:
        where, n = _search_clause("i", "intervention_persons", "intervention_id",
                                  ("summary", "link", "intervention_type"))
        rows = _intervention_rows(db, where, (f"%{q}%",) * n)
    else:
        rows = _intervention_rows(db)
    return render_template("interventions.html", interventions=rows, q=q)


def _save_intervention(db, intervention):
    """Validate the intervention form and insert/update. Returns (id, errors)."""
    organisation_id = _valid_organisation(db, request.form.get("organisation_id"))
    person_ids = _ids_from_form(db, "person_ids", "persons")
    participant_ids = _ids_from_form(db, "participant_ids", "moderators")
    intervention_type = (request.form.get("intervention_type") or "").strip()
    genre = (request.form.get("genre") or "").strip()
    link, link_ok = _to_url(request.form.get("link"))
    intervention_date, date_ok = _to_iso(request.form.get("intervention_date"))
    summary = (request.form.get("summary") or "").strip()
    recorded_by = _valid_moderator(db, request.form.get("recorded_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))

    errors = []
    if organisation_id is None:
        errors.append("Choisissez l'organisation de l'intervention.")
    if not person_ids:
        errors.append("Sélectionnez au moins une personne.")
    if not participant_ids:
        errors.append("Indiquez qui est intervenu pour PauseIA.")
    if intervention_type not in INTERVENTION_TYPES:
        errors.append("Le type d'intervention est obligatoire.")
    if genre and genre not in GENRES:
        errors.append("Le genre de l'intervention est invalide.")
    if not link:
        errors.append("Le lien vers l'intervention est obligatoire.")
    elif not link_ok:
        errors.append("Le lien doit être une adresse web (https://…).")
    if not intervention_date:
        errors.append("La date de l'intervention est obligatoire.")
    elif not date_ok:
        errors.append("La date de l'intervention est invalide (format JJ/MM/AAAA).")
    if recorded_by is None:
        errors.append("Indiquez qui a saisi l'intervention.")
    if validated_by is None:
        errors.append("Indiquez qui a validé l'intervention.")

    if errors:
        return None, errors

    values = (organisation_id, intervention_date, intervention_type, genre or None,
              link, summary or None, recorded_by, validated_by)
    if intervention is None:
        cur = db.execute(
            """
            INSERT INTO interventions (organisation_id, intervention_date,
                intervention_type, genre, link, summary, recorded_by,
                validated_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, _now()),
        )
        intervention_id = cur.lastrowid
    else:
        intervention_id = intervention["id"]
        db.execute(
            """
            UPDATE interventions SET organisation_id = ?, intervention_date = ?,
                intervention_type = ?, genre = ?, link = ?, summary = ?,
                recorded_by = ?, validated_by = ?
            WHERE id = ?
            """,
            (*values, intervention_id),
        )
    _set_links(db, "intervention_persons", "intervention_id", intervention_id,
               "person_id", person_ids)
    _set_links(db, "intervention_moderators", "intervention_id", intervention_id,
               "moderator_id", participant_ids)
    _link_persons_to_organisation(db, person_ids, organisation_id)
    db.commit()
    return intervention_id, []


def _render_intervention_form(db, status=200, **ctx):
    return render_template(
        "intervention_form.html", people=_person_choices(db),
        organisations=_organisation_choices(db), moderators=_moderators(db),
        intervention_types=INTERVENTION_TYPES, today=date.today().isoformat(),
        **ctx,
    ), status


@app.route("/interventions/new", methods=["GET", "POST"])
@login_required
def new_intervention():
    db = get_db()
    ctx = dict(action_url=url_for("new_intervention"),
               heading="Nouvelle intervention", cancel_url=url_for("interventions"))
    if request.method == "POST":
        intervention_id, errors = _save_intervention(db, None)
        if not errors:
            flash("Intervention ajoutée.", "success")
            return redirect(url_for("intervention_detail",
                                    intervention_id=intervention_id))
        for e in errors:
            flash(e, "error")
        return _render_intervention_form(
            db, 400, form=request.form,
            selected_ids=set(request.form.getlist("person_ids")),
            selected_mods=set(request.form.getlist("participant_ids")), **ctx)
    form = {"organisation_id": request.args.get("organisation_id", "")}
    selected = {request.args["person_id"]} if request.args.get("person_id") else set()
    return _render_intervention_form(db, form=form, selected_ids=selected,
                                     selected_mods=set(), **ctx)


@app.route("/interventions/<int:intervention_id>/edit", methods=["GET", "POST"])
@login_required
def edit_intervention(intervention_id):
    db = get_db()
    intervention = db.execute(
        "SELECT * FROM interventions WHERE id = ?", (intervention_id,)
    ).fetchone()
    if intervention is None:
        abort(404)
    ctx = dict(action_url=url_for("edit_intervention",
                                  intervention_id=intervention_id),
               heading="Modifier l'intervention",
               cancel_url=url_for("intervention_detail",
                                  intervention_id=intervention_id))
    if request.method == "POST":
        _, errors = _save_intervention(db, intervention)
        if not errors:
            flash("Intervention mise à jour.", "success")
            return redirect(url_for("intervention_detail",
                                    intervention_id=intervention_id))
        for e in errors:
            flash(e, "error")
        return _render_intervention_form(
            db, 400, form=request.form,
            selected_ids=set(request.form.getlist("person_ids")),
            selected_mods=set(request.form.getlist("participant_ids")), **ctx)
    linked = {
        str(r[0]) for r in db.execute(
            "SELECT person_id FROM intervention_persons WHERE intervention_id = ?",
            (intervention_id,))
    }
    linked_mods = {
        str(r[0]) for r in db.execute(
            "SELECT moderator_id FROM intervention_moderators "
            "WHERE intervention_id = ?", (intervention_id,))
    }
    return _render_intervention_form(db, form=_form_from_row(intervention),
                                     selected_ids=linked,
                                     selected_mods=linked_mods, **ctx)


@app.route("/interventions/<int:intervention_id>/delete", methods=["POST"])
@login_required
def delete_intervention(intervention_id):
    db = get_db()
    if db.execute("SELECT 1 FROM interventions WHERE id = ?",
                  (intervention_id,)).fetchone() is None:
        abort(404)
    db.execute("DELETE FROM interventions WHERE id = ?", (intervention_id,))
    db.commit()
    flash("Intervention supprimée.", "success")
    return redirect(url_for("interventions"))


@app.route("/interventions/<int:intervention_id>")
@login_required
def intervention_detail(intervention_id):
    db = get_db()
    rows = _intervention_rows(db, "WHERE i.id = ?", (intervention_id,))
    if not rows:
        abort(404)
    intervention = rows[0]
    return render_template(
        "intervention_detail.html", i=intervention,
        people=_persons_of(db, "intervention_persons", "intervention_id",
                           intervention_id),
        participants=_moderator_names_of(db, "intervention_moderators",
                                         "intervention_id", intervention_id),
        recorded_by=_moderator_name(db, intervention["recorded_by"]),
        validated_by=_moderator_name(db, intervention["validated_by"]),
    )


# --------------------------------------------------------------------------- #
# Mails
# --------------------------------------------------------------------------- #

@app.route("/mails")
@login_required
def mails():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    base = """
        SELECT x.*,
               GROUP_CONCAT(p.name, ', ') AS person_names
        FROM mails x
        LEFT JOIN mail_persons xp ON xp.mail_id = x.id
        LEFT JOIN persons p       ON p.id = xp.person_id
    """
    # Une page, pas la table entière : la boîte d'audit contient déjà plus de
    # 2 000 messages et n'en perdra jamais. On demande un élément de plus que la
    # page pour savoir s'il existe une suite, sans payer un COUNT(*).
    page = max(1, request.args.get("page", type=int) or 1)
    offset = (page - 1) * MAILS_PER_PAGE
    window = "LIMIT ? OFFSET ?"
    if q:
        like = f"%{q}%"
        rows = db.execute(
            base
            + """
            WHERE x.id IN (
                SELECT x2.id FROM mails x2
                LEFT JOIN mail_persons xp2 ON xp2.mail_id = x2.id
                LEFT JOIN persons p2       ON p2.id = xp2.person_id
                WHERE p2.name LIKE ? OR x2.summary LIKE ? OR x2.subject LIKE ?
            )
            GROUP BY x.id
            ORDER BY x.mail_date DESC, x.id DESC
            """ + window,
            (like, like, like, MAILS_PER_PAGE + 1, offset),
        ).fetchall()
    else:
        rows = db.execute(
            base + " GROUP BY x.id ORDER BY x.mail_date DESC, x.id DESC " + window,
            (MAILS_PER_PAGE + 1, offset),
        ).fetchall()
    has_next = len(rows) > MAILS_PER_PAGE
    rows = rows[:MAILS_PER_PAGE]
    return render_template("mails.html", mails=rows, q=q, page=page,
                           has_next=has_next, first_index=offset + 1,
                           directions=MAIL_DIRECTIONS)


def _save_mail(db, mail):
    """Validate the mail form and insert/update. Returns (mail_id, errors).

    `mail` is the existing row when editing, or None when creating.
    """
    people = db.execute("SELECT id FROM persons").fetchall()
    valid_ids = {str(p["id"]) for p in people}

    mail_date, date_ok = _to_iso(request.form.get("mail_date"))
    direction = (request.form.get("direction") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    summary = (request.form.get("summary") or "").strip()
    important = 1 if request.form.get("important") else 0
    follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
    person_ids = [pid for pid in request.form.getlist("person_ids") if pid in valid_ids]
    received_by = _valid_moderator(db, request.form.get("received_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))
    remove_doc = bool(request.form.get("remove_document"))

    # A received mail gets a relance too. It can be a person's most recent
    # interaction, and a person's « Relance prévue » mirrors that latest one —
    # so blanking it here by rule would silently drop their follow-up instead
    # of letting someone decide there should not be one.

    errors = []
    if not person_ids:
        errors.append("Sélectionnez au moins une personne.")
    if not subject:
        errors.append("L'objet du courriel est obligatoire.")
    if received_by is None:
        errors.append("Indiquez qui a reçu ou envoyé le courriel.")
    if validated_by is None:
        errors.append("Indiquez qui a validé le courriel.")
    if not mail_date:
        errors.append("La date du courriel est obligatoire.")
    elif not date_ok:
        errors.append("La date du courriel est invalide (format JJ/MM/AAAA).")
    if direction not in MAIL_DIRECTIONS:
        errors.append("Précisez si le courriel a été envoyé ou reçu.")
    if not summary:
        errors.append("Un bref résumé est obligatoire.")
    if not fu_ok:
        errors.append("La date de relance est invalide (format JJ/MM/AAAA).")
    file, stored_name, orig_name = _stage_upload(errors)

    if errors:
        return None, errors

    person_id_ints = [int(pid) for pid in person_ids]

    if mail is None:
        cur = db.execute(
            """
            INSERT INTO mails (
                mail_date, direction, important, subject, summary, follow_up_date,
                received_by, validated_by,
                document_stored_name, document_orig_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mail_date, direction, important, subject, summary,
             follow_up_date or None,
             received_by, validated_by, stored_name, orig_name,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        mail_id = cur.lastrowid
    else:
        mail_id = mail["id"]
        new_stored, new_orig = mail["document_stored_name"], mail["document_orig_name"]
        if stored_name:
            _delete_upload(mail["document_stored_name"])
            new_stored, new_orig = stored_name, orig_name
        elif remove_doc:
            _delete_upload(mail["document_stored_name"])
            new_stored, new_orig = None, None
        db.execute(
            """
            UPDATE mails SET mail_date = ?, direction = ?, important = ?,
                subject = ?, summary = ?,
                follow_up_date = ?, received_by = ?, validated_by = ?,
                document_stored_name = ?, document_orig_name = ?
            WHERE id = ?
            """,
            (mail_date, direction, important, subject, summary,
             follow_up_date or None,
             received_by, validated_by, new_stored, new_orig, mail_id),
        )

    if file and stored_name:
        file.save(UPLOAD_DIR / stored_name)
    _set_person_links(db, "mail_persons", "mail_id", mail_id, person_id_ints)
    # No copy-down onto `persons`: a person's relance is derived from their most
    # recent interaction (person_follow_up_sql), so this mail already drives it
    # if it is the latest one — and must not overwrite it if it is not.
    db.commit()
    return mail_id, []


@app.route("/mails/new", methods=["GET", "POST"])
@login_required
def new_mail():
    db = get_db()
    people = _person_choices(db)
    mods = _moderators(db)

    if request.method == "POST":
        mail_id, errors = _save_mail(db, None)
        if not errors:
            flash("Courriel ajouté.", "success")
            return redirect(url_for("mail_detail", mail_id=mail_id))
        for e in errors:
            flash(e, "error")
        return (
            render_template(
                "new_mail.html", people=people, moderators=mods, directions=MAIL_DIRECTIONS,
                form=request.form, selected_ids=set(request.form.getlist("person_ids")),
                current=None, today=date.today().isoformat(),
                action_url=url_for("new_mail"), heading="Nouveau courriel",
                cancel_url=url_for("mails"),
            ),
            400,
        )

    # Arriving from a person's page pre-ticks them.
    selected = {request.args["person_id"]} if request.args.get("person_id") else set()
    return render_template(
        "new_mail.html", people=people, moderators=mods, directions=MAIL_DIRECTIONS,
        form={}, selected_ids=selected, current=None, today=date.today().isoformat(),
        action_url=url_for("new_mail"), heading="Nouveau courriel",
        cancel_url=url_for("mails"),
    )


@app.route("/mails/<int:mail_id>/edit", methods=["GET", "POST"])
@login_required
def edit_mail(mail_id):
    db = get_db()
    mail = db.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if mail is None:
        abort(404)
    people = _person_choices(db)
    mods = _moderators(db)
    linked = {
        str(r["person_id"])
        for r in db.execute(
            "SELECT person_id FROM mail_persons WHERE mail_id = ?", (mail_id,)
        )
    }

    if request.method == "POST":
        _, errors = _save_mail(db, mail)
        if not errors:
            flash("Courriel mis à jour.", "success")
            return redirect(url_for("mail_detail", mail_id=mail_id))
        for e in errors:
            flash(e, "error")
        form = request.form
        selected = set(request.form.getlist("person_ids"))
    else:
        form = _form_from_row(mail)
        selected = linked

    return render_template(
        "new_mail.html", people=people, moderators=mods, directions=MAIL_DIRECTIONS,
        form=form, selected_ids=selected, current=mail,
        today=date.today().isoformat(),
        action_url=url_for("edit_mail", mail_id=mail_id),
        heading="Modifier le courriel",
        cancel_url=url_for("mail_detail", mail_id=mail_id),
    )


@app.route("/mails/<int:mail_id>/delete", methods=["POST"])
@login_required
def delete_mail(mail_id):
    db = get_db()
    mail = db.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if mail is None:
        abort(404)
    _delete_upload(mail["document_stored_name"])
    # mail_persons is ON DELETE CASCADE.
    db.execute("DELETE FROM mails WHERE id = ?", (mail_id,))
    db.commit()
    flash("Courriel supprimé.", "success")
    return redirect(url_for("mails"))


@app.route("/mails/<int:mail_id>")
@login_required
def mail_detail(mail_id):
    db = get_db()
    mail = db.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if mail is None:
        abort(404)
    people = _persons_of(db, "mail_persons", "mail_id", mail_id)
    members = db.execute(
        """
        SELECT m.id, COALESCE(m.name, m.email) AS name, m.email
        FROM members m JOIN mail_members mm ON mm.member_id = m.id
        WHERE mm.mail_id = ? ORDER BY name COLLATE NOCASE
        """,
        (mail_id,),
    ).fetchall()
    received_by = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (mail["received_by"],)
    ).fetchone()
    validator = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (mail["validated_by"],)
    ).fetchone()
    return render_template(
        "mail_detail.html", x=mail, people=people, members=members,
        directions=MAIL_DIRECTIONS,
        received_by=received_by["name"] if received_by else None,
        validated_by=validator["name"] if validator else None,
    )


# --------------------------------------------------------------------------- #
# Members (association @pauseia.fr, auto-imported from their élu·e correspondence)
# --------------------------------------------------------------------------- #

@app.route("/membres")
@login_required
def members():
    db = get_db()
    rows = db.execute(
        """
        SELECT m.id, m.email, COALESCE(m.name, m.email) AS name,
               COUNT(mm.mail_id) AS mail_count
        FROM members m
        LEFT JOIN mail_members mm ON mm.member_id = m.id
        GROUP BY m.id
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    return render_template("members.html", members=rows)


@app.route("/membres/<int:member_id>")
@login_required
def member_detail(member_id):
    db = get_db()
    member = db.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    if member is None:
        abort(404)
    mails = db.execute(
        """
        SELECT x.*,
               (SELECT GROUP_CONCAT(p.name, ', ')
                  FROM mail_persons xp JOIN persons p ON p.id = xp.person_id
                 WHERE xp.mail_id = x.id) AS elus
        FROM mails x
        JOIN mail_members mm ON mm.mail_id = x.id
        WHERE mm.member_id = ?
        ORDER BY x.mail_date DESC, x.id DESC
        """,
        (member_id,),
    ).fetchall()
    sent = sum(1 for x in mails if x["direction"] == "sent")
    received = sum(1 for x in mails if x["direction"] == "received")
    conversations = _conversation_groups(db, mails)
    return render_template(
        "member_detail.html", m=member, conversations=conversations,
        sent=sent, received=received, directions=MAIL_DIRECTIONS,
    )


# --------------------------------------------------------------------------- #
# Suivi des échanges: unified, thread-grouped view of all mails
# --------------------------------------------------------------------------- #

def _conversation_groups(db, mails):
    """Group a list of mail rows into conversations (by thread_key, else the mail
    itself). Each group carries a message count, latest date/subject, the élu·es
    and members involved, and an origin type (membre / citoyen / autre)."""
    ids = [m["id"] for m in mails]
    if not ids:
        return []
    qm = ",".join("?" * len(ids))
    tkey = {r[0]: r[1] for r in db.execute(
        f"SELECT mail_id, thread_key FROM mail_thread WHERE mail_id IN ({qm})", ids)}
    elus, membs, kinds = {}, {}, {}
    # The counterpart's own type, so the list can say "Journaliste" where it used
    # to say "élu·e" for everybody. Since the press CRM was merged in, half of
    # these conversations are with journalists.
    for mid, name, ctype in db.execute(
        f"SELECT xp.mail_id, p.name, p.contact_type FROM mail_persons xp "
        f"JOIN persons p ON p.id = xp.person_id WHERE xp.mail_id IN ({qm})", ids):
        elus.setdefault(mid, []).append(name)
        kinds.setdefault(mid, set()).add(ctype)
    for mid, name in db.execute(
        f"SELECT mm.mail_id, COALESCE(m.name, m.email) FROM mail_members mm "
        f"JOIN members m ON m.id = mm.member_id WHERE mm.mail_id IN ({qm})", ids):
        membs.setdefault(mid, []).append(name)

    groups, order = {}, []
    for m in mails:  # mails are expected newest-first
        key = tkey.get(m["id"]) or f"m{m['id']}"
        g = groups.get(key)
        if g is None:
            g = {"key": key, "count": 0, "last_date": m["mail_date"],
                 "kinds": set(),
                 # The Objet is the mail's real subject line. Older rows and
                 # anything imported before the column existed have none, so
                 # they keep falling back to the body.
                 "subject": m["subject"] or m["summary"],
                 # Le corps, conservé à part : c'est lui qui porte la marque
                 # « Mail d'un citoyen à … » posée par le pipeline citoyen, et
                 # non l'objet. Voir le calcul de `type` plus bas.
                 "resumes": [],
                 "elus": set(), "members": set(),
                 "has_doc": False, "directions": set()}
            groups[key] = g
            order.append(key)
        g["count"] += 1
        g["resumes"].append(m["summary"] or "")
        if m["mail_date"] >= g["last_date"]:      # keep the latest message's subject
            g["last_date"] = m["mail_date"]
            g["subject"] = m["subject"] or m["summary"]
        g["elus"].update(elus.get(m["id"], []))
        g["kinds"].update(kinds.get(m["id"], set()))
        g["members"].update(membs.get(m["id"], []))
        g["directions"].add(m["direction"])
        if m["document_stored_name"]:
            g["has_doc"] = True
    convs = [groups[k] for k in order]
    for g in convs:
        # Two independent questions, and the list used to answer only the first:
        # who wrote (a member, an anonymous citizen) and who was written to (an
        # élu·e, a journalist). A press exchange showed up as plain "Membre".
        # Le test portait sur `subject`, qui est l'OBJET du courriel. Or la
        # marque « Mail d'un citoyen à … » est écrite par le pipeline citoyen
        # dans le CORPS (`summary`, voir import_campaign_mails.py), et
        # `g["subject"]` ne retombe sur le corps que si l'objet est vide.
        # Conséquence : tout mail citoyen ayant un objet — c'est-à-dire la
        # quasi-totalité — tombait dans « autre », et la colonne « Origine »
        # restait vide. C'est ce que montre le gabarit : rien n'est affiché
        # pour « autre ».
        citoyen = any(r.startswith("Mail d'un citoyen") for r in g["resumes"])
        g["type"] = ("citoyen" if citoyen
                     else "membre" if g["members"]
                     else "autre")
        # "" quand on ne sait pas, "plusieurs" quand le fil mêle des types :
        # une valeur vide plutôt qu'un caractère d'affichage stocké comme donnée.
        g["kind"] = (g["kinds"].copy().pop() if len(g["kinds"]) == 1
                     else ("plusieurs" if g["kinds"] else ""))
    return convs


@app.route("/echanges")
@login_required
def exchanges():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    typ = request.args.get("type") or ""
    # Les fils se regroupent en Python, donc on ne peut pas paginer par
    # conversation sans couper un fil en deux. On borne plutôt la fenêtre lue :
    # les N courriels les plus récents, N doublable depuis la page. Sans borne,
    # cette requête lisait la table entière à chaque affichage — imperceptible à
    # 2 000 messages, intenable à 30 000.
    window = min(request.args.get("fenetre", type=int) or EXCHANGES_WINDOW,
                 EXCHANGES_WINDOW_MAX)
    mails = db.execute(
        "SELECT id, mail_date, direction, subject, summary, document_stored_name "
        "FROM mails ORDER BY mail_date DESC, id DESC LIMIT ?", (window + 1,)
    ).fetchall()
    truncated = len(mails) > window
    mails = mails[:window]
    kind = request.args.get("kind") or ""
    # "Je suis…" — whose exchanges to show. Remembered in a cookie because the
    # application has no per-user login (one shared password), so this is a
    # convenience, NOT a permission: everything stays visible to everyone, and
    # the interface must not pretend otherwise.
    me = request.args.get("me")
    if me is None:
        me = request.cookies.get("crm_me") or ""
    convs = _conversation_groups(db, mails)
    if typ in ("membre", "citoyen", "autre"):
        convs = [c for c in convs if c["type"] == typ]
    if kind:
        convs = [c for c in convs if kind in c["kinds"]]
    members_list = db.execute(
        "SELECT id, COALESCE(name, email) AS name FROM members "
        "ORDER BY name COLLATE NOCASE").fetchall()
    me_name = next((m["name"] for m in members_list if str(m["id"]) == me), None)
    if me_name:
        convs = [c for c in convs if me_name in c["members"]]
    if q:
        ql = q.lower()
        convs = [c for c in convs if ql in c["subject"].lower()
                 or any(ql in n.lower() for n in c["elus"] | c["members"])]
    response = make_response(render_template(
        "exchanges.html", conversations=convs, q=q, typ=typ, kind=kind,
        contact_types=CONTACT_TYPES, members_list=members_list, me=me,
        me_name=me_name, truncated=truncated, window=window,
        next_window=min(window * 2, EXCHANGES_WINDOW_MAX),
        directions=MAIL_DIRECTIONS))
    if request.args.get("me") is not None:
        # A year, and no personal data in it: a members.id this browser chose.
        response.set_cookie("crm_me", me, max_age=31536000, samesite="Lax",
                            httponly=True,
                            secure=bool(os.environ.get("PRODUCTION")))
    return response


@app.route("/echanges/fil")
@login_required
def conversation(key=None):
    db = get_db()
    key = request.args.get("key", "")
    mails = db.execute(
        """
        SELECT x.*, b.body,
               (SELECT GROUP_CONCAT(p.name, ', ') FROM mail_persons xp
                  JOIN persons p ON p.id = xp.person_id WHERE xp.mail_id = x.id) AS elus,
               (SELECT GROUP_CONCAT(COALESCE(m.name, m.email), ', ') FROM mail_members mm
                  JOIN members m ON m.id = mm.member_id WHERE mm.mail_id = x.id) AS membres
        FROM mails x
        LEFT JOIN mail_thread mt ON mt.mail_id = x.id
        LEFT JOIN mail_bodies b ON b.mail_id = x.id
        WHERE COALESCE(mt.thread_key, 'm' || x.id) = ?
        ORDER BY x.mail_date, x.id
        """,
        (key,),
    ).fetchall()
    if not mails:
        abort(404)
    return render_template("conversation.html", mails=mails,
                           directions=MAIL_DIRECTIONS)


# --------------------------------------------------------------------------- #
# Échanges à rattacher: the queue, out of the CLI and into the interface
# --------------------------------------------------------------------------- #

UNLINKED_STATUSES = ("pending", "absent")


# --------------------------------------------------------------------------- #
# Déposer un courriel à la main
# --------------------------------------------------------------------------- #

def _member_importer():
    """Le module d'import, ou None s'il n'est pas dans le conteneur.

    `utils/` n'est PAS copié par le Dockerfile : il est injecté à l'exécution
    par `docker cp`. Une page qui l'importe au chargement du module ferait donc
    planter l'application entière sur une image fraîche. L'import est donc tardif
    et son échec est une information affichée à l'écran, pas une erreur 500.
    """
    utils = str(BASE_DIR / "utils")
    if utils not in sys.path:
        sys.path.insert(0, utils)
    try:
        import import_member_mails                  # noqa: PLC0415
        return import_member_mails
    except ImportError:
        return None


# Un courriel avec ses pièces jointes tient largement dedans, et ça borne ce
# qu'un dépôt peut coûter en mémoire. MAX_CONTENT_LENGTH borne déjà la requête.
MAX_EML_BYTES = 8 * 1024 * 1024
ALLOWED_EML_SUFFIXES = (".eml", ".msg", ".txt")


@app.route("/echanges/deposer", methods=["GET", "POST"])
@login_required
def deposit():
    """Déposer un ou plusieurs fichiers .eml, traités comme l'import automatique.

    La capture automatique ne retient que ce que l'association a une raison de
    suivre (voir maildomains.in_scope), ce qui laisse dehors un cas courant : le
    journaliste qui écrit depuis son gmail. Ici, quelqu'un choisit délibérément
    de confier un échange au CRM — c'est le consentement qui manque à la
    capture — donc le périmètre restrictif ne s'applique pas.

    Publication directe, sans passer par la modération : la personne qui dépose
    son propre échange sait ce qu'elle dépose.
    """
    importer = _member_importer()
    db_people = get_db().execute(
        "SELECT id, name, contact_type FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
    if request.method == "GET":
        return render_template("deposit.html", importer=bool(importer),
                               people=db_people)

    if importer is None:
        flash("Le module d'import n'est pas présent dans le conteneur "
              "(utils/ n'est pas copié par l'image). Dépôt impossible.", "error")
        return redirect(url_for("deposit"))

    files = [f for f in request.files.getlist("courriels") if f and f.filename]
    if not files:
        flash("Aucun fichier reçu.", "error")
        return redirect(url_for("deposit"))

    db = get_db()
    # Qui est en face, quand l'adresse n'a pas de fiche. Sans cette indication,
    # un dépôt dont la contrepartie est inconnue mettait l'adresse en file et
    # N'ENREGISTRAIT PAS le courriel — c'est pourtant le cas le plus courant du
    # dépôt, le journaliste qui écrit depuis son adresse personnelle.
    rattacher_a = (request.form.get("person_id") or "").strip()
    nom_saisi = (request.form.get("person_nom") or "").strip()
    if not rattacher_a.isdigit() and nom_saisi:
        trouvees = db.execute(
            "SELECT id FROM persons WHERE name = ? COLLATE NOCASE", (nom_saisi,)
        ).fetchall()
        if len(trouvees) == 1:
            rattacher_a = str(trouvees[0]["id"])
        else:
            flash(f"« {nom_saisi} » : "
                  + ("plusieurs fiches portent ce nom."
                     if trouvees else "aucune fiche à ce nom.")
                  + " Choisissez un nom proposé par la liste, ou laissez vide.",
                  "error")
            return redirect(url_for("deposit"))
    rattacher_a = int(rattacher_a) if rattacher_a.isdigit() else None

    results, counts = [], {"imported": 0, "queued": 0,
                           "duplicate": 0, "unmatched": 0, "rejected": 0}
    for storage in files:
        name = storage.filename
        if not name.lower().endswith(ALLOWED_EML_SUFFIXES):
            results.append((name, "rejected", "ce n'est pas un fichier .eml"))
            counts["rejected"] += 1
            continue
        raw = storage.read(MAX_EML_BYTES + 1)
        if len(raw) > MAX_EML_BYTES:
            results.append((name, "rejected", "fichier trop volumineux"))
            counts["rejected"] += 1
            continue
        try:
            msg = email.message_from_bytes(raw)
            state, subject = importer.handle_one_message(
                db, msg, rattacher_a=rattacher_a)
        except Exception as exc:                     # noqa: BLE001
            # Un .eml mal formé ne doit pas emporter les autres fichiers du même
            # dépôt, ni rendre une erreur 500 à quelqu'un qui a juste glissé le
            # mauvais fichier.
            app.logger.exception("dépôt de courriel : %s", name)
            results.append((name, "rejected", f"illisible ({type(exc).__name__})"))
            counts["rejected"] += 1
            continue
        results.append((name, state, subject))
        counts[state] += 1

    db.commit()
    session["deposit_results"] = results
    flash(_deposit_summary(counts), "success" if counts["imported"] else "error")
    return redirect(url_for("deposit"))


def _deposit_summary(counts):
    parts = []
    if counts["imported"]:
        parts.append(f"{counts['imported']} courriel(s) enregistré(s)")
    if counts["queued"]:
        parts.append(f"{counts['queued']} adresse(s) mise(s) en file « à "
                     f"rattacher »")
    if counts["duplicate"]:
        parts.append(f"{counts['duplicate']} déjà connu(s)")
    if counts["unmatched"]:
        parts.append(f"{counts['unmatched']} sans membre PauseIA identifiable")
    if counts["rejected"]:
        parts.append(f"{counts['rejected']} refusé(s)")
    return ", ".join(parts) + "." if parts else "Rien à traiter."


@app.route("/echanges/a-rattacher")
@login_required
def unlinked():
    """Addresses the import saw but could not attach to anybody.

    This queue already existed — `civicrm_pending`, filled by the member import
    and emptied by the CiviCRM sync — but only a script could read it. So the one
    place holding the answer to "why isn't my exchange here?" was a command line.
    A member who cannot find their exchange now sees it waiting, and can say who
    it is without anyone touching the server.
    """
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT email, display, first_seen, last_seen, seen_count, status
              FROM civicrm_pending
             WHERE status IN (?, ?)
             ORDER BY seen_count DESC, last_seen DESC
            """, UNLINKED_STATUSES).fetchall()
    except sqlite3.Error:
        rows = []                      # queue not created yet: nothing to show
    people = db.execute(
        "SELECT id, name, contact_type FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return render_template("unlinked.html", rows=rows, people=people)


@app.route("/echanges/a-rattacher/lier", methods=["POST"])
@login_required
def unlinked_link():
    """Say whose address this is. The mails follow on the next sweep.

    Recording it in `person_emails` rather than overwriting `persons.email` is
    deliberate: someone writes from several addresses, and the one they happened
    to use here is not necessarily their main one. This is exactly what the
    import already learns by itself from a mail thread — done by hand.
    """
    db = get_db()
    address = (request.form.get("email") or "").strip().lower()
    person_id = (request.form.get("person_id") or "").strip()
    nom = (request.form.get("person_nom") or "").strip()
    if not address:
        flash("Indiquez l'adresse et la personne à qui la rattacher.", "error")
        return redirect(url_for("unlinked"))

    # Sans JavaScript, le formulaire n'envoie qu'un nom, tapé avec l'aide du
    # datalist : on le résout ici. Un nom porté par deux fiches n'est jamais
    # tranché au hasard — rattacher un échange à la mauvaise personne est pire
    # que de redemander.
    if not person_id.isdigit() and nom:
        homonymes = db.execute(
            "SELECT id, name FROM persons WHERE name = ? COLLATE NOCASE", (nom,)
        ).fetchall()
        if len(homonymes) == 1:
            person_id = str(homonymes[0]["id"])
        elif len(homonymes) > 1:
            flash(f"Plusieurs fiches portent le nom « {nom} » : ouvrez la fiche "
                  f"voulue et rattachez l'adresse depuis celle-ci.", "error")
            return redirect(url_for("unlinked"))
        else:
            flash(f"Aucune fiche au nom de « {nom} ». Choisissez un nom proposé "
                  f"par la liste, ou créez la fiche d'abord.", "error")
            return redirect(url_for("unlinked"))

    if not person_id.isdigit():
        flash("Indiquez l'adresse et la personne à qui la rattacher.", "error")
        return redirect(url_for("unlinked"))
    person = db.execute(
        "SELECT name FROM persons WHERE id = ?", (int(person_id),)).fetchone()
    if person is None:
        abort(404)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "INSERT OR IGNORE INTO person_emails (email, person_id, source, created_at)"
        " VALUES (?, ?, 'interface', ?)", (address, int(person_id), now))
    db.execute(
        "UPDATE civicrm_pending SET status = 'resolved', resolved_at = ?, "
        "person_id = ? WHERE email = ?", (now, int(person_id), address))
    db.commit()
    flash(f"{address} rattachée à {person['name']}. Les courriels concernés "
          f"seront rattachés au prochain import (moins de 10 minutes).", "success")
    return redirect(url_for("unlinked"))


@app.route("/echanges/a-rattacher/ignorer", methods=["POST"])
@login_required
def unlinked_ignore():
    """Not a person we follow — a robot, a supplier, a personal mail.

    'ignored' is a fourth status the scripts never set and never revisit, so the
    queue stays a list of real questions instead of growing into noise nobody
    reads. Reversible: « Réexaminer » puts it back.
    """
    db = get_db()
    address = (request.form.get("email") or "").strip().lower()
    # Trois états possibles depuis l'interface :
    #   pending  : remise en file (« Réexaminer »)
    #   ignored  : plus proposée au rattachement, mais rien n'est bloqué
    #   refused  : « ne jamais enregistrer » — plus de file ET plus d'import
    #              automatique. Un dépôt manuel reste possible : c'est un geste
    #              délibéré, et c'est la porte de sortie voulue.
    if request.form.get("undo"):
        target = "pending"
    elif request.form.get("refuser"):
        target = "refused"
    else:
        target = "ignored"
    db.execute("UPDATE civicrm_pending SET status = ? WHERE email = ?",
               (target, address))
    db.commit()
    libelle = {
        "pending": "remise en file",
        "ignored": "ignorée",
        "refused": "ne sera plus jamais enregistrée automatiquement",
    }[target]
    flash(f"{address} : {libelle}.", "success")
    return redirect(url_for("unlinked",
                            ignorees=1 if target != "pending" else None))


@app.route("/echanges/a-rattacher/ignorees")
@login_required
def unlinked_ignored():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT email, display, first_seen, last_seen, seen_count, status "
            "FROM civicrm_pending WHERE status IN ('ignored', 'refused') "
            "ORDER BY seen_count DESC, last_seen DESC").fetchall()
    except sqlite3.Error:
        rows = []
    return render_template("unlinked.html", rows=rows, people=[], ignored=True)


@app.route("/mails/uploads/<int:mail_id>")
@login_required
def mail_download(mail_id):
    db = get_db()
    mail = db.execute(
        "SELECT document_stored_name, document_orig_name FROM mails WHERE id = ?",
        (mail_id,),
    ).fetchone()
    if mail is None or not mail["document_stored_name"]:
        abort(404)
    return send_from_directory(
        UPLOAD_DIR,
        mail["document_stored_name"],
        as_attachment=True,
        download_name=mail["document_orig_name"],
    )


# --------------------------------------------------------------------------- #
# Anonymous submissions ("Déclarer une activité" — no password)
# --------------------------------------------------------------------------- #
# These routes are deliberately NOT @login_required. An anonymous user can only
# *add* drafts to the pending_* staging tables; they can never read the real
# data. A certified user later reviews each draft on /moderation.

# Lightweight, dependency-free spam protection for the public forms:
#   * a math captcha whose answer is held in the session, and
#   * an in-memory per-IP rate limit (resets on restart; fine for a small,
#     single-process internal app — swap for Flask-Limiter + Redis if scaled).
RATE_LIMIT_MAX = 10           # submissions allowed...
RATE_LIMIT_WINDOW = 300      # ...per this many seconds, per IP
_submission_log = defaultdict(list)


def _new_captcha():
    """Pick a fresh addition question, stash the answer in the session, and
    return the question text to display."""
    a, b = random.randint(1, 50), random.randint(1, 50)
    session["captcha_answer"] = a + b
    return f"{a} + {b}"


def _check_captcha(errors):
    expected = session.get("captcha_answer")
    given = (request.form.get("captcha") or "").strip()
    if expected is None or given != str(expected):
        errors.append("La vérification anti-robot est incorrecte.")
        # A wrong answer spends rate-limit budget too, so a bot cannot
        # brute-force the captcha with free retries.
        _record_submission()


def _rate_limited():
    """True if the requesting IP has already hit the submission cap. Prunes
    timestamps outside the window as a side effect."""
    ip = request.remote_addr or "unknown"
    now = time.monotonic()
    recent = [t for t in _submission_log[ip] if now - t < RATE_LIMIT_WINDOW]
    _submission_log[ip] = recent
    return len(recent) >= RATE_LIMIT_MAX


def _record_submission():
    _submission_log[request.remote_addr or "unknown"].append(time.monotonic())


def _public_person_names(db):
    """(name, role) for every person, for the anonymous declaration pickers.

    The whole table, deliberately: a picker restricted to elected officials
    made the forms unusable for anything else, and the declarant could type
    the missing name by hand anyway. All a visitor learns from the list is
    that we hold a fiche on somebody — not whether we ever contacted them,
    nor anything they said. Only the name and the role are exposed here;
    the email, the stance, the notes and every other column stay private to
    logged-in members.
    """
    return db.execute(
        "SELECT name, role FROM persons ORDER BY name_key(name)"
    ).fetchall()


@app.route("/declarer")
def declarer():
    return render_template("declarer_home.html")


@app.route("/declarer/personne", methods=["GET", "POST"])
def declarer_person():
    if request.method == "POST" and _rate_limited():
        flash("Trop de déclarations envoyées récemment. Réessayez dans quelques minutes.", "error")
    elif request.method == "POST":
        db = get_db()
        name = (request.form.get("name") or "").strip()
        contact_type = (request.form.get("contact_type") or "").strip()
        if contact_type not in CONTACT_TYPES:
            contact_type = ""
        religion = (request.form.get("religion") or "").strip()
        if religion not in RELIGIONS:
            religion = ""
        territoire = (request.form.get("territoire") or "").strip()
        # No type narrows the whitelist here: the form asks for the type but, as
        # everywhere on these pages, does not insist. A draft that skipped it
        # keeps whatever functions were ticked, and the moderator sets the type
        # at approval — where the real form does require it. Same for the
        # religion: given, it narrows the functions; omitted, every religious
        # one is accepted.
        role = _roles_from_form(contact_type or None, religion or None)
        portefeuille = (request.form.get("portefeuille") or "").strip()
        if not has_portfolio(role):
            portefeuille = ""
        role_detail = (request.form.get("role_detail") or "").strip()
        if not has_role_detail(role):
            role_detail = ""
        proposed_organisation = (request.form.get("proposed_organisation") or "").strip()
        stance = (request.form.get("stance") or "").strip()
        # Non unless the declarant says Oui; the moderator checks it anyway.
        signed_declaration = _signed_declaration_from_form() or 0
        first_contacted, fc_ok = _to_iso(request.form.get("first_contacted"))
        notes = (request.form.get("notes") or "").strip()
        submitted_by = (request.form.get("submitted_by") or "").strip()

        errors = []
        if not name:
            errors.append("Le nom est obligatoire.")
        if not fc_ok:
            errors.append("La date de contact est invalide (format JJ/MM/AAAA).")
        if not submitted_by:
            errors.append("Indiquez votre nom ou pseudo Discord (ou « anonyme »).")
        _check_captcha(errors)

        if not errors:
            db.execute(
                """
                INSERT INTO pending_persons (
                    name, contact_type, role, portefeuille, role_detail,
                    proposed_organisation, religion, territoire,
                    stance, signed_declaration, first_contacted, email, phone,
                    notes, submitted_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, contact_type or None, role or None, portefeuille or None,
                 role_detail or None, proposed_organisation or None,
                 religion or None, territoire or None, stance or None,
                 signed_declaration,
                 first_contacted or None,
                 (request.form.get("email") or "").strip() or None,
                 (request.form.get("phone") or "").strip() or None,
                 notes or None, submitted_by or None, _now()),
            )
            db.commit()
            _record_submission()
            return redirect(url_for("declarer_thanks"))
        for e in errors:
            flash(e, "error")

    return render_template(
        "declarer_person.html", stances=STANCES,
        organisation_names=_public_organisation_names(get_db()),
        form=request.form if request.method == "POST" else {},
        today=date.today().isoformat(), captcha_question=_new_captcha(),
    )


def _public_organisation_names(db):
    """Organisation names, for the pickers on the anonymous declaration forms.

    A média and a groupe politique are both public entities, so listing their
    names leaks nothing. Same for people: _public_person_names lists the whole
    table, and declarants type any name with no fiche yet for a moderator to
    match.
    """
    return db.execute(
        "SELECT name, org_type FROM organisations ORDER BY name COLLATE NOCASE"
    ).fetchall()


def _field(name):
    return (request.form.get(name) or "").strip()


def _declare(template, validate, **extra):
    """Shared flow of every public declaration form.

    `validate(db, errors)` reads the form, appends to `errors`, and returns a
    function that inserts the draft (called only once everything is valid, so
    an uploaded file is never written for a rejected submission).
    """
    if request.method == "POST" and _rate_limited():
        flash("Trop de déclarations envoyées récemment. Réessayez dans quelques minutes.", "error")
    elif request.method == "POST":
        db = get_db()
        errors = []
        insert = validate(db, errors)
        if not _field("submitted_by"):
            errors.append("Indiquez votre nom ou pseudo Discord (ou « anonyme »).")
        _check_captcha(errors)
        if not errors:
            insert()
            db.commit()
            _record_submission()
            return redirect(url_for("declarer_thanks"))
        for e in errors:
            flash(e, "error")

    return render_template(
        template, form=request.form if request.method == "POST" else {},
        organisation_names=_public_organisation_names(get_db()),
        # Same treatment as the organisations: « Personnes concernées » stays
        # free text (a declarant may name somebody with no fiche yet) but comes
        # with a picker of everyone on file — see _public_person_names.
        person_names=_public_person_names(get_db()),
        directions=MAIL_DIRECTIONS, today=date.today().isoformat(),
        captcha_question=_new_captcha(), **extra,
    )


def _validate_organisation_and_people(errors):
    proposed_organisation = _field("proposed_organisation")
    proposed_people = _field("proposed_people")
    if not proposed_organisation:
        errors.append("Indiquez l'organisation concernée.")
    if not proposed_people:
        errors.append("Indiquez la ou les personnes concernées.")
    return proposed_organisation, proposed_people


def _validate_required_link(errors, what):
    link, link_ok = _to_url(request.form.get("link"))
    if not link:
        errors.append(f"Le lien vers {what} est obligatoire.")
    elif not link_ok:
        errors.append("Le lien doit être une adresse web (https://…).")
    return link


@app.route("/declarer/organisation", methods=["GET", "POST"])
def declarer_organisation():
    def validate(db, errors):
        name = _field("name")
        link, link_ok = _to_url(request.form.get("link"))
        if not name:
            errors.append("Le nom de l'organisation est obligatoire.")
        if not link_ok:
            errors.append("Le lien doit être une adresse web (https://…).")
        org_type, media_type = _field("org_type"), _field("media_type")
        orientation, chambre = _field("orientation"), _field("chambre")
        religion, stance = _field("religion"), _field("stance")
        values = (name,
                  org_type if org_type in ORG_TYPES else None,
                  media_type if media_type in MEDIA_TYPES else None,
                  orientation if orientation in ORIENTATIONS else None,
                  chambre if chambre in CHAMBERS else None,
                  religion if religion in RELIGIONS else None,
                  stance if stance in STANCES else None,
                  link or None, _field("notes") or None,
                  _field("submitted_by") or None, _now())
        return lambda: db.execute(
            """
            INSERT INTO pending_organisations (name, org_type, media_type,
                orientation, chambre, religion, stance, link, notes,
                submitted_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return _declare("declarer_organisation.html", validate)


@app.route("/declarer/contenu", methods=["GET", "POST"])
def declarer_content():
    def validate(db, errors):
        proposed_organisation, proposed_people = _validate_organisation_and_people(errors)
        link = _validate_required_link(errors, "le contenu")
        published_on, date_ok = _to_iso(request.form.get("published_on"))
        if not published_on:
            errors.append("La date de publication est obligatoire.")
        elif not date_ok:
            errors.append("La date de publication est invalide (format JJ/MM/AAAA).")
        content_type = _field("content_type")
        # Asked of a declarant and of nobody else: « Autre » is there for
        # whatever the four named types miss, so there is always an answer.
        if not content_type:
            errors.append("Le type de contenu est obligatoire.")
        elif content_type not in CONTENT_TYPES:
            errors.append("Le type de contenu est invalide.")
        values = (proposed_organisation, proposed_people,
                  content_type if content_type in CONTENT_TYPES else None,
                  link, published_on, _field("summary") or None,
                  _field("submitted_by") or None, _now())
        return lambda: db.execute(
            """
            INSERT INTO pending_contents (proposed_organisation, proposed_people,
                content_type, link, published_on, summary, submitted_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return _declare("declarer_content.html", validate, content_types=CONTENT_TYPES)


@app.route("/declarer/intervention", methods=["GET", "POST"])
def declarer_intervention():
    def validate(db, errors):
        proposed_organisation, proposed_people = _validate_organisation_and_people(errors)
        link = _validate_required_link(errors, "l'intervention")
        intervention_date, date_ok = _to_iso(request.form.get("intervention_date"))
        if not intervention_date:
            errors.append("La date de l'intervention est obligatoire.")
        elif not date_ok:
            errors.append("La date de l'intervention est invalide (format JJ/MM/AAAA).")
        intervention_type = _field("intervention_type")
        # Obligatoire ici et nulle part ailleurs, comme le type de contenu :
        # « Autre » est là pour ce que les quatre types nommés ne couvrent pas.
        if not intervention_type:
            errors.append("Le type d'intervention est obligatoire.")
        elif intervention_type not in INTERVENTION_TYPES:
            errors.append("Le type d'intervention est invalide.")
        values = (proposed_organisation, proposed_people, intervention_date,
                  intervention_type if intervention_type in INTERVENTION_TYPES else None,
                  link, _field("summary") or None,
                  _field("submitted_by") or None, _now())
        return lambda: db.execute(
            """
            INSERT INTO pending_interventions (proposed_organisation,
                proposed_people, intervention_date, intervention_type, link,
                summary, submitted_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return _declare("declarer_intervention.html", validate,
                    intervention_types=INTERVENTION_TYPES)


@app.route("/declarer/rencontre", methods=["GET", "POST"])
def declarer_meeting():
    if request.method == "POST" and _rate_limited():
        flash("Trop de déclarations envoyées récemment. Réessayez dans quelques minutes.", "error")
    elif request.method == "POST":
        db = get_db()
        meeting_date, date_ok = _to_iso(request.form.get("meeting_date"))
        meeting_time, time_ok = _to_time(request.form.get("meeting_time"))
        # Asked for, but not insisted on: this form is lenient by design, and a
        # missing format is one more thing the moderator fills in at approval,
        # where the shared rencontre form does require it.
        meeting_format = (request.form.get("meeting_format") or "").strip()
        if meeting_format not in MEETING_FORMATS:
            meeting_format = ""
        meeting_place = (request.form.get("meeting_place") or "").strip()
        summary = (request.form.get("summary") or "").strip()
        follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
        proposed_people = (request.form.get("proposed_people") or "").strip()
        submitted_by = (request.form.get("submitted_by") or "").strip()

        errors = []
        if not proposed_people:
            errors.append("Indiquez la ou les personnes concernées.")
        if not meeting_date:
            errors.append("La date de la rencontre est obligatoire.")
        elif not date_ok:
            errors.append("La date de la rencontre est invalide (format JJ/MM/AAAA).")
        if not time_ok:
            errors.append("L'heure de la rencontre est invalide (format HH:MM).")
        if not summary:
            errors.append("Un bref résumé est obligatoire.")
        if not fu_ok:
            errors.append("La date de relance est invalide (format JJ/MM/AAAA).")
        if not submitted_by:
            errors.append("Indiquez votre nom ou pseudo Discord (ou « anonyme »).")
        file, stored_name, orig_name = _stage_upload(errors)
        _check_captcha(errors)

        if not errors:
            db.execute(
                """
                INSERT INTO pending_meetings (
                    meeting_date, meeting_time, meeting_format, meeting_place,
                    summary, follow_up_date,
                    proposed_people, submitted_by, document_stored_name,
                    document_orig_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (meeting_date, meeting_time or None, meeting_format or None,
                 meeting_place or None, summary,
                 follow_up_date or None,
                 proposed_people, submitted_by or None, stored_name, orig_name,
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
            if file and stored_name:
                file.save(UPLOAD_DIR / stored_name)
            db.commit()
            _record_submission()
            return redirect(url_for("declarer_thanks"))
        for e in errors:
            flash(e, "error")

    return render_template(
        "declarer_meeting.html",
        form=request.form if request.method == "POST" else {},
        people=_public_person_names(get_db()),
        today=date.today().isoformat(), captcha_question=_new_captcha(),
    )


@app.route("/declarer/courriel", methods=["GET", "POST"])
def declarer_mail():
    if request.method == "POST" and _rate_limited():
        flash("Trop de déclarations envoyées récemment. Réessayez dans quelques minutes.", "error")
    elif request.method == "POST":
        db = get_db()
        mail_date, date_ok = _to_iso(request.form.get("mail_date"))
        direction = (request.form.get("direction") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        summary = (request.form.get("summary") or "").strip()
        important = 1 if request.form.get("important") else 0
        follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
        proposed_people = (request.form.get("proposed_people") or "").strip()
        submitted_by = (request.form.get("submitted_by") or "").strip()

        errors = []
        if not proposed_people:
            errors.append("Indiquez la ou les personnes concernées.")
        if not subject:
            errors.append("L'objet du courriel est obligatoire.")
        if not mail_date:
            errors.append("La date du courriel est obligatoire.")
        elif not date_ok:
            errors.append("La date du courriel est invalide (format JJ/MM/AAAA).")
        if direction not in MAIL_DIRECTIONS:
            errors.append("Précisez si le courriel a été envoyé ou reçu.")
        if not summary:
            errors.append("Un bref résumé est obligatoire.")
        if not fu_ok:
            errors.append("La date de relance est invalide (format JJ/MM/AAAA).")
        if not submitted_by:
            errors.append("Indiquez votre nom ou pseudo Discord (ou « anonyme »).")
        file, stored_name, orig_name = _stage_upload(errors)
        _check_captcha(errors)

        if not errors:
            db.execute(
                """
                INSERT INTO pending_mails (
                    mail_date, direction, important, subject, summary, follow_up_date,
                    proposed_people, submitted_by, document_stored_name,
                    document_orig_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mail_date, direction, important, subject, summary,
                 follow_up_date or None,
                 proposed_people, submitted_by or None, stored_name, orig_name,
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
            if file and stored_name:
                file.save(UPLOAD_DIR / stored_name)
            db.commit()
            _record_submission()
            return redirect(url_for("declarer_thanks"))
        for e in errors:
            flash(e, "error")

    return render_template(
        "declarer_mail.html", directions=MAIL_DIRECTIONS,
        form=request.form if request.method == "POST" else {},
        people=_public_person_names(get_db()),
        today=date.today().isoformat(), captcha_question=_new_captcha(),
    )


@app.route("/declarer/merci")
def declarer_thanks():
    return render_template("declarer_thanks.html")


# --------------------------------------------------------------------------- #
# Moderation (certified users review the anonymous drafts)
# --------------------------------------------------------------------------- #

@app.route("/moderation")
@login_required
def moderation():
    db = get_db()
    drafts = {
        table: db.execute(
            f"SELECT * FROM {table} ORDER BY created_at DESC, id DESC"
        ).fetchall()
        for table in PENDING_TABLES
    }
    return render_template(
        "moderation.html",
        pending_persons=drafts["pending_persons"],
        pending_organisations=drafts["pending_organisations"],
        pending_meetings=drafts["pending_meetings"],
        pending_mails=drafts["pending_mails"],
        pending_interventions=drafts["pending_interventions"],
        pending_contents=drafts["pending_contents"],
        directions=MAIL_DIRECTIONS,
    )


def _reject(table, row_id):
    db = get_db()
    db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    db.commit()


# Which staging table each /moderation/<kind>/… URL acts on, and what to call
# it in the confirmation. The keys are the only values the routes accept.
PENDING_KINDS = {
    "personne":     ("pending_persons", "Brouillon de personne rejeté."),
    "organisation": ("pending_organisations", "Brouillon d'organisation rejeté."),
    "rencontre":    ("pending_meetings", "Brouillon de rencontre rejeté."),
    "courriel":     ("pending_mails", "Brouillon de courriel rejeté."),
    "intervention": ("pending_interventions", "Brouillon d'intervention rejeté."),
    "contenu":      ("pending_contents", "Brouillon de contenu rejeté."),
}


@app.route("/moderation/<kind>/<int:pid>/reject", methods=["POST"])
@login_required
def reject_pending(kind, pid):
    if kind not in PENDING_KINDS:
        abort(404)
    table, message = PENDING_KINDS[kind]
    db = get_db()
    # Rencontres and courriels can carry an attached document; rejecting the
    # draft has to take the file with it, or it is orphaned on disk forever.
    if table in ("pending_meetings", "pending_mails"):
        draft = db.execute(
            f"SELECT document_stored_name FROM {table} WHERE id = ?", (pid,)
        ).fetchone()
        if draft:
            _delete_upload(draft["document_stored_name"])
    _reject(table, pid)
    flash(message, "success")
    return redirect(url_for("moderation"))


@app.route("/moderation/personne/<int:pid>/approve", methods=["GET", "POST"])
@login_required
def approve_pending_person(pid):
    db = get_db()
    draft = db.execute(
        "SELECT * FROM pending_persons WHERE id = ?", (pid,)
    ).fetchone()
    if draft is None:
        abort(404)
    ctx = dict(action_url=url_for("approve_pending_person", pid=pid),
               heading="Valider une personne", cancel_url=url_for("moderation"),
               moderation_origin=draft, submitted_by=draft["submitted_by"])

    if request.method == "POST":
        person_id, errors = _save_person(db, None)
        if not errors:
            db.execute("DELETE FROM pending_persons WHERE id = ?", (pid,))
            db.commit()
            flash("Personne validée et ajoutée.", "success")
            return redirect(url_for("person_detail", person_id=person_id))
        for e in errors:
            flash(e, "error")
        return _render_person_form(
            db, 400, form=request.form,
            selected_orgs=set(request.form.getlist("organisation_ids")), **ctx)

    form = _form_from_row(draft)
    form["added_by"] = _submitted_by_moderator(db, draft["submitted_by"]) or ""
    # The declarant typed the média or groupe as free text; tick whatever it
    # matches and name what it did not, so the gap is visible rather than found.
    selected, unmatched = _match_names(db, "organisations",
                                       draft["proposed_organisation"])
    return _render_person_form(
        db, form=form, selected_orgs=selected,
        proposed_organisation=draft["proposed_organisation"],
        unmatched_organisations=unmatched, **ctx)


@app.route("/moderation/organisation/<int:pid>/approve", methods=["GET", "POST"])
@login_required
def approve_pending_organisation(pid):
    db = get_db()
    draft = db.execute(
        "SELECT * FROM pending_organisations WHERE id = ?", (pid,)
    ).fetchone()
    if draft is None:
        abort(404)
    ctx = dict(action_url=url_for("approve_pending_organisation", pid=pid),
               heading="Valider une organisation", cancel_url=url_for("moderation"),
               moderation_origin=draft, submitted_by=draft["submitted_by"])
    if request.method == "POST":
        organisation_id, errors = _save_organisation(db, None)
        if not errors:
            db.execute("DELETE FROM pending_organisations WHERE id = ?", (pid,))
            db.commit()
            flash("Organisation validée et ajoutée.", "success")
            return redirect(url_for("organisation_detail",
                                    organisation_id=organisation_id))
        for e in errors:
            flash(e, "error")
        return _render_organisation_form(db, 400, form=request.form, **ctx)
    form = _form_from_row(draft)
    form["added_by"] = _submitted_by_moderator(db, draft["submitted_by"]) or ""
    return _render_organisation_form(db, form=form, **ctx)


def _approve_pending_linked(pid, table, render, save, detail_endpoint, detail_arg,
                            heading, success):
    """The shared approval flow for a draft contenu / intervention.

    Both name their organisation and their people as free text, so both need the
    same thing: match those names onto real rows, tick what matched, say what
    did not, and hand the moderator the normal form to finish.
    """
    endpoint = request.endpoint
    db = get_db()
    draft = db.execute(f"SELECT * FROM {table} WHERE id = ?", (pid,)).fetchone()
    if draft is None:
        abort(404)
    ctx = dict(action_url=url_for(endpoint, pid=pid), heading=heading,
               cancel_url=url_for("moderation"),
               moderation_origin=draft, submitted_by=draft["submitted_by"])
    if request.method == "POST":
        rec_id, errors = save(db, None)
        if not errors:
            db.execute(f"DELETE FROM {table} WHERE id = ?", (pid,))
            db.commit()
            flash(success, "success")
            return redirect(url_for(detail_endpoint, **{detail_arg: rec_id}))
        for e in errors:
            flash(e, "error")
        return render(
            db, 400, form=request.form,
            selected_ids=set(request.form.getlist("person_ids")),
            selected_mods=set(request.form.getlist("participant_ids")), **ctx)

    form = _form_from_row(draft)
    declarant = _submitted_by_moderator(db, draft["submitted_by"])
    # The declarant is who the record was "saisi" by; for an intervention they
    # also spoke for PauseIA, so they are pre-ticked as a participant too.
    form["recorded_by"] = declarant or ""
    org_ids, org_unmatched = _match_names(db, "organisations",
                                          draft["proposed_organisation"])
    form["organisation_id"] = next(iter(sorted(org_ids)), "")
    people_ids, people_unmatched = _match_names(db, "persons",
                                                draft["proposed_people"])
    return render(
        db, form=form, selected_ids=people_ids,
        selected_mods={declarant} if declarant else set(),
        proposed_organisation=draft["proposed_organisation"],
        unmatched_organisations=org_unmatched,
        proposed_people=draft["proposed_people"],
        unmatched_people=people_unmatched, **ctx)


@app.route("/moderation/contenu/<int:pid>/approve", methods=["GET", "POST"])
@login_required
def approve_pending_content(pid):
    return _approve_pending_linked(
        pid, "pending_contents", _render_content_form, _save_content,
        "content_detail", "content_id", "Valider un contenu",
        "Contenu validé et ajouté.",
    )


@app.route("/moderation/intervention/<int:pid>/approve", methods=["GET", "POST"])
@login_required
def approve_pending_intervention(pid):
    return _approve_pending_linked(
        pid, "pending_interventions", _render_intervention_form,
        _save_intervention, "intervention_detail", "intervention_id",
        "Valider une intervention", "Intervention validée et ajoutée.",
    )


@app.route("/moderation/rencontre/<int:pid>/approve", methods=["GET", "POST"])
@login_required
def approve_pending_meeting(pid):
    db = get_db()
    draft = db.execute(
        "SELECT * FROM pending_meetings WHERE id = ?", (pid,)
    ).fetchone()
    if draft is None:
        abort(404)
    people = _person_choices(db)

    if request.method == "POST":
        meeting_id, errors = _save_meeting(db, None)
        if not errors:
            # Carry over the declarant's attached document unless the moderator
            # uploaded one to replace it. The file already lives on disk under
            # its stored name, so we just transfer the DB reference.
            if draft["document_stored_name"]:
                current_doc = db.execute(
                    "SELECT document_stored_name FROM meetings WHERE id = ?", (meeting_id,)
                ).fetchone()["document_stored_name"]
                if current_doc:
                    _delete_upload(draft["document_stored_name"])  # replaced; drop the draft's file
                else:
                    db.execute(
                        "UPDATE meetings SET document_stored_name = ?, document_orig_name = ? "
                        "WHERE id = ?",
                        (draft["document_stored_name"], draft["document_orig_name"], meeting_id),
                    )
            db.execute("DELETE FROM pending_meetings WHERE id = ?", (pid,))
            db.commit()
            flash("Rencontre validée et ajoutée.", "success")
            return redirect(url_for("meeting_detail", meeting_id=meeting_id))
        for e in errors:
            flash(e, "error")
        form = request.form
        selected = set(request.form.getlist("person_ids"))
        selected_mods = set(request.form.getlist("participant_ids"))
        unmatched = []  # the moderator has now made the call themselves
    else:
        form = _form_from_row(draft)
        selected, unmatched = _match_proposed_people(db, draft["proposed_people"])
        declarant = _submitted_by_moderator(db, draft["submitted_by"])
        # The declarant is who the rencontre was "saisie" by, and they attended
        # it — both fields describe them, not the moderator validating.
        form["recorded_by"] = declarant or ""
        selected_mods = {declarant} if declarant else set()

    return render_template(
        "new_meeting.html", people=people, moderators=_moderators(db), form=form,
        selected_ids=selected, selected_mods=selected_mods,
        unmatched_people=unmatched,
        current=None, today=date.today().isoformat(),
        action_url=url_for("approve_pending_meeting", pid=pid),
        heading="Valider une rencontre", cancel_url=url_for("moderation"),
        proposed_people=draft["proposed_people"],
        proposed_document=draft["document_orig_name"],
        submitted_by=draft["submitted_by"],
    )


@app.route("/moderation/courriel/<int:pid>/approve", methods=["GET", "POST"])
@login_required
def approve_pending_mail(pid):
    db = get_db()
    draft = db.execute(
        "SELECT * FROM pending_mails WHERE id = ?", (pid,)
    ).fetchone()
    if draft is None:
        abort(404)
    people = _person_choices(db)

    if request.method == "POST":
        mail_id, errors = _save_mail(db, None)
        if not errors:
            # Carry over the declarant's attached document unless the moderator
            # uploaded one to replace it (the file already lives on disk).
            if draft["document_stored_name"]:
                current_doc = db.execute(
                    "SELECT document_stored_name FROM mails WHERE id = ?", (mail_id,)
                ).fetchone()["document_stored_name"]
                if current_doc:
                    _delete_upload(draft["document_stored_name"])
                else:
                    db.execute(
                        "UPDATE mails SET document_stored_name = ?, document_orig_name = ? "
                        "WHERE id = ?",
                        (draft["document_stored_name"], draft["document_orig_name"], mail_id),
                    )
            db.execute("DELETE FROM pending_mails WHERE id = ?", (pid,))
            db.commit()
            flash("Courriel validé et ajouté.", "success")
            return redirect(url_for("mail_detail", mail_id=mail_id))
        for e in errors:
            flash(e, "error")
        form = request.form
        selected = set(request.form.getlist("person_ids"))
        unmatched = []  # the moderator has now made the call themselves
    else:
        form = _form_from_row(draft)
        selected, unmatched = _match_proposed_people(db, draft["proposed_people"])
        form["received_by"] = _submitted_by_moderator(db, draft["submitted_by"]) or ""

    return render_template(
        "new_mail.html", people=people, moderators=_moderators(db), directions=MAIL_DIRECTIONS,
        form=form, selected_ids=selected, current=None, unmatched_people=unmatched,
        today=date.today().isoformat(),
        action_url=url_for("approve_pending_mail", pid=pid),
        heading="Valider un courriel", cancel_url=url_for("moderation"),
        proposed_people=draft["proposed_people"],
        proposed_document=draft["document_orig_name"],
        submitted_by=draft["submitted_by"],
    )


# --------------------------------------------------------------------------- #
# Moderators / certified users administration
# --------------------------------------------------------------------------- #

@app.route("/moderateurs")
@login_required
def moderators_admin():
    db = get_db()
    mods = db.execute(
        """
        SELECT mo.*,
            (SELECT COUNT(*) FROM meeting_moderators mm WHERE mm.moderator_id = mo.id)
              + (SELECT COUNT(*) FROM meetings  WHERE validated_by = mo.id)
              + (SELECT COUNT(*) FROM mails     WHERE validated_by = mo.id OR received_by = mo.id)
              + (SELECT COUNT(*) FROM persons   WHERE validated_by = mo.id OR added_by = mo.id)
              AS ref_count
        FROM moderators mo
        ORDER BY mo.name COLLATE NOCASE
        """
    ).fetchall()
    return render_template("moderators.html", moderators=mods)


@app.route("/moderateurs/add", methods=["POST"])
@login_required
def add_moderator():
    db = get_db()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Le nom ou pseudo est obligatoire.", "error")
    elif db.execute(
        "SELECT 1 FROM moderators WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone():
        flash("Cet utilisateurice existe déjà.", "error")
    else:
        db.execute("INSERT INTO moderators (name) VALUES (?)", (name,))
        db.commit()
        flash("Utilisateurice ajouté.", "success")
    return redirect(url_for("moderators_admin"))


@app.route("/moderateurs/<int:mod_id>/delete", methods=["POST"])
@login_required
def delete_moderator(mod_id):
    db = get_db()
    # FK columns are ON DELETE SET NULL / the join is ON DELETE CASCADE, so
    # removing a user leaves existing records intact (just unattributed).
    db.execute("DELETE FROM moderators WHERE id = ?", (mod_id,))
    db.commit()
    flash("Utilisateurice supprimé.", "success")
    return redirect(url_for("moderators_admin"))


# Initialise the database as soon as the module is imported, so it works both
# under `flask run` and `uv run python app.py`.
init_db()


if __name__ == "__main__":
    app.run(debug=True)
