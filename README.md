# Website_meeting

A small internal Flask app for the **PauseIA** team to track its advocacy
outreach: the **people** it talks to (politicians / officials), the **meetings**
it has with them, and the **mails** it exchanges. The interface is in French;
the code and comments are in English. However, this tool can be used by any NGO
that wants to do some kind of lobbying towards French politicians. Apart from
the "Position sur PauseIA" field and the PauseIA colors of the interface, it 
is easily reusable. Note that the entire code was written by Claude and I 
did NOT review it thourougly although I did think about data security. 

The app has **two audiences**, and the features below are split accordingly:

- **Team members who have the shared password** — the full internal journal:
  reading, writing, editing, deleting, moderating submissions, and managing the
  list of certified _utilisateurices_.
- **Contributors who don't have the password** — a public, write-only
  declaration form for signalling a person, a meeting or a mail to the team,
  without ever being able to read the journal.

This is useful to harness the power of the collective while not giving admin 
permissions to untrusted users.
---

## For team members (with the password)

Everything here lives behind the shared password; every route requires a signed
session (`login_required`).

- **People** — name, political group, stance on PauseIA, first-contact date, an
  optional follow-up ("relance") date, and free-form notes. Each person's page
  shows how many mails were sent to / received from them, how many meetings were
  held, and how many days remain until the next planned follow-up.
- **Meetings** — date, time, one or more people met, a short summary, an
  optional detailed report, and an optional attached document (`.pdf` / `.docx`
  / `.odt` / `.txt`). The meetings list is split into **upcoming** (soonest first) and
  **past** (most recent first).
