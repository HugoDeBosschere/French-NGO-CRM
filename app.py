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

Run with:  uv run flask --app app run --debug
"""

import calendar as pycalendar
import os
import random
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import date, datetime
from functools import wraps
from pathlib import Path

import magic
from flask import (
    Flask,
    abort,
    flash,
    g,
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
DB_PATH = BASE_DIR / "meetings.db"
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
    ],
    "Autre": [
        "Gouvernement / Administration",
        "Collectivité territoriale",
        "Autre / Non applicable",
    ],
}

# The person's actual function/role — single source of truth for the dropdown.
ROLES = [
    "Sénateur·ice",
    "Député·e",
    "Maire·sse",
    "Personnalité publique",
]

# How a person feels about PauseAI — single source of truth for the dropdown.
STANCES = [
    "Favorable",
    "Plutôt favorable",
    "Neutre / indécis",
    "Plutôt opposé",
    "Opposé",
    "Inconnu",
]

# Shown as the "who added this person" value for rows imported in bulk from the
# official lists of elected officials (they have no moderator in `added_by`).
AUTO_IMPORT_LABEL = (
    "Ajout automatique à partir des listes officielles d'élus au 26-07-2026."
)

# Mail directions: stored value -> French label shown in the UI.
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


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def get_db():
    """Return a request-scoped SQLite connection with foreign keys enabled."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):  # noqa: ARG001
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_pending_count():
    """Expose the number of drafts awaiting moderation to every template, so the
    nav can show a badge. Only computed for authenticated (certified) users."""
    if not session.get("authenticated"):
        return {"pending_count": 0}
    db = get_db()
    total = sum(
        db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("pending_persons", "pending_meetings", "pending_mails")
    )
    return {"pending_count": total}


