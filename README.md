# French-NGO-CRM

A small internal Flask app for the **PauseIA** team to track its advocacy
outreach: the **people** it talks to — politicians and journalists alike — the
**organisations** they speak for (political groups and media outlets), the
**meetings** it has with them, the **mails** it exchanges, the **interventions**
where PauseIA itself speaks, and the **contenus** those organisations publish. The interface is in French;
the code and comments are in English. However, this tool can be used by any NGO
that wants to do some kind of lobbying towards French politicians. Apart from
the "Position sur PauseIA" field and the PauseIA colors of the interface, it 
is easily reusable. Note that the entire code was written by Claude and I 
did NOT review it thourougly although I did think about data security. 
It is already populated with information about the 577 député.e.s and 
348 sénateurices that were elected officials on the 26-07-2026, plus the 
36 members of the government (ministres, ministres délégué.e.s, Premier 
ministre and Président de la République) as of the 08-08-2026.

The app has **two audiences**, and the features below are split accordingly:

- **Team members who have the shared password** — the full internal journal:
  reading, writing, editing, deleting, moderating submissions, and managing the
  list of certified _utilisateurices_.
- **Contributors who don't have the password** — a public, write-only
  declaration form for signalling a person, an organisation, a meeting, a
  mail, an intervention or a contenu to the team, without ever being able to
  read the journal.

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

A **person** is either a *journaliste* or a *politique* (`contact_type`), and
belongs to **organisations** that are either *médias* or *groupes politiques*
(`org_type`). The type de contact is the one field that conditions the rest:
which functions the form offers, which kind of organisation the person can
belong to, and whether the mandate-only fields (circonscription, portefeuille)
appear. Everything else — email, téléphone, réseaux sociaux, notes, position
sur PauseIA — is the same for everyone.

Entities are linked **many-to-many** through join tables, alongside a
`moderators` (certified _utilisateurices_) table and a set of `pending_*`
staging tables for anonymous submissions:

```
persons ──< person_organisations  >── organisations
persons ──< meeting_persons       >── meetings ──< meeting_moderators   >── moderators
                                              └──< meeting_availability >── moderators
persons ──< mail_persons          >── mails
persons ──< intervention_persons  >── interventions >── organisations
                                              └──< intervention_moderators >── moderators
persons ──< content_persons       >── contents      >── organisations

pending_persons  pending_organisations  pending_meetings
pending_mails    pending_interventions  pending_contents     (anonymous drafts)
```

- A meeting / mail / intervention / contenu involves **1..n** persons.
- A person belongs to **0..n** organisations — a pigiste writes for several
  titles, and an élu·e who changes group can hold both while the change is
  recorded. A *politique* must have at least one groupe politique; a
  *journaliste*'s médias are optional.
- An intervention or a contenu names exactly **one** organisation, and it may
  be a groupe politique as readily as a média.
- Saving an intervention or a contenu links its people to its organisation, but
  **only where the types match**: a journaliste interviewed on a party's own
  channel does not thereby join that party.
- `persons.political_group` still exists, but it is now a **mirror** of the
  person's groupe politique, not the source of truth. The app keeps it in step
  (`_sync_group_mirror`), and it is NULL for a journaliste. It is kept because
  `utils/insert_*.py` and `utils/export_contacts_xlsx.py` write and read it
  directly; see `BACKWARD_COMPATIBILITY.md`.
- Join tables use `ON DELETE CASCADE`, so deleting a person, an organisation or
  a meeting/mail cleans up its links automatically. (`PRAGMA foreign_keys = ON`
  is set per connection.) An organisation still holding contenus or
  interventions refuses to be deleted, so that work cannot be lost by mistake.
- `meeting_format` is `presentiel` or `visio`, with `meeting_place` holding the
  address (required in présentiel) or the link (optional in visio). The column
  is nullable so that rencontres recorded before the field existed keep an
  honest NULL — they display as « Non renseigné » — rather than being
  backfilled with a format nobody chose. The form requires it, so editing an
  old rencontre closes the gap.
