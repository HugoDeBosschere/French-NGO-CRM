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

## 4. Sync élu·e emails from the sending tool (`sync_emails_from_elus.py`)

The CRM seeds `persons.email` **statically** at import time, and only deputies
were filled — senators (≈348) and government members have none. So mails to
senators never match in the importer below.

The "Écrire à mes élus" site (repo **pauseai-france**) already solves this: its
`scripts/generate-elus.js` builds `src/lib/data/elus.json` from **official open
data** — the data.gouv Assemblée nationale dataset (deputies) and the Sénat ODSEN
dataset (senators) — with a cross-checked `emailConfidence` per address. That file
is the exact source of the address the tool puts in a mail's `To:`. Syncing the
CRM from it makes CRM addresses line up with what is actually sent, so matching
is reliable.

```bash
# From a local checkout of the sending tool (recommended on the server):
python3 utils/sync_emails_from_elus.py --elus ../pauseai-france/src/lib/data/elus.json --dry-run
python3 utils/sync_emails_from_elus.py --elus ../pauseai-france/src/lib/data/elus.json

# Or fetch the committed file straight from GitHub (needs network):
python3 utils/sync_emails_from_elus.py --dry-run
```

Defaults are conservative: only **fills empty** emails, only trusts
**`high`** confidence, matches by normalised full name (both datasets build
`"Prénom Nom"` from the same official sources, so it is exact in practice), and
**never guesses** — a name not matched to a single elus entry is reported. Use
`--overwrite` to also replace a differing address, `--min-confidence medium|low`
to widen, `--db` / `IMAP_DB_PATH` for the DB path. Run this **before** the first
campaign-mail backfill so senator mails match.

> Senators who publish no email anywhere (≈15) stay without one — the sending
> tool falls back to their official contact form, and nothing can match them by
> address. That is expected, not a bug.

## 5. Campaign-mail import (`import_campaign_mails.py`)

Unlike the seed scripts above, this one is **recurring**. It feeds the CRM from
the follow-up mailbox that receives a BCC of every mail citizens send to their
élu·e through the site. It matches each mail's recipient against `persons.email`
(or an `X-Elu-Id` marker) and records one mail per message.

Two output modes:

- **Moderation queue (default).** One draft in `pending_mails` — the same queue
  as anonymous "declarer" submissions — with the matched names in
  `proposed_people`, so a certified (Tier 2) member validates each import on
  `/moderation`. Use this while confirming that matching is reliable.
- **Auto-publish** (`--auto-publish`, or `IMPORT_AUTO_PUBLISH=1`). Writes straight
  into the real `mails` table with a structured `mail_persons` link to every
  matched person — a real mail to several élu·es is one mail with several links,
  never duplicated. Because matching yields a **certain `persons.id`** (email
  match or the marker header), this is a clean full automation once the `To:`
  test below is trusted. Recommended path: run a backfill in moderation mode
  first, confirm the drafts match the right élu·es, then switch the cron to
  `--auto-publish`.

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

# Full automation once matching is trusted (writes straight to the mails table):
python3 utils/import_campaign_mails.py --auto-publish
```

### Historical backfill from the group (`.mbox`)

`suivi-campagne@pauseia.fr` only receives mail sent **after** it joined the group,
so the older history lives in the **Google Group archive** (`campagne@pauseia.fr`),
which IMAP cannot read. **Do not forward the old mails by hand** — a Gmail
"Transférer" rewrites `To:` to the follow-up mailbox and buries the original
recipient in the body, so nothing would match.

Instead export the group archive via **Google Takeout** (→ a `.mbox` file, which
keeps each message's original `To:` header) and import it once:

```bash
python3 utils/import_campaign_mails.py --mbox groupe-campagne.mbox --dry-run
python3 utils/import_campaign_mails.py --mbox groupe-campagne.mbox --auto-publish
```

Same matching and same Message-ID dedup as the IMAP path, so it is safe to run
alongside (or before) the live IMAP import without double-counting.

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

### Turnkey deployment (`deploy/deploy.sh`)

On the server (as the app owner, after the repo checkout is up to date), one
script does the whole rollout — DB backup, email sync, backfill, and the systemd
timer. It runs **host-side** (stdlib-only Python on the persisted DB — no docker
exec):

```bash
cd /opt/volunteer-apps/apps/website-meeting
DRY_RUN=1 bash utils/deploy/deploy.sh                 # preview, writes nothing
bash utils/deploy/deploy.sh                           # real run, moderation queue
AUTO=1 bash utils/deploy/deploy.sh                    # real run, auto-publish
MBOX=~/groupe-campagne.mbox bash utils/deploy/deploy.sh   # + replay group history
```

The daily timer defaults to the moderation queue; add `IMPORT_AUTO_PUBLISH=1` to
the secrets env file to make the daily run auto-publish. The unit files it installs
live in `utils/deploy/`. The manual steps below are the same thing spelled out.

### Deployment on the server (systemd timer, manual)

This is a short periodic job, so a **systemd timer** (oneshot service + timer)
fits better than a 24/7 service — same server, same `systemd`/`/opt` conventions
as the other Pause IA bots. The script runs **inside the app container** so its
DB path (`/app/meetings.db`) is the persisted volume, and the app password is
loaded from the secrets env file (never on the command line).

`/etc/systemd/system/import-campaign-mails.service`:

```ini
[Unit]
Description=Import campaign BCC mails into the CRM moderation queue
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
# Drop --auto-publish while validating; add it once the To: test is trusted.
ExecStart=/usr/bin/docker exec \
    --env-file /opt/volunteer-apps/secrets/website-meeting.env \
    website-meeting-app \
    python3 /app/utils/import_campaign_mails.py
```

`/etc/systemd/system/import-campaign-mails.timer`:

```ini
[Unit]
Description=Run the campaign-mail import daily

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true      # catch up if the server was off at 06:00

[Install]
WantedBy=timers.target
```

Enable and test:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now import-campaign-mails.timer
sudo systemctl list-timers | grep import-campaign   # next run
sudo systemctl start import-campaign-mails.service   # run once, now
sudo journalctl -u import-campaign-mails.service -n 50   # see its output
```

> The **backfill is a one-off** — run it by hand once (not via the timer):
> `sudo docker exec --env-file /opt/volunteer-apps/secrets/website-meeting.env
> website-meeting-app python3 /app/utils/import_campaign_mails.py --backfill`.
> The timer then only picks up new mail (UID-incremental).

> A plain **cron** line works too if you prefer:
> `0 6 * * * docker exec --env-file /opt/volunteer-apps/secrets/website-meeting.env
> website-meeting-app python3 /app/utils/import_campaign_mails.py >> /var/log/import_campaign_mails.log 2>&1`