def init_db():
    """Create tables if they don't exist yet, and run lightweight migrations."""
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        -- Certified users (password holders). Just an identity — name or Discord
        -- pseudo. Referenced by the provenance fields on the real records below.
        CREATE TABLE IF NOT EXISTS moderators (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS persons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            role            TEXT,
            political_group TEXT NOT NULL,
            stance          TEXT NOT NULL,
            first_contacted TEXT,
            follow_up_date  TEXT,
            notes           TEXT,
            circonscription TEXT,
            email           TEXT,
            added_by        INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            validated_by    INTEGER REFERENCES moderators(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meetings (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date         TEXT NOT NULL,
            meeting_time         TEXT,
            summary              TEXT NOT NULL,
            details              TEXT,   -- optional "compte rendu détaillé"
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

        CREATE TABLE IF NOT EXISTS mails (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_date            TEXT NOT NULL,
            direction            TEXT NOT NULL,   -- 'sent' or 'received'
            important            INTEGER NOT NULL DEFAULT 0,
            summary              TEXT NOT NULL,
            follow_up_date       TEXT,            -- for sent mails: when to follow up
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

        -- Staging tables. Anonymous users (no password) submit drafts here via
        -- the "Déclarer une activité" forms. A certified user reviews them on the
        -- /moderation page and either promotes a draft into the real table above
        -- or deletes it. Nothing here is ever shown in the normal lists.
        CREATE TABLE IF NOT EXISTS pending_persons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            role            TEXT,
            political_group TEXT,   -- nullable: an anonymous draft may omit it
            stance          TEXT,
            first_contacted TEXT,
            follow_up_date  TEXT,
            notes           TEXT,
            submitted_by    TEXT,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_meetings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date    TEXT NOT NULL,
            meeting_time    TEXT,
            summary         TEXT NOT NULL,
            details         TEXT,   -- optional "compte rendu détaillé"
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
    if "follow_up_date" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN follow_up_date TEXT")
    if "role" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN role TEXT")
    mail_cols = [r[1] for r in db.execute("PRAGMA table_info(mails)")]
    if "follow_up_date" not in mail_cols:
        db.execute("ALTER TABLE mails ADD COLUMN follow_up_date TEXT")
    meeting_cols = [r[1] for r in db.execute("PRAGMA table_info(meetings)")]
    if "meeting_time" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN meeting_time TEXT")
    if "details" not in meeting_cols:
        db.execute("ALTER TABLE meetings ADD COLUMN details TEXT")
    pmeeting_cols = [r[1] for r in db.execute("PRAGMA table_info(pending_meetings)")]
    if "details" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN details TEXT")
    if "document_stored_name" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN document_stored_name TEXT")
    if "document_orig_name" not in pmeeting_cols:
        db.execute("ALTER TABLE pending_meetings ADD COLUMN document_orig_name TEXT")
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
    db.commit()
    db.close()


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
               GROUP_CONCAT(DISTINCT p.political_group) AS groups
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
                WHERE p2.name LIKE ? OR p2.political_group LIKE ? OR m2.summary LIKE ?
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
        "SELECT id FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
    valid_ids = {str(p["id"]) for p in people}

    mod_ids = {str(m["id"]) for m in _moderators(db)}

    meeting_date, date_ok = _to_iso(request.form.get("meeting_date"))
    meeting_time, time_ok = _to_time(request.form.get("meeting_time"))
    summary = (request.form.get("summary") or "").strip()
    details = (request.form.get("details") or "").strip()
    recorded_by = _valid_moderator(db, request.form.get("recorded_by"))
    person_ids = [pid for pid in request.form.getlist("person_ids") if pid in valid_ids]
    participant_ids = [m for m in request.form.getlist("participant_ids") if m in mod_ids]
    validated_by = _valid_moderator(db, request.form.get("validated_by"))
    remove_doc = bool(request.form.get("remove_document"))

    errors = []
    if not person_ids:
        errors.append("Sélectionnez au moins une personne.")
    if not participant_ids:
        errors.append("Indiquez qui a participé à la rencontre.")
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
    if not summary:
        errors.append("Un bref résumé est obligatoire.")
    file, stored_name, orig_name = _stage_upload(errors)

    if errors:
        return None, errors

    person_id_ints = [int(pid) for pid in person_ids]
    participant_id_ints = [int(m) for m in participant_ids]

    if meeting is None:
        cur = db.execute(
            """
            INSERT INTO meetings (
                meeting_date, meeting_time, summary, details, recorded_by, validated_by,
                document_stored_name, document_orig_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_date, meeting_time or None, summary, details or None,
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
            UPDATE meetings SET meeting_date = ?, meeting_time = ?, summary = ?,
                details = ?, recorded_by = ?, validated_by = ?, document_stored_name = ?,
                document_orig_name = ? WHERE id = ?
            """,
            (meeting_date, meeting_time or None, summary, details or None,
             recorded_by, validated_by, new_stored, new_orig, meeting_id),
        )

    if file and stored_name:
        file.save(UPLOAD_DIR / stored_name)
    _set_person_links(db, "meeting_persons", "meeting_id", meeting_id, person_id_ints)
    _set_meeting_moderators(db, meeting_id, participant_id_ints)
    db.commit()
    return meeting_id, []


@app.route("/meetings/new", methods=["GET", "POST"])
@login_required
def new_meeting():
    db = get_db()
    people = db.execute(
        "SELECT id, name, political_group FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
    mods = _moderators(db)

    if request.method == "POST":
        meeting_id, errors = _save_meeting(db, None)
        if not errors:
            flash("Rencontre ajoutée.", "success")
            return redirect(url_for("meeting_detail", meeting_id=meeting_id))
        for e in errors:
            flash(e, "error")
        return (
            render_template(
                "new_meeting.html", people=people, moderators=mods, form=request.form,
                selected_ids=set(request.form.getlist("person_ids")),
                selected_mods=set(request.form.getlist("participant_ids")),
                current=None, today=date.today().isoformat(),
                action_url=url_for("new_meeting"), heading="Nouvelle rencontre",
                cancel_url=url_for("index"),
            ),
            400,
        )

    return render_template(
        "new_meeting.html", people=people, moderators=mods, form={}, selected_ids=set(),
        selected_mods=set(), current=None, today=date.today().isoformat(),
        action_url=url_for("new_meeting"), heading="Nouvelle rencontre",
        cancel_url=url_for("index"),
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
    people = db.execute(
        "SELECT id, name, political_group FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
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
    people = db.execute(
        """
        SELECT p.* FROM persons p
        JOIN meeting_persons mp ON mp.person_id = p.id
        WHERE mp.meeting_id = ?
        ORDER BY p.name COLLATE NOCASE
        """,
        (meeting_id,),
    ).fetchall()
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
        SELECT m.id, m.meeting_date, m.meeting_time, m.summary,
               GROUP_CONCAT(p.name, ', ') AS person_names
        FROM meetings m
        LEFT JOIN meeting_persons mp ON mp.meeting_id = m.id
        LEFT JOIN persons p          ON p.id = mp.person_id
        WHERE m.meeting_date BETWEEN ? AND ?
        GROUP BY m.id
        """,
        (lo, hi),
    ).fetchall()
    followups = db.execute(
        """
        SELECT id, name, follow_up_date FROM persons
        WHERE follow_up_date BETWEEN ? AND ?
        """,
        (lo, hi),
    ).fetchall()

    # Map ISO date -> list of events for that day.
    events = {}
    for m in meetings:
        who = m["person_names"] or m["summary"]
        label = f"{m['meeting_time']} {who}" if m["meeting_time"] else who
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
# Persons
# --------------------------------------------------------------------------- #

@app.route("/people")
@login_required
def people():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    # Counts via correlated subqueries so the three relationships don't multiply.
    base = """
        SELECT p.*,
            (SELECT COUNT(*) FROM meeting_persons mp
             WHERE mp.person_id = p.id) AS meeting_count,
            (SELECT COUNT(*) FROM mail_persons xp
             JOIN mails x ON x.id = xp.mail_id
             WHERE xp.person_id = p.id AND x.direction = 'sent') AS mails_sent,
            (SELECT COUNT(*) FROM mail_persons xp
             JOIN mails x ON x.id = xp.mail_id
             WHERE xp.person_id = p.id AND x.direction = 'received') AS mails_received
        FROM persons p
    """
    if q:
        like = f"%{q}%"
        persons = db.execute(
            base
            + """
            WHERE p.name LIKE ? OR p.role LIKE ? OR p.political_group LIKE ? OR p.stance LIKE ?
            ORDER BY p.name COLLATE NOCASE
            """,
            (like, like, like, like),
        ).fetchall()
    else:
        persons = db.execute(
            base + " ORDER BY p.name COLLATE NOCASE"
        ).fetchall()
    return render_template("people.html", persons=persons, q=q)


def _save_person(db, person):
    """Validate the person form and insert/update. Returns (person_id, errors).

    `person` is the existing row when editing, or None when creating.
    """
    name = (request.form.get("name") or "").strip()
    role = (request.form.get("role") or "").strip()
    political_group = (request.form.get("political_group") or "").strip()
    stance = (request.form.get("stance") or "").strip()
    first_contacted, fc_ok = _to_iso(request.form.get("first_contacted"))
    follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
    notes = (request.form.get("notes") or "").strip()
    circonscription = (request.form.get("circonscription") or "").strip()
    email = (request.form.get("email") or "").strip()
    added_by = _valid_moderator(db, request.form.get("added_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))

    errors = []
    if not name:
        errors.append("Le nom est obligatoire.")
    if not political_group:
        errors.append("Le groupe politique est obligatoire.")
    if not stance:
        errors.append("La position sur PauseAI est obligatoire.")
    if added_by is None:
        errors.append("Indiquez qui a ajouté la personne.")
    if validated_by is None:
        errors.append("Indiquez qui a validé la fiche.")
    if not fc_ok:
        errors.append("La date de contact est invalide (format JJ/MM/AAAA).")
    if not fu_ok:
        errors.append("La date de relance est invalide (format JJ/MM/AAAA).")

    if errors:
        return None, errors

    if person is None:
        cur = db.execute(
            """
            INSERT INTO persons (
                name, role, political_group, stance, first_contacted,
                follow_up_date, notes, circonscription, email,
                added_by, validated_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, role or None, political_group, stance, first_contacted or None,
             follow_up_date or None, notes or None, circonscription or None,
             email or None, added_by, validated_by,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        person_id = cur.lastrowid
    else:
        person_id = person["id"]
        db.execute(
            """
            UPDATE persons SET name = ?, role = ?, political_group = ?, stance = ?,
                first_contacted = ?, follow_up_date = ?, notes = ?,
                circonscription = ?, email = ?,
                added_by = ?, validated_by = ? WHERE id = ?
            """,
            (name, role or None, political_group, stance, first_contacted or None,
             follow_up_date or None, notes or None, circonscription or None,
             email or None, added_by, validated_by, person_id),
        )
    db.commit()
    return person_id, []


@app.route("/people/new", methods=["GET", "POST"])
@login_required
def new_person():
    db = get_db()
    mods = _moderators(db)
    if request.method == "POST":
        person_id, errors = _save_person(db, None)
        if not errors:
            flash("Personne ajoutée.", "success")
            return redirect(url_for("person_detail", person_id=person_id))
        for e in errors:
            flash(e, "error")
        return (
            render_template(
                "new_person.html", groups=POLITICAL_GROUPS, roles=ROLES, stances=STANCES,
                moderators=mods, form=request.form, today=date.today().isoformat(),
                action_url=url_for("new_person"), heading="Nouvelle personne",
                cancel_url=url_for("people"),
            ),
            400,
        )

    return render_template(
        "new_person.html", groups=POLITICAL_GROUPS, roles=ROLES, stances=STANCES,
        moderators=mods, form={}, today=date.today().isoformat(),
        action_url=url_for("new_person"), heading="Nouvelle personne",
        cancel_url=url_for("people"),
    )


@app.route("/people/<int:person_id>/edit", methods=["GET", "POST"])
@login_required
def edit_person(person_id):
    db = get_db()
    person = db.execute(
        "SELECT * FROM persons WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None:
        abort(404)
    mods = _moderators(db)

    if request.method == "POST":
        _, errors = _save_person(db, person)
        if not errors:
            flash("Personne mise à jour.", "success")
            return redirect(url_for("person_detail", person_id=person_id))
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = _form_from_row(person)

    return render_template(
        "new_person.html", groups=POLITICAL_GROUPS, roles=ROLES, stances=STANCES,
        moderators=mods, form=form, today=date.today().isoformat(),
        action_url=url_for("edit_person", person_id=person_id),
        heading="Modifier la personne",
        cancel_url=url_for("person_detail", person_id=person_id),
    )


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
        "SELECT * FROM persons WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None:
        abort(404)
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
    added_by = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (person["added_by"],)
    ).fetchone()
    validator = db.execute(
        "SELECT name FROM moderators WHERE id = ?", (person["validated_by"],)
    ).fetchone()
    return render_template(
        "person_detail.html",
        p=person,
        meetings=meetings,
        mails=mails,
        mails_sent=mails_sent,
        mails_received=mails_received,
        directions=MAIL_DIRECTIONS,
        added_by=added_by["name"] if added_by else AUTO_IMPORT_LABEL,
        validated_by=validator["name"] if validator else None,
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
    if q:
        like = f"%{q}%"
        rows = db.execute(
            base
            + """
            WHERE x.id IN (
                SELECT x2.id FROM mails x2
                LEFT JOIN mail_persons xp2 ON xp2.mail_id = x2.id
                LEFT JOIN persons p2       ON p2.id = xp2.person_id
                WHERE p2.name LIKE ? OR x2.summary LIKE ?
            )
            GROUP BY x.id
            ORDER BY x.mail_date DESC, x.id DESC
            """,
            (like, like),
        ).fetchall()
    else:
        rows = db.execute(
            base + " GROUP BY x.id ORDER BY x.mail_date DESC, x.id DESC"
        ).fetchall()
    return render_template("mails.html", mails=rows, q=q, directions=MAIL_DIRECTIONS)


def _save_mail(db, mail):
    """Validate the mail form and insert/update. Returns (mail_id, errors).

    `mail` is the existing row when editing, or None when creating.
    """
    people = db.execute("SELECT id FROM persons").fetchall()
    valid_ids = {str(p["id"]) for p in people}

    mail_date, date_ok = _to_iso(request.form.get("mail_date"))
    direction = (request.form.get("direction") or "").strip()
    summary = (request.form.get("summary") or "").strip()
    important = 1 if request.form.get("important") else 0
    follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
    person_ids = [pid for pid in request.form.getlist("person_ids") if pid in valid_ids]
    received_by = _valid_moderator(db, request.form.get("received_by"))
    validated_by = _valid_moderator(db, request.form.get("validated_by"))
    remove_doc = bool(request.form.get("remove_document"))

    # A follow-up date only makes sense for a mail we sent.
    if direction != "sent":
        follow_up_date, fu_ok = "", True

    errors = []
    if not person_ids:
        errors.append("Sélectionnez au moins une personne.")
    if received_by is None:
        errors.append("Indiquez qui a reçu le courriel.")
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
                mail_date, direction, important, summary, follow_up_date,
                received_by, validated_by,
                document_stored_name, document_orig_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mail_date, direction, important, summary, follow_up_date or None,
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
            UPDATE mails SET mail_date = ?, direction = ?, important = ?, summary = ?,
                follow_up_date = ?, received_by = ?, validated_by = ?,
                document_stored_name = ?, document_orig_name = ?
            WHERE id = ?
            """,
            (mail_date, direction, important, summary, follow_up_date or None,
             received_by, validated_by, new_stored, new_orig, mail_id),
        )

    if file and stored_name:
        file.save(UPLOAD_DIR / stored_name)
    _set_person_links(db, "mail_persons", "mail_id", mail_id, person_id_ints)
    # Propagate the follow-up date onto the people, so their "relance" reflects
    # the latest plan.
    if follow_up_date:
        db.executemany(
            "UPDATE persons SET follow_up_date = ? WHERE id = ?",
            [(follow_up_date, pid) for pid in person_id_ints],
        )
    db.commit()
    return mail_id, []


@app.route("/mails/new", methods=["GET", "POST"])
@login_required
def new_mail():
    db = get_db()
    people = db.execute(
        "SELECT id, name, political_group FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
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

    return render_template(
        "new_mail.html", people=people, moderators=mods, directions=MAIL_DIRECTIONS,
        form={}, selected_ids=set(), current=None, today=date.today().isoformat(),
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
    people = db.execute(
        "SELECT id, name, political_group FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()
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
    people = db.execute(
        """
        SELECT p.* FROM persons p
        JOIN mail_persons xp ON xp.person_id = p.id
        WHERE xp.mail_id = ?
        ORDER BY p.name COLLATE NOCASE
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
        "mail_detail.html", x=mail, people=people, directions=MAIL_DIRECTIONS,
        received_by=received_by["name"] if received_by else None,
        validated_by=validator["name"] if validator else None,
    )


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


def _depute_names(db):
    """Names of sitting deputies, for the anonymous declaration dropdowns.
    Only deputies are exposed here — that list is public data. Every other
    contact in `persons` stays private to logged-in members."""
    return [
        r[0] for r in db.execute(
            "SELECT name FROM persons WHERE role = 'Député·e' ORDER BY name"
        )
    ]


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
        role = (request.form.get("role") or "").strip()
        political_group = (request.form.get("political_group") or "").strip()
        stance = (request.form.get("stance") or "").strip()
        first_contacted, fc_ok = _to_iso(request.form.get("first_contacted"))
        follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
        notes = (request.form.get("notes") or "").strip()
        submitted_by = (request.form.get("submitted_by") or "").strip()

        errors = []
        if not name:
            errors.append("Le nom est obligatoire.")
        if not fc_ok:
            errors.append("La date de contact est invalide (format JJ/MM/AAAA).")
        if not fu_ok:
            errors.append("La date de relance est invalide (format JJ/MM/AAAA).")
        if not submitted_by:
            errors.append("Indiquez votre nom ou pseudo Discord (ou « anonyme »).")
        _check_captcha(errors)

        if not errors:
            db.execute(
                """
                INSERT INTO pending_persons (
                    name, role, political_group, stance, first_contacted,
                    follow_up_date, notes, submitted_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, role or None, political_group or None, stance or None,
                 first_contacted or None, follow_up_date or None, notes or None,
                 submitted_by or None, datetime.utcnow().isoformat(timespec="seconds")),
            )
            db.commit()
            _record_submission()
            return redirect(url_for("declarer_thanks"))
        for e in errors:
            flash(e, "error")

    return render_template(
        "declarer_person.html", groups=POLITICAL_GROUPS, roles=ROLES,
        stances=STANCES, form=request.form if request.method == "POST" else {},
        today=date.today().isoformat(), captcha_question=_new_captcha(),
    )


@app.route("/declarer/rencontre", methods=["GET", "POST"])
def declarer_meeting():
    if request.method == "POST" and _rate_limited():
        flash("Trop de déclarations envoyées récemment. Réessayez dans quelques minutes.", "error")
    elif request.method == "POST":
        db = get_db()
        meeting_date, date_ok = _to_iso(request.form.get("meeting_date"))
        meeting_time, time_ok = _to_time(request.form.get("meeting_time"))
        summary = (request.form.get("summary") or "").strip()
        details = (request.form.get("details") or "").strip()
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
        if not submitted_by:
            errors.append("Indiquez votre nom ou pseudo Discord (ou « anonyme »).")
        file, stored_name, orig_name = _stage_upload(errors)
        _check_captcha(errors)

        if not errors:
            db.execute(
                """
                INSERT INTO pending_meetings (
                    meeting_date, meeting_time, summary, details, proposed_people,
                    submitted_by, document_stored_name, document_orig_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (meeting_date, meeting_time or None, summary, details or None,
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
        deputies=_depute_names(get_db()),
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
        summary = (request.form.get("summary") or "").strip()
        important = 1 if request.form.get("important") else 0
        follow_up_date, fu_ok = _to_iso(request.form.get("follow_up_date"))
        proposed_people = (request.form.get("proposed_people") or "").strip()
        submitted_by = (request.form.get("submitted_by") or "").strip()
        if direction != "sent":
            follow_up_date, fu_ok = "", True

        errors = []
        if not proposed_people:
            errors.append("Indiquez la ou les personnes concernées.")
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
                    mail_date, direction, important, summary, follow_up_date,
                    proposed_people, submitted_by, document_stored_name,
                    document_orig_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mail_date, direction, important, summary, follow_up_date or None,
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
        deputies=_depute_names(get_db()),
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
    pending_persons = db.execute(
        "SELECT * FROM pending_persons ORDER BY created_at DESC, id DESC"
    ).fetchall()
    pending_meetings = db.execute(
        "SELECT * FROM pending_meetings ORDER BY created_at DESC, id DESC"
    ).fetchall()
    pending_mails = db.execute(
        "SELECT * FROM pending_mails ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return render_template(
        "moderation.html",
        pending_persons=pending_persons,
        pending_meetings=pending_meetings,
        pending_mails=pending_mails,
        directions=MAIL_DIRECTIONS,
    )


def _reject(table, row_id):
    db = get_db()
    db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    db.commit()


@app.route("/moderation/personne/<int:pid>/reject", methods=["POST"])
@login_required
def reject_pending_person(pid):
    _reject("pending_persons", pid)
    flash("Brouillon de personne rejeté.", "success")
    return redirect(url_for("moderation"))


@app.route("/moderation/rencontre/<int:pid>/reject", methods=["POST"])
@login_required
def reject_pending_meeting(pid):
    db = get_db()
    draft = db.execute(
        "SELECT document_stored_name FROM pending_meetings WHERE id = ?", (pid,)
    ).fetchone()
    if draft:
        _delete_upload(draft["document_stored_name"])
    _reject("pending_meetings", pid)
    flash("Brouillon de rencontre rejeté.", "success")
    return redirect(url_for("moderation"))


@app.route("/moderation/courriel/<int:pid>/reject", methods=["POST"])
@login_required
def reject_pending_mail(pid):
    db = get_db()
    draft = db.execute(
        "SELECT document_stored_name FROM pending_mails WHERE id = ?", (pid,)
    ).fetchone()
    if draft:
        _delete_upload(draft["document_stored_name"])
    _reject("pending_mails", pid)
    flash("Brouillon de courriel rejeté.", "success")
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

    if request.method == "POST":
        person_id, errors = _save_person(db, None)
        if not errors:
            db.execute("DELETE FROM pending_persons WHERE id = ?", (pid,))
            db.commit()
            flash("Personne validée et ajoutée.", "success")
            return redirect(url_for("person_detail", person_id=person_id))
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = _form_from_row(draft)

    return render_template(
        "new_person.html", groups=POLITICAL_GROUPS, roles=ROLES, stances=STANCES,
        moderators=_moderators(db), form=form, today=date.today().isoformat(),
        action_url=url_for("approve_pending_person", pid=pid),
        heading="Valider une personne", cancel_url=url_for("moderation"),
        moderation_origin=draft, submitted_by=draft["submitted_by"],
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
    people = db.execute(
        "SELECT id, name, political_group FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()

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
    else:
        form = _form_from_row(draft)
        selected = set()
        selected_mods = set()

    return render_template(
        "new_meeting.html", people=people, moderators=_moderators(db), form=form,
        selected_ids=selected, selected_mods=selected_mods,
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
    people = db.execute(
        "SELECT id, name, political_group FROM persons ORDER BY name COLLATE NOCASE"
    ).fetchall()

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
    else:
        form = _form_from_row(draft)
        selected = set()

    return render_template(
        "new_mail.html", people=people, moderators=_moderators(db), directions=MAIL_DIRECTIONS,
        form=form, selected_ids=selected, current=None,
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
