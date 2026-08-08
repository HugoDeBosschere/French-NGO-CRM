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
