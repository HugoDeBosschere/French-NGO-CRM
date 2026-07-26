# Elected-officials import scripts (`utils/`)

One-off **maintenance scripts** that seed the `persons` table in `meetings.db`
with the official list of sitting deputies and senators.

> ⚠️ Not part of the web app. The DB is filled *before* deployment and left
> alone afterwards (except when a moderator adds a person via the UI). These are
> kept so the lists can be **refreshed later**.

Run them from the repo root. All are **idempotent** (a person whose `name`
already exists is skipped).

## Layout

| Path | What |
|------|------|
| `json/` | Raw Assemblée nationale open-data dump (`acteur/`, `organe/`, `deport/`) |
| `actual_dataset/deputes_officiel.json` | Extracted deputies, nosdeputes.fr format |
| `actual_dataset/senateurices_actifs.json` | Senators, Sénat open-data format |

## 1. Download fresh data

```bash
# Deputies — AN open-data archive (actors + mandates + organs)
curl https://data.assemblee-nationale.fr/static/openData/repository/17/amo/deputes_actifs_mandats_actifs_organes/AMO10_deputes_actifs_mandats_actifs_organes.json.zip -o amo10.zip
unzip -o amo10.zip     # creates/updates json/

# Senators — Sénat API
curl https://www.senat.fr/api-senat/senateurs.json -o actual_dataset/senateurices_actifs.json
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

## 3. Full refresh (from repo root)

```bash
curl …AMO10…zip -o amo10.zip && unzip -o amo10.zip
curl https://www.senat.fr/api-senat/senateurs.json -o actual_dataset/senateurices_actifs.json
python3 utils/extract_deputes.py
python3 utils/insert_deputes.py
python3 utils/insert_senateurices.py
```