- `details` (« Compte rendu détaillé ») is gone: one free-text box is enough,
  so `init_db()` folds whatever it held into `summary` after a blank line and
  retires the column as `details_legacy` — nothing reads it, but the original
  split is still recoverable. Runs once, on both `meetings` and
  `pending_meetings`.
- `alt_dates` holds the candidate dates of a rencontre whose date is not
  settled, comma-joined. Non-NULL is what puts a rencontre on `/repartition`
  instead of in the "à venir" list on `/todo`, and choosing a date clears it
  back to NULL. The main `meeting_date` is always one of the candidates, so a
  rencontre under arbitration still has a real date everywhere else.
- Provenance columns (`recorded_by`, `validated_by`, `added_by`, `received_by`)
  reference `moderators(id)` with `ON DELETE SET NULL`, so removing an
  _utilisateurice_ keeps the record but clears the attribution.

Tables:

| Table                 | Key columns |
|-----------------------|-------------|
| `persons`             | `id`, `name`, `contact_type` (`Journaliste`/`Politique`), `role`, `political_group` (mirror), `stance`, `first_contacted`, `email`, `phone`, `social_links`, `circonscription`, `portefeuille`, `in_office`, `notes`, `added_by`, `validated_by`, `created_at` |
| `organisations`       | `id`, `name`, `org_type` (`Média`/`Groupe politique`), `media_type`, `orientation` (média only), `chambre` (groupe only), `stance`, `link`, `notes`, `added_by`, `validated_by`, `created_at` |
| `meetings`            | `id`, `meeting_date`, `meeting_time`, `meeting_format`, `meeting_place`, `alt_dates`, `summary`, `recorded_by`, `validated_by`, `document_*`, `created_at` |
| `mails`               | `id`, `mail_date`, `direction` (`sent`/`received`), `important`, `subject`, `summary`, `follow_up_date`, `received_by`, `validated_by`, `document_*`, `created_at` |
| `interventions`       | `id`, `organisation_id`, `intervention_date`, `intervention_type`, `link`, `summary`, `recorded_by`, `validated_by`, `created_at` |
| `contents`            | `id`, `organisation_id`, `content_type`, `link`, `published_on`, `summary`, `recorded_by`, `validated_by`, `created_at` |
| `moderators`          | `id`, `name` — the certified _utilisateurices_ |
| `person_organisations`| `(person_id, organisation_id)` |
| `meeting_persons`     | `(meeting_id, person_id)` |
| `mail_persons`        | `(mail_id, person_id)` |
| `intervention_persons`| `(intervention_id, person_id)` |
| `content_persons`     | `(content_id, person_id)` |
| `meeting_moderators`  | `(meeting_id, moderator_id)` — who took part |
| `intervention_moderators` | `(intervention_id, moderator_id)` — who spoke for PauseIA |
| `meeting_availability`| `(meeting_id, moderator_id, on_date)` — who is free on which candidate date, until one is chosen |
| `pending_*`           | anonymous drafts, one table per kind (`submitted_by`, `proposed_people` / `proposed_organisation` free-text, …) |

The database (`meetings.db`, SQLite) and the `uploads/` folder are created
automatically on first run. `init_db()` also runs lightweight migrations that
add newer columns to older tables if they are missing, relax the old NOT NULL
on `political_group`, and turn each distinct groupe politique already stored on
a person into an `organisations` row.

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
static/form-masks.js Client-side input helpers (date/time masks, and the
                     type-de-contact / type-d'organisation form toggles)
templates/           Jinja templates (base, _macros, login, people,
                     organisations, interventions, contenus, meetings, mails,
                     declarer_*, moderation, moderators)
utils/               Bulk importers for the official lists of élu·es, the
                     mail importers, insert_medias_journalistes.py (the médias
                     and journalistes, baked into the script) and
                     import_media_crm.py (import straight from the old
                     journalist CRM's database)
meetings.db          SQLite database (created on first run)
uploads/             Attached documents (created on first run)
BACKWARD_COMPATIBILITY.md  What the merge changed, and what it did not
```
