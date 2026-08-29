# Elected-officials import scripts (`utils/`)

One-off **maintenance scripts** that seed the `persons` table in `meetings.db`
with the official lists of sitting deputies, senators and government members.

> ⚠️ Not part of the web app. The DB is filled *before* deployment and left
> alone afterwards (except when a moderator adds a person via the UI). These are
> kept so the lists can be **refreshed later**.

Run them from the repo root. All are **idempotent**: re-running one never
creates a duplicate person. The deputies and senators scripts simply skip a
`name` that already exists; the government script instead adds the missing role
to that person (see below).

## Layout

| Path | What |
|------|------|
| `json/` | Raw Assemblée nationale open-data dump (`acteur/`, `organe/`, `deport/`) |
| `actual_dataset/deputes_officiel.json` | Extracted deputies, nosdeputes.fr format |
| `actual_dataset/senateurices_actifs.json` | Senators, Sénat open-data format |
| `actual_dataset/gouvernement.json` | Government members, downloaded by `extract_gouvernement.py` |

## 1. Download fresh data

```bash
# Deputies — AN open-data archive (actors + mandates + organs)
curl https://data.assemblee-nationale.fr/static/openData/repository/17/amo/deputes_actifs_mandats_actifs_organes/AMO10_deputes_actifs_mandats_actifs_organes.json.zip -o amo10.zip
unzip -o amo10.zip     # creates/updates json/

# Senators — Sénat API
curl https://www.senat.fr/api-senat/senateurs.json -o actual_dataset/senateurices_actifs.json

# Government — no manual download, extract_gouvernement.py calls the API itself.
```

## 2. Scripts

- **`extract_deputes.py`** — walks `json/acteur/`, keeps only sitting deputies
  (active `ASSEMBLEE` mandate), resolves each group via the `GP` mandate →
  `json/organe/`, and writes `actual_dataset/deputes_officiel.json` in the
  nosdeputes.fr format.
- **`insert_deputes.py`** — inserts those deputies into `persons`: role
  `Député·e`, `political_group` mapped to the exact `POLITICAL_GROUPS` label,
  `stance` `Inconnu`, plus `circonscription` and `email`.
- **`insert_senateurices.py`** — inserts senators: role `Sénateur·ice`, mapped
  group, `circonscription` (the Sénat data has no email). Also checks the group
  labels still match `app.py`.
- **`extract_gouvernement.py`** — downloads the sitting government (ministres,
  ministres délégué·es, secrétaires d'État, Premier·e ministre, Président·e) from
  the *Annuaire de l'administration* API into `actual_dataset/gouvernement.json`.
  The API has no party data, hence `political_group` = `Gouvernement /
  Administration`.
- **`insert_gouvernement.py`** — inserts them. If a member is already in
  `persons` (as `Député·e`, etc.), it **adds** the government role to the ones
  they already hold rather than skipping them, and keeps their real party:
  `"Député·e"` → `"Ministre, Député·e"`.

## 3. Full refresh (from repo root)

```bash
curl …AMO10…zip -o amo10.zip && unzip -o amo10.zip
curl https://www.senat.fr/api-senat/senateurs.json -o actual_dataset/senateurices_actifs.json
python3 utils/extract_deputes.py
python3 utils/insert_deputes.py
python3 utils/insert_senateurices.py
python3 utils/extract_gouvernement.py
python3 utils/insert_gouvernement.py
```

Copy `meetings.db` first: the government insert is the only one that *updates*
existing rows.

## 4. Campaign-mail import (`import_campaign_mails.py`)

Unlike the seed scripts above, this one is **recurring**. It feeds the CRM from
the follow-up mailbox that receives a BCC of every mail citizens send to their
élu·e through the site. It matches each mail's recipient against `persons.email`
and stages one draft per matched person in `pending_mails` — the same moderation
queue as anonymous "declarer" submissions — so a certified (Tier 2) member
validates each import on `/moderation`. Nothing is published automatically.

Only the Python standard library is used (`imaplib`, `email`) — no extra
dependency.

### Configuration (env, never hard-coded)

Add to the server env file (`/opt/volunteer-apps/secrets/website-meeting.env`),
reading the app password from Vaultwarden:

```
IMAP_HOST=imap.gmail.com
IMAP_USER=suivi-campagne@pauseia.fr
IMAP_APP_PASSWORD=xxxxxxxxxxxxxxxx
# optional: IMAP_PORT (993), IMAP_MAILBOX (INBOX), IMAP_DB_PATH (<repo>/meetings.db)
```

`suivi-campagne@pauseia.fr` is a member of the group `campagne@pauseia.fr` (the
BCC target), set to receive every message.

### Running

```bash
# One-off first pass over the whole mailbox history:
python3 utils/import_campaign_mails.py --backfill

# Preview without writing:
python3 utils/import_campaign_mails.py --backfill --dry-run

# Daily incremental (only IMAP UIDs newer than the last processed one):
python3 utils/import_campaign_mails.py
```

Duplicates are avoided two ways: the last processed IMAP UID is remembered per
mailbox (incremental runs fetch only `UID > last`), and every `Message-ID` is
recorded, so a mail is never staged twice even across a backfill/daily overlap.
Both live in tables this script owns (`imported_mail_state`, `imported_mails`),
separate from the app schema.

### Matching

- **Primary:** an `X-Elu-Id` / `X-Depute-Id` header carrying a `persons.id`, if
  the site injects one when generating the mail — match-certain even if the
  official address changes.
- **Fallback:** every address in `To`, `Cc`, `X-Original-To` and `Delivered-To`
  matched case-insensitively against `persons.email`. One draft per matched
  person (a mail may target several élu·es).

> ⚠️ **Validate the `To:` test first.** If the Google Group rewrites the `To:`
> header, the address fallback breaks — check `X-Original-To` / `Delivered-To`
> in a real received message, or have the site inject the `X-Elu-Id` marker. This
> conditions the whole matching logic.

### Cron (daily, inside the Docker host)

```bash
# Run inside the container so the DB path matches (/app/meetings.db), loading the
# secrets env file. Example crontab line on the host:
0 6 * * *  docker exec --env-file /opt/volunteer-apps/secrets/website-meeting.env \
             website-meeting-app python3 /app/utils/import_campaign_mails.py \
             >> /var/log/import_campaign_mails.log 2>&1
```
