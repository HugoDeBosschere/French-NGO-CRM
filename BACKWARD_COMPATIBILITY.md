# Backward compatibility — merging the journalist CRM into this app

The journalist CRM (`Website_media`) has been folded into this app, which is the
one in production. `journalists` + `media` became the two halves of `persons` +
`organisations`, and the contenus and interventions came with them.

This document records what the merge changed for anything that was already
reading or writing this database: URLs, the schema, the `utils/` scripts, and
the deployment. **Nothing that worked before has been removed** — the one thing
that changed shape is described under "Endpoint names" below, and it does not
affect URLs.

Verified against the production database on 2026-09-14.

---

## 1. The data, after migration

| | before | after |
|---|---|---|
| `persons` | 1044 | 1177 (1044 politiques + 133 journalistes) |
| `organisations` | — | 196 (31 groupes politiques + 165 médias) |
| `person_organisations` | — | 1189 |
| `interventions` / `contents` | — | 2 / 0 |
| `meetings` / `meeting_persons` | 4 / 5 | 4 / 5 (unchanged) |
| `moderators` | 2 | 3 (the média CRM's moderator merged in by name) |

**Row ids are preserved.** Every one of the 1044 pre-existing persons kept its
`id` and `name` through the table rebuild described below, so every
`meeting_persons` / `mail_persons` link, every bookmarked `/people/<id>` URL and
every provenance reference still points at the same person.

## 2. URLs

**No URL that worked before has stopped working.** Confirmed by diffing the
route table against the pre-merge `app.py`.

Unchanged: `/`, `/todo`, `/fait`, `/repartition`, `/calendar`, `/echanges`,
`/echanges/fil`, `/people`, `/people/new`, `/people/<id>`, `/people/<id>/edit`,
`/people/<id>/delete`, all `/meetings/…`, all `/mails/…`, `/membres`,
`/membres/<id>`, `/moderateurs…`, `/moderation`, all `/moderation/…/approve`,
all `/declarer/…`, `/login`, `/logout`, `/uploads/<id>`,
`/mails/uploads/<id>`.

New: `/organisations…`, `/interventions…`, `/contenus…`,
`/declarer/organisation`, `/declarer/intervention`, `/declarer/contenu`, and
the three matching `/moderation/<kind>/<pid>/approve`.

### Endpoint names

The three reject routes were merged into one generic route, because the only
thing that differed between them was the table name:

| before (3 endpoints) | after (1 endpoint) |
|---|---|
| `reject_pending_person` — `/moderation/personne/<pid>/reject` | `reject_pending` — `/moderation/<kind>/<pid>/reject` |
| `reject_pending_meeting` — `/moderation/rencontre/<pid>/reject` | idem |
| `reject_pending_mail` — `/moderation/courriel/<pid>/reject` | idem |

**The URLs are byte-identical** and all three still resolve (verified: they
return 302, not 404). What changed is the *endpoint name*, so a
`url_for('reject_pending_person', …)` in a template would break. The only such
calls were in `templates/moderation.html`, which is updated. `kind` is
whitelisted (`PENDING_KINDS`); anything else is a 404.

## 3. Schema

### Tables changed

**`persons` was rebuilt once** (`_relax_political_group` in `init_db`), to drop
the `NOT NULL` on `political_group` — a journaliste has no groupe politique, and
storing `''` to satisfy the constraint would make "no group" and "group not
filled in" indistinguishable. SQLite cannot relax `NOT NULL` with `ALTER`, hence
the rebuild: create, copy, drop, rename. Columns the running table had but this
version does not know about (such as `follow_up_date_legacy`) are added to the
rebuilt table first and copied, so nothing is silently dropped. The rename runs
under `PRAGMA legacy_alter_table = ON` so the `REFERENCES persons` clauses of
`meeting_persons` / `mail_persons` are left naming `persons`, which is what the
rebuilt table becomes.

Columns added to `persons`: `contact_type` (`NOT NULL DEFAULT 'Politique'`, so
every pre-existing row is a politique — which is all this app tracked),
`phone`, `social_links`.

Columns added to `pending_persons`: `contact_type`, `email`, `phone`,
`proposed_organisation`. Its `political_group` column is kept but no longer
written by the declaration form.

Every other pre-existing table — `meetings`, `mails`, `moderators`, `members`,
`mail_bodies`, `mail_thread`, all the join tables, `pending_meetings`,
`pending_mails` — is **untouched**.

### `persons.political_group` is now a mirror

The column stays, and the app keeps it in step with the person's linked groupe
politique (`_sync_group_mirror`). It is the deliberate compatibility surface:
`utils/insert_*.py` write a group *name*, and `utils/export_contacts_xlsx.py`
and ad-hoc SQL read one. Rules:

- For a **politique**: the name of their groupe politique organisation
  (alphabetically first if they somehow belong to two, since the column holds
  one value).
- For a **journaliste**: always `NULL`.
- It is rewritten whenever a person's links change, whenever an organisation is
  renamed or retyped, and whenever an organisation is deleted.

The source of truth is `person_organisations`. Anything that *writes*
`political_group` and nothing else (an importer, a manual `UPDATE`) will have
its value picked up as an organisation link on the next app start, by
`_seed_organisations_from_groups` — but see §4: the four importers now create
the link themselves, so they don't wait for a restart.

### Tables added

`organisations`, `person_organisations`, `interventions`,
`intervention_persons`, `intervention_moderators`, `contents`,
`content_persons`, `pending_organisations`, `pending_interventions`,
`pending_contents`.

## 4. `utils/` scripts

All were run against a copy of the production database after the change.

| script | status |
|---|---|
| `insert_deputes.py` | **was already broken**, fixed; now links the organisation |
| `insert_senateurices.py` | **was already broken**, fixed; now links the organisation |
| `insert_eurodeputes.py` | **was already broken**, fixed; now links the organisation |
| `insert_gouvernement.py` | **was already broken**, fixed; now links the organisation |
| `sync_officials.py` | unchanged, works |
| `sync_emails_from_elus.py` | unchanged, works |
| `export_contacts_xlsx.py` | unchanged, works (reads the mirror column) |
| `import_member_mails.py` | unchanged, works |
| `import_campaign_mails.py` | unchanged, works |
| `backfill_member_mails.py` | unchanged, works |
| `migrate_notes_to_fields.py` | unchanged, works |
| `extract_*.py` | unchanged, not touched by the merge |
| `tests/` | 10 passed |

Two problems were found and fixed in the four bulk importers:

1. **Pre-existing breakage, unrelated to this merge.** All four inserted into
   `persons.follow_up_date`, a column retired as `follow_up_date_legacy` by an
   earlier change. Every run raised
   `table persons has no column named follow_up_date` and inserted nothing.
   **"Fixed" means one concrete edit per script:** the column name and its
   `NULL` placeholder were deleted from the `INSERT` statement. Nothing else
   about their behaviour changed. The rows already seeded in the database were
   never affected — the breakage only prevented *re-running* an importer to pick
   up newly elected officials.
2. **Caused by this merge, fixed here.** `insert_eurodeputes.py` and
   `insert_gouvernement.py` read `ROLES` out of `app.py` by parsing it with
   `ast.literal_eval`. `ROLES` is now `POLITICAL_ROLES + JOURNALIST_ROLES` — an
   expression, not a list literal — so that read would have raised. Both now
   read `POLITICAL_ROLES`, which is the half they wanted.
   `insert_senateurices.py` reads `POLITICAL_GROUPS`, which is unchanged.

`utils/orglink.py` is new: the shared helper the four importers call to create
the groupe politique organisation and the link. `utils/import_media_crm.py` is
the one-off import of the old CRM (idempotent; rerunning it adds nothing).

Nothing in `utils/deploy/` needed changing: the systemd units and timers call
the same scripts by the same paths.

## 5. Template context

Relevant only if there are local template edits not in this repo.

- Rencontres list: `m['groups']` → `m['organisation_names']`, and the separator
  changed from `,` to `|` (an organisation name may contain a comma).
- People lists on the rencontre / courriel detail pages: `p['political_group']`
  → `p['organisation_names']` plus `p['contact_type']`.
- Person form: `groups` (the `POLITICAL_GROUPS` dict) is no longer passed;
  `organisations` (a row list) is.

## 6. Deployment notes

### The one-time `persons` rebuild is atomic

It runs inside a single `BEGIN EXCLUSIVE`, so it either happens completely or
not at all. Three things follow, each verified against copies of the production
database:

- **An interrupted start changes nothing.** If the process dies mid-rebuild the
  transaction rolls back and `persons` is left exactly as it was, `NOT NULL` and
  all. The next start simply tries again.
- **A concurrent writer cannot lose a write.** An importer writing during the
  rebuild waits for the lock, and fails loudly with `database is locked` if it
  waits too long. It never lands a write in a table that is about to be dropped.
  *An earlier draft of this document said such a write could be silently lost.
  That was wrong:* the copy, the `DROP` and the `RENAME` are one transaction, so
  there is no window between them.
- **A half-finished attempt self-heals.** The scratch table is dropped with
  `DROP TABLE IF EXISTS persons_rebuilt` before each attempt. Without that, one
  interrupted run would have made *every* later start fail with
  `table persons_rebuilt already exists` — the app would not have booted at all.

So **the service does not need to be stopped to deploy this**, and the systemd
importers do not need to be masked first. Take the usual backup anyway.

The app runs `gunicorn -w 1` (README, *System requirements*), so only one
process ever runs `init_db()` and the migration cannot race itself.

### Still true after the merge

- **`persons.follow_up_date` is gone, and stays gone.** Anything outside this
  repo still reading it fails, exactly as it did before the merge.
- **`Website_media/` is now dead.** Its database was the import source and is
  otherwise untouched. Nothing in this app reads it.

### How to deploy this

Two separate things have to reach production, and only one of them travels
through git.

**1. The code.** `meetings.db` is gitignored, so pushing and rebuilding brings
the *schema* change but none of the journalist CRM's rows. On the first start,
`init_db()` runs the three migrations by itself: `_relax_political_group`
(§3), `_seed_organisations_from_groups` — which turns every distinct
`political_group` already in production into an `organisations` row and links
its people — and `_slotify_availability` (§3).

```bash
# locally
git add -A && git commit && git push          # backups are gitignored

# on the server, in /opt/volunteer-apps/apps/website-meeting
git pull
sudo cp meetings.db meetings.db.bak-premerge-$(date +%Y%m%d-%H%M%S)
sudo docker compose build && sudo docker compose up -d   # or your usual image step
sudo docker logs --tail 50 website-meeting-app           # migrations run at import
```

Then open `/people` and check the counts: every existing person should read
« Politique » and carry their groupe politique as an organisation.

**2. The journalist CRM's data.** The 165 médias, 133 journalistes and 2
interventions are now baked into `utils/insert_medias_journalistes.py`, so they
travel through git with the code and no database has to be copied anywhere. Run
it *inside* the container, as the user that owns `meetings.db`, or the write
fails as read-only:

```bash
# on the server, after the code deploy above
sudo docker cp utils website-meeting-app:/app/utils    # image ships no utils/
sudo docker exec website-meeting-app \
     python3 /app/utils/insert_medias_journalistes.py            # dry run
sudo docker exec website-meeting-app \
     python3 /app/utils/insert_medias_journalistes.py --commit
```

It reports what it would do and writes nothing until `--commit`, and it is
idempotent: a rerun reports zeros. Names are matched within a type, so a
journaliste sharing a name with an élu·e stays a separate fiche. Pass
`--added-by NAME` to record someone else in « Qui a ajouté » / « Validé par »
(default: the utilisateurice who entered them locally).

`utils/import_media_crm.py` remains for the other direction — importing
straight from the old CRM's `meetings.db`, if you ever have one to hand — but
the seed script is the path that needs nothing but this repository.

**Order matters between the image and the checkout.** The élu·e importers read
`POLITICAL_ROLES` out of `/app/app.py` (§4), and the systemd units copy
`utils/` from the server checkout into the container on every run. New `utils/`
against an old image raises `POLITICAL_ROLES not found in app.py`; old `utils/`
against a new image keeps raising the pre-existing `follow_up_date` error. Both
fail loudly rather than corrupting anything, but update the two together.

**Nothing else changes.** No new Python dependencies (`pyproject.toml` is
untouched — flask, pytest, python-magic), no new environment variables, no
change to `uploads/`, and the systemd units and timers call the same scripts by
the same paths. Keep `SECRET_KEY` stable as always, or everyone is logged out.

### Rollback

The pre-merge state is on disk:

```
meetings.db.bak-premerge-20260914-232030                 the database before any change
app.py.bak-premerge-20260914-232030                      the app before any change
templates-static.bak-premerge-20260914-232030.tar.gz     templates/ and static/
meetings.db.bak-preimport-20260914-233908                after the schema migration,
                                                         before the média data import
```

Restoring the first three returns the app to its exact pre-merge state. The
schema migration is not reversible in place — roll back by restoring the
database file, not by editing it.

---

# Appendix — adding « Religieux·se », the third type de contact

Added 2026-09-16, after the merge above. Verified against a copy of the
production database the same day.

**Short version: this one is additive, and materially safer than the merge.**
Three nullable `ALTER TABLE … ADD COLUMN`, one new value in each of two
existing lists, and no row of any kind changes type. There is no table rebuild,
no `BEGIN EXCLUSIVE`, no orphan-table recovery — none of the machinery §6
describes is involved.

## What changed

| | before | after |
|---|---|---|
| `CONTACT_TYPES` | Journaliste, Politique | + **Religieux·se** |
| `ORG_TYPES` | Média, Groupe politique | + **Culte** |
| `persons` | — | + `religion`, + `territoire` (both nullable TEXT) |
| `organisations` | — | + `religion` (nullable TEXT) |
| `pending_persons` | — | + `religion`, + `territoire` |
| `pending_organisations` | — | + `religion` |

`religion` takes one of seven values (`RELIGIONS` in `app.py`): Catholicisme,
Protestantisme, Christianisme orthodoxe, Judaïsme, Islam, Bouddhisme, Autre
culte / Interreligieux. `territoire` is « Territoire assigné » — the diocèse,
paroisse or circonscription rabbinique someone answers for, the religious
counterpart of a politique's `circonscription`. Both are **only ever set for a
`contact_type = 'Religieux·se'`**; `_save_person` clears them for anyone else,
exactly as it clears `circonscription` and `portefeuille` for a non-politique.

## Fonctions are now conditioned twice

`ROLES_BY_CONTACT_TYPE` gained a `Religieux·se` entry, and for that type alone a
second level applies: `ROLES_BY_RELIGION` narrows the ~60 religious fonctions to
the chosen culte's. `_roles_from_form` takes an optional `religion` argument to
match. **When no religion is given it falls back to every religious fonction,
never to none** — a fiche whose religion nobody has filled in keeps its roles.

`ROLES` is now `_dedup(POLITICAL_ROLES + JOURNALIST_ROLES + RELIGIOUS_ROLES)`.
The de-duplication is needed because a label can belong to several cultes
(`Archevêque`, `Évêque`, `Diacre`, `Prêtre` and `Moine` are catholic *and*
orthodox titles); the religion is what disambiguates them.

## What is *not* affected

- **No existing row changes.** Verified on a copy of production: 1177 persons,
  196 organisations, 1189 links, identical `contact_type` and `org_type`
  distributions and an identical sum of person ids before and after. `init_db()`
  run twice is a no-op.
- **`persons.political_group`** keeps its meaning exactly. `_sync_group_mirror`
  already filters on `contact_type = 'Politique'`, so a religieux·se's mirror
  stays NULL with no change — confirmed by the guard the bishops importer
  prints (`religieux·ses avec un groupe politique résiduel : 0`).
- **`_link_persons_to_organisation`** needed no change either: it keys off
  `CONTACT_TYPE_BY_ORG_TYPE`, so an intervention on a culte links only
  religieux·ses, and one on a groupe politique still refuses to link them.
- **The four élu·e importers** read `POLITICAL_ROLES` and `POLITICAL_GROUPS`,
  neither of which this touches. `ROLES` remains a call rather than a literal,
  as it has been since the merge, so nothing regressed there.
- **`export_contacts_xlsx.py`** selects named columns and does not see the new
  ones. If « Territoire assigné » should appear in the export, that is a
  separate, deliberate change.
- **URLs and endpoint names** are unchanged. `/people?contact_type=…` and
  `/organisations?org_type=…` simply accept one more value each; the filter
  chips are generated from the lists, so they picked it up with no edit.

## Deploying it

No special procedure and **no need to stop the service**: the migration is three
`ADD COLUMN`s inside `init_db()`, which runs at import time as always. Deploy the
code, restart, done. Then, optionally and in this order:

```bash
python3 utils/insert_cultes.py                    # dry run, read it
python3 utils/insert_cultes.py --commit           # 15 organisations
python3 utils/extract_eveques.py
python3 utils/insert_eveques.py                   # dry run, read it
python3 utils/insert_eveques.py --commit          # ~119 bishops, ~100 diocèses
```

Both refuse to run before the migration has been applied, and both are
idempotent — a rerun reports « déjà présents » and writes nothing.

### Rollback

Nothing in the app reads the new columns for a person who is not a
religieux·se, so rolling the *code* back to the previous version leaves a
working app on the migrated database: the three columns simply sit unread. Only
the rows the seed scripts added would be visible, as people and organisations
of a type the older code does not know — delete them with
`DELETE FROM persons WHERE contact_type = 'Religieux·se'` and the matching
`DELETE FROM organisations WHERE org_type = 'Culte'` if that matters. Take the
usual copy of `meetings.db` before deploying regardless.