- **Mails** — date, one or more people, direction (sent / received), an
  importance flag, a short summary, an optional follow-up date (for sent mails,
  which propagates onto the linked people's "relance" date), and an optional
  attached document.
- **Provenance / accountability** — meetings and mails record **who did what**,
  chosen from the certified _utilisateurices_ list via dropdowns:
  - **Saisi par** — who entered the record (**mandatory**).
  - **Validé par** — who validated it (**mandatory**).
  - **Qui a participé / reçu** — the _utilisateurices_ involved in the meeting
    or who received the mail.
- **Calendar** — a monthly view (`/calendar`) plotting upcoming meetings and
  follow-up ("relance") dates, with month-to-month navigation.
- **Edit & delete** — every record (person, meeting, mail) can be **edited** via
  a "Modifier" button, or **deleted** from its detail page; meeting/mail
  documents can be replaced or removed when editing.
- **Moderation queue** (`/moderation`) — the submissions sent in by contributors
  without the password land here as **drafts**. A badge in the navigation shows
  how many are pending. For each draft you can:
  - **Approve** — opens the normal create form, prefilled from the draft, so you
    can link the real people / _utilisateurices_ before saving; approving then
    promotes the draft into the real table and removes it from the queue.
  - **Reject** — discards the draft.
- **Utilisateurices** (`/moderateurs`) — manage the list of certified
  _utilisateurices_ (add / remove). These are the names that populate the
  "Saisi par", "Validé par" and "Qui a participé / reçu" dropdowns. Removing an
  _utilisateurice_ leaves already-saved records intact but unlinks them.
- **Search** everywhere, and French dates throughout: **typed and displayed as
  DD/MM/YYYY** (`JJ/MM/AAAA`) and stored internally as ISO.

## For contributors (without the password)

Linked from the login page, the **declaration** flow (`/declarer`) is fully
public — **no password, write-only**. Contributors can never read the journal;
they can only add drafts, which stay isolated in staging tables until a team
member moderates them.

- `/declarer/personne` — signal a person (politician / official).
- `/declarer/rencontre` — signal a meeting.
- `/declarer/courriel` — signal a mail (with optional attached document).
- Each form asks for a **name or Discord tag** (`submitted_by`) so the team
  knows who reported it, and the "people concerned" are given as **free text**
  (the contributor can't browse the real people list).
- After submitting, contributors land on a **thank-you** page (`/declarer/merci`).

Submissions are written into dedicated `pending_*` staging tables that are
physically separate from the real data, so anonymous input never touches the
journal until a team member approves it in the moderation queue.

---

## Data model

Three main entities, each with its own integer primary key, linked
**many-to-many** through join tables, plus a `moderators` (certified
_utilisateurices_) table and a set of `pending_*` staging tables for anonymous
submissions:

```
persons ──< meeting_persons >── meetings ──< meeting_moderators >── moderators
persons ──< mail_persons    >── mails

pending_persons   pending_meetings   pending_mails      (anonymous drafts)
```

- A meeting / mail involves **1..n** persons.
- A person appears in **0..n** meetings and **0..n** mails.
- Join tables use `ON DELETE CASCADE`, so deleting a person or a meeting/mail
  cleans up its links automatically. (`PRAGMA foreign_keys = ON` is set per
  connection.)
- Provenance columns (`recorded_by`, `validated_by`, `added_by`, `received_by`)
  reference `moderators(id)` with `ON DELETE SET NULL`, so removing an
  _utilisateurice_ keeps the record but clears the attribution.

Tables:

| Table                | Key columns |
|----------------------|-------------|
| `persons`            | `id`, `name`, `political_group`, `stance`, `first_contacted`, `follow_up_date`, `notes`, `added_by`, `validated_by`, `created_at` |
| `meetings`           | `id`, `meeting_date`, `meeting_time`, `summary`, `details`, `recorded_by`, `validated_by`, `document_*`, `created_at` |
| `mails`              | `id`, `mail_date`, `direction` (`sent`/`received`), `important`, `summary`, `follow_up_date`, `received_by`, `validated_by`, `document_*`, `created_at` |
| `moderators`         | `id`, `name` — the certified _utilisateurices_ |
| `meeting_persons`    | `(meeting_id, person_id)` |
| `mail_persons`       | `(mail_id, person_id)` |
| `meeting_moderators` | `(meeting_id, moderator_id)` — who took part |
| `pending_persons`    | anonymous person drafts (`submitted_by`, …) |
| `pending_meetings`   | anonymous meeting drafts (`proposed_people` free-text, `submitted_by`, …) |
| `pending_mails`      | anonymous mail drafts (`proposed_people` free-text, `submitted_by`, …) |

The database (`meetings.db`, SQLite) and the `uploads/` folder are created
automatically on first run. `init_db()` also runs lightweight migrations that
add newer columns (follow-up dates, provenance fields, document fields on
drafts) to older tables if they are missing.

## Running locally

```bash
uv run flask --app app run --debug
```

Then open <http://127.0.0.1:5000> and sign in with the shared password. The
public declaration form is reachable from the login page without signing in.

## Deployment

The app is designed to run behind a **reverse proxy** (Caddy / nginx) that
terminates HTTPS and forwards requests to the app.

### Environment variables (all three required in production)

| Variable | Purpose |
|----------|---------|
| `APP_PASSWORD` | The shared team password. **Must** be set — the fallback is a public placeholder. |
| `SECRET_KEY` | Signs the session cookies. Generate once (`python -c "import secrets; print(secrets.token_hex(32))"`) and keep it **stable** across restarts, or every deploy logs everyone out. **Must** be set — the fallback is a public placeholder. |
| `PRODUCTION` | Set to `1` when running behind HTTPS + a reverse proxy. Enables production-only behaviour that is deliberately off in local dev: the session cookie becomes HTTPS-only, and the app reads the real visitor IP from the proxy's `X-Forwarded-For` header (one proxy hop expected). Without it, per-visitor rate limiting on the public forms does not work behind a proxy. Do **not** set it if the app is exposed directly without a proxy. |

### System requirements

- **`libmagic1`** (Debian/Ubuntu package) — required by `python-magic`, which
  verifies that uploaded documents match their file extension. In a Docker
  image: `apt-get install -y libmagic1`.
- **gunicorn with a single worker** (`gunicorn -w 1 app:app`) — the app uses
  SQLite and keeps its rate-limiting state in memory, both of which assume one
  process.

### Data persistence (Docker)

`meetings.db` (the whole database) and `uploads/` (attached documents) live on
disk next to `app.py`. When running in a container, **both must be on a mounted
volume** — otherwise redeploying the container silently destroys all data, and
host-level server backups never see it.

### First run

The database schema is created automatically. Before meetings or mails can be
saved, at least one certified _utilisateurice_ must be added at
`/moderateurs` (the "Saisi par" / "Validé par" dropdowns are mandatory and
start empty).


## Project layout

```
app.py               Flask app: config, schema/migrations, routes
static/style.css     Stylesheet (no build step)
static/form-masks.js Client-side input helpers (date/time masks)
templates/           Jinja templates (base, login, people, meetings, mails,
                     declarer_*, moderation, moderators)
meetings.db          SQLite database (created on first run)
uploads/             Attached documents (created on first run)
```
