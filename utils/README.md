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

### Automatic weekly refresh (`sync_officials.py`)

The manual steps above are orchestrated by **`sync_officials.py`**, driven by a
weekly systemd timer (`deploy/sync-officials.{service,timer}`, Monday 05:30) — so
the officials stay in sync on their own. It covers **all four chambers**:
deputies, senators, government **and eurodéputés** (§7 scripts, called from here).
It downloads each source in **pure Python** (no `curl`/`unzip`): the AN open-data
zip (unzipped via `zipfile`), the Sénat API JSON, the government list
(self-fetched), and the MEPs (cached, see §7), then runs extract+insert per
chamber.

- **Per-chamber isolation:** a network or format failure on one chamber is
  reported but never blocks the others; the run exits non-zero if any failed.
- **`in_office` reconciliation:** after a *complete* run, persons present in any
  current roster are marked `in_office=1`, the others `0` (kept for history,
  shown with a "Non élu·e actuellement" badge). Handles the government
  multi-role case; never touches hand-entered rows (`added_by`/`validated_by`).
  Skipped if any chamber failed, so an incomplete union can't retire anyone.
- **Genre:** after the reconciliation it runs `backfill_genre.py --commit`, which
  fills `persons.gender` (Femme / Homme) from the four datasets' civilité, for
  the TODO genre filter. Only a NULL gender on a script-made fiche
  (`added_by IS NULL`, `contact_type = 'Politique'`) is filled; nobody's is
  guessed from a prénom. Run it by hand once after deploying the column:
  `python3 utils/backfill_genre.py` (dry run), then `--commit`.
- **Disk-safe:** the large AN dump is removed after extraction.
- **Election-proof-ish:** the AN dump URL carries the legislature number
  (`AN_LEGISLATURE`, default `17`); bump it after a general election.

The eurodéputé timer (`sync-eurodeputes`) was superseded by this job and has been removed from the repository.

```bash
python3 utils/sync_officials.py     # fetch + extract + insert, all four chambers
```

## 3 bis. The other two types de contact (journalistes, religieux·ses)

`persons` holds three kinds of contact — `Journaliste`, `Politique`,
`Religieux·se` — and `organisations` the three matching kinds: `Média`,
`Groupe politique`, `Culte`. The scripts above seed the political half. These
seed the other two. All are **dry-run by default**: they print what they would
do and write nothing until `--commit`, and all are idempotent, matched by name
*within their own type* so that a journaliste who shares a name with an élu·e
stays a separate fiche.

- **`insert_medias_journalistes.py`** — the 165 médias and 133 journalistes
  that came from the old journalist CRM, hard-coded in the file because that
  database was never version-controlled.
- **`insert_cultes.py`** — the religious organisations: each culte plus the
  body that actually speaks for it (CEF, FPF, CNEF, AEOF, Consistoire central,
  CRIF, Grande Mosquée de Paris, FORIF, UBF, CRCF). Fifteen rows, hard-coded.
- **`extract_eveques.py`** + **`insert_eveques.py`** — the ~120 French bishops
  from the Conférence des évêques de France's own annuaire. Creates each
  diocèse as a `Culte` organisation, sets the fonction from the title
  (`Évêque`, `Archevêque`, `Évêque auxiliaire`, `Cardinal`, `Nonce
  apostolique`) and the see as « Territoire assigné ». `émérite` in a title
  sets `in_office = 0`.

```bash
python3 utils/insert_medias_journalistes.py            # dry run
python3 utils/insert_medias_journalistes.py --commit
python3 utils/insert_cultes.py --commit
python3 utils/extract_eveques.py && python3 utils/insert_eveques.py --commit
```

> **Why there is no equivalent for the other cultes.** There is no open dataset
> worth importing. SIRENE's NAF `94.91Z` (renumbered `94.91Y` under NAF 2025)
> lists ~18 000 religious organisations through
> `recherche-entreprises.api.gouv.fr`, but with no religion label and no
> people — useful to *look one up*, not to import. The FPF, AEOF, Consistoire
> and UBF directories stop at the level of organisations. For Islam nothing at
> all is published: the CFCM lapsed and the Ministère de l'Intérieur has
> declined to release FORIF's membership. Everyone else is entered by hand as
> contact is made.

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

Le script remplit aussi **`photo_url` et `fiche_url`** : le portrait officiel
et le lien vers la fiche de l'assemblée, les mêmes que ceux de l'outil
« Écrire à mes élus ». Ils s'affichent partout où un nom de personne apparaît
(listes, fil de conversation, fiches).

Une URL déjà posée **est rafraîchie** si elle vient manifestement de la source
officielle — le domaine est celui de l'Assemblée, du Sénat ou du Parlement
européen. À chaque législature les portraits changent d'adresse, et les
anciennes resteraient en base en affichant du vide. Tout le reste est réputé
saisi à la main et reste intouchable. Le test porte sur l'hôte et non sur la
fin de la chaîne, sinon `evil-assemblee-nationale.fr` passerait pour officiel.

Un `elus.json` joignable mais presque vide (source refondue, fichier tronqué)
sort **en erreur** plutôt que de rendre 0 sans rien écrire : sans ça, l'alerte
systemd ne partirait jamais.

Ce script ne couvre que les **député·es et sénateur·ices** : `elus.json` est
construit par l'outil « Écrire à mes élus » du site, qui ne s'adresse qu'à
eux. Les **eurodéputé·es** obtiennent leur portrait et leur fiche par
`insert_eurodeputes.py`, qui les dérive de l'identifiant du Parlement européen
(`/mepphoto/<id>.jpg` et `/meps/fr/<id>`), sans appel d'API supplémentaire, et
applique la même règle : remplir ce qui manque, rafraîchir ce qui vient du
Parlement, ne jamais toucher une valeur saisie à la main. La fonction qui
décide si une URL vient d'une source officielle est partagée entre les deux
scripts, et elle compare l'hôte et non la fin de la chaîne.

Minuterie : `sync-elus.timer`, tous les jours à 05:55, avec `OnFailure`.
C'était auparavant un `ExecStartPre` de l'import citoyen précédé d'un « - »,
qui avalait son échec — la synchro pouvait s'arrêter des mois sans que
personne l'apprenne.

## 4 ter. Ce que les élu·es ont dit sur l'IA (`import_veille.py`)

Demande de Romain (27/10/2026) : sur la fiche d'un·e élu·e, voir ses passages
sur l'IA trouvés par la **veille des élu·es** (dépôt
[Pause-IA/declarations-elus-ia](https://github.com/Pause-IA/declarations-elus-ia),
site https://veille.pauseia.fr/). La fiche affiche une section « Sur l'IA,
d'après la veille » : date, type (séance, question écrite), chambre, famille de
mots-clés, extrait avec les mots-clés en gras, lien vers la source officielle.
Elle n'apparaît que pour les élu·es qui ont au moins un passage, et rappelle que
les extraits sont repérés automatiquement : à relire avant toute citation.

```bash
python3 utils/import_veille.py --file synthese_elus.json --dry-run
python3 utils/import_veille.py --file synthese_elus.json
```

- **Source** : `synthese_elus.json`, produit chaque matin par la veille et rangé
  sur l'hôte dans `/opt/volunteer-apps/data/veille-elus/`. `import-veille.service`
  le copie dans le conteneur, puis lance l'import (07:15, heure de Paris).
- **Rapprochement**, du plus sûr au moins sûr : l'identifiant officiel de la
  veille (`ids_officiels` : PA… à l'AN, matricule au Sénat, numéro au PE)
  retrouvé dans `persons.fiche_url` ; sinon le nom (accents et majuscules
  ignorés) s'il ne désigne qu'une fiche, ou une seule fiche dont la fonction
  correspond à la chambre. **Deux homonymes indiscernables sont laissés de côté**
  et listés (« à vérifier ») : un passage sur la mauvaise fiche serait pire
  qu'un passage absent. Les personnalités auditionnées ne sont pas rapprochées.
- **Table `veille_passages`**, réécrite en entier à chaque import : relancer ne
  crée aucun doublon, et une correction côté veille se répercute. Un fichier
  sans aucune personne arrête le script sans rien effacer. Rien d'autre n'est
  modifié dans le CRM.
- **Pas de score** : la fiche montre ce que l'élu·e a dit, sources à l'appui,
  pas le classement de la veille.

Installation sur le serveur :

```bash
sudo cp utils/deploy/import-veille.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start import-veille.service      # un premier passage tout de suite
journalctl -u import-veille.service -n 30
sudo systemctl enable --now import-veille.timer
```

## 4 bis. Fusionner les fiches en double (`fusionner_doublons.py`)

Les scripts d'insertion apparient par (nom normalisé, type de contact) : une
même personne arrivée par deux chemins — import presse, open data
parlementaire, CiviCRM — obtient deux fiches. Trois « Yannick Jadot » liées au
même courriel, c'est ce qu'on a trouvé en production.

**La garantie : aucun échange n'est perdu.** Chaque courriel, rencontre,
intervention, contenu, organisation et adresse apprise de la fiche absorbée
est rattaché à la fiche conservée AVANT toute suppression. Le script recompte,
avant et après, les échanges ayant encore au moins une personne ; s'il en
manquait un seul, il annule tout sans écrire. On compte les échanges et non
les lignes de liaison : fusionner deux fiches liées au même courriel fait
légitimement tomber deux lignes à une.

Les tables à reprendre sont **découvertes dans le schéma** (`PRAGMA
foreign_key_list`), pas listées en dur — une table ajoutée demain sera reprise
sans que personne y pense. `thread_persons`, qui garde des identifiants en CSV
hors clé étrangère, est repris à part : sinon une réponse serait attribuée à
une fiche supprimée et retomberait dans « à rattacher ».

La fiche conservée est celle que la source officielle connaît (elle porte la
fiche parlementaire), à défaut celle qui porte le plus d'échanges, à défaut la
plus complète, à défaut la plus ancienne. Elle récupère les champs vides des
autres, jamais en remplacement ; les fonctions se cumulent.

**Jamais fusionné automatiquement** : deux fiches sans nom (sinon toutes les
fiches vides du CRM deviendraient une seule personne), et deux noms seulement
ressemblants. `--approchants` signale « Flore » / « Flaure » ; un humain
tranche, parce que fusionner deux personnes distinctes est la seule erreur
irréparable de ce script. Pour corriger une coquille : renommer la fiche
depuis `/people/<id>/edit`, le passage suivant fusionnera.

```bash
python3 utils/fusionner_doublons.py --db meetings.db --dry-run --approchants
python3 utils/fusionner_doublons.py --db meetings.db
```

Minuterie : `fusion-doublons.timer`, tous les jours à 07:10, après la synchro
CiviCRM qui en est la principale pourvoyeuse. C'est le **seul travail planifié
qui supprime des lignes**, d'où une sauvegarde en `ExecStartPre` et une limite
de 50 fiches par passage (`--max`) : au-delà, il s'arrête et alerte plutôt que
de nettoyer en masse sans témoin.

## 6. Members' correspondence with élu·es (`import_member_mails.py`)

Tracks the association's own members (@pauseia.fr) exchanging mail with élu·es —
**both directions** (member → élu = `sent`, élu → member = `received`) — and
attributes the member. Companion to the campaign importer; shares the same
matching/dedup helpers and the same DB.

**Capture (no per-member setup, newcomers covered automatically).** A Google
Workspace **content-compliance / routing rule** copies every message where one
side is an élu·e address (`@senat.fr` / `@assemblee-nationale.fr` /
`@europarl.europa.eu`) and the other a `@pauseia.fr` account into one audit
mailbox (e.g. `suivi-membres@pauseia.fr`). Set it up in
`admin.google.com → Apps → Google Workspace → Gmail → Compliance → Content
compliance`: scope = internal sending + receiving, condition = recipient/sender
matches the élu domains, action = add `suivi-membres@pauseia.fr` in Bcc (or
"also deliver to"). The rule applies org-wide, so new members need nothing.

**Members table.** Created on the fly from the mail headers (`Name <email>`): a
member appears the first time they mail an élu·e. Group addresses (`campagne@`,
`contact@`, `all@`, …) are excluded (`GROUP_ADDRESSES` in the script). An
optional Google Directory sync could pre-populate members who haven't mailed yet
— not required for the automation to work.

**Config** (secrets env file): `MEMBER_IMAP_USER`, `MEMBER_IMAP_APP_PASSWORD`
(the audit mailbox), optional `MEMBER_IMAP_HOST`/`PORT`/`MAILBOX`. Shares
`IMAP_DB_PATH` and `IMPORT_AUTO_PUBLISH` with the campaign importer.

```bash
python3 utils/import_member_mails.py --backfill --dry-run --verbose   # preview
python3 utils/import_member_mails.py --backfill --auto-publish         # first pass
python3 utils/import_member_mails.py                                   # daily incremental
```

**Every 10 minutes:** install `deploy/import-member-mails.{service,timer}` the
same way as the campaign unit:
```bash
sudo cp utils/deploy/import-member-mails.service /etc/systemd/system/
sudo cp utils/deploy/import-member-mails.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now import-member-mails.timer
```

New data lives in `members` and `mail_members` (created by the script); the mail
summary also names the member and the élu·e, so it shows in the existing UI
without any change to `app.py`.

**Member address format** doesn't matter: the full address is taken verbatim
from the header (`prenom@`, `prenom.n@`, `p.nom@`, …), so disambiguated logins
work with no special handling. Only the domain (`@pauseia.fr`) and the
group-exclusion list matter.

**Élu·e replies from a non-official address** (personal, cabinet, attaché) are
still attributed, by three layered fallbacks:

1. **Address + learned aliases** — every address in the headers is resolved
   against `persons.email` **and** learned aliases (`person_emails`).
2. **Body scan** — a reply usually quotes the original, which carries the élu·e's
   official address; that is matched even if the reply's `From` is something else.
3. **Thread linking** — `In-Reply-To` / `References` inherit the élu·e from the
   mail this one replies to (`thread_persons`).

**Auto-learning:** when a reply is tied to an élu·e via the thread but comes from
a new non-official address, that address is **recorded as an alias**
(`person_emails`), so all later mails to/from it match directly — no thread
needed. The system gets more robust on its own over time. The only case still
unmatched is a member writing to a brand-new non-official address that has never
appeared in a thread; it resolves itself as soon as one reply threads back.

### Relance prévue et « Qui a reçu / envoyé » (octobre 2026)

Each imported member mail now gets a **follow-up date** (`follow_up_date` = mail
date + `RELANCE_JOURS`, 10 days — the same delay as the app's forms, checked by
`tests/test_relances_membres.py`) and, when the member's header name matches an
utilisateurice's name, **`received_by`**. Before this fix both were left NULL,
so /todo never reminded anyone to follow those mails up.

Mails imported before the fix are repaired once with
`backfill_member_relances.py`: follow-up = date added to the CRM + 10 days, and
`received_by` when the name matches and the field is empty. Only mails linked to
a member (`mail_members`, filled by the importer alone) and drafts submitted by
the importer are touched; an existing follow-up is never overwritten.
Idempotent.

```bash
# Preview first (writes nothing), optionally limited to recent mails:
python3 utils/backfill_member_relances.py --dry-run
python3 utils/backfill_member_relances.py --dry-run --depuis 2026-09-01
# Then for real:
python3 utils/backfill_member_relances.py --depuis 2026-09-01
```

`--depuis` keeps very old mails out: their follow-up would land in the past and
show up as overdue on /todo all at once.

## 7. Eurodéputé·es — automatic weekly refresh (`extract_` + `insert_eurodeputes.py`)

French MEPs are seeded like the other chambers, but — unlike deputies/senators —
their list is kept **in sync automatically**, so a mid-term replacement or a new
intake after a European election appears in the CRM on its own (this closes the
"MEPs present locally but missing on the deployed app" gap).

- **`extract_eurodeputes.py`** — downloads the members *sitting today* from the
  European Parliament open-data API (`data.europarl.europa.eu`, endpoint
  `meps/show-current?country-of-representation=FR`). Emails are **published by
  the Parliament** (`hasEmail`), never guessed. Writes
  `actual_dataset/eurodeputes_fr.json`.
- **`insert_eurodeputes.py`** — upserts them into `persons` (role
  `Député·e européen·ne`, group mapped to the exact `POLITICAL_GROUPS` label,
  email as published). Idempotent: new MEPs inserted, missing emails backfilled,
  departed MEPs **reported but never deleted** (their meeting/mail history is
  kept).

**Why it stays fast and doesn't get rate-limited.** The per-MEP detail endpoint
rate-limits (HTTP 429), and a member's email never changes, so `extract` uses the
previous `eurodeputes_fr.json` as a **cache** and only calls the detail endpoint
for identifiers it has never resolved (a genuine newcomer). A normal week is one
list call and **zero** detail calls; the week after an election, only the handful
of new members. A guard aborts the run (leaving the good file untouched) if more
than a quarter of members come back without an email — the signature of a
throttled run — so a degraded fetch can never overwrite good data.

```bash
python3 utils/extract_eurodeputes.py     # refresh the dataset (cached)
python3 utils/insert_eurodeputes.py      # upsert into persons
```

**Weekly run:** none of its own — `sync-officials` covers the four chambers,
eurodéputé·es included (Monday 05:30). Les unités `sync-eurodeputes.{service,
timer}` ont été **retirées du dépôt** : elles y restaient désactivées, ce qui
revenait à proposer une installation qui ferait tourner deux synchros
concurrentes sur les mêmes fiches. Si elles sont encore installées sur le
serveur :

```bash
sudo systemctl disable --now sync-eurodeputes.timer
sudo rm -f /etc/systemd/system/sync-eurodeputes.{service,timer}
sudo systemctl daemon-reload
```

> **Automatic:** new/replacement MEPs (with email) and political-group changes.
> **Deliberately manual:** removing an MEP who has left the Parliament (kept for
> history), and overwriting an existing official email (never auto-replaced).

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

Works from a Takeout of a **group member's own mailbox** too: individual
("every email") deliveries keep the original `To:`, and **digest / abridged**
deliveries — which bundle several posts into one email — are automatically
exploded into their embedded `message/rfc822` messages, so each post is matched
on its real recipient.

### Last resort: text pasted from the Groups web view (`--pasted`)

When no export is possible at all (no Vault, no admin export, digest-only
mailboxes), the group's web archive can still be **copy-pasted** into a text
file. `--pasted FILE` parses the `to: <élu>` / `À : <élu>` lines, keeps only
official-domain addresses (élu·es), ignores media recipients, and skips anything
already recorded for the same person on the same date (so it won't duplicate the
mbox/IMAP/eml imports).

```bash
python3 utils/import_campaign_mails.py --pasted campagne.txt --dry-run --verbose
python3 utils/import_campaign_mails.py --pasted campagne.txt --auto-publish
```

It only recovers the recipient, date and the fact a mail was sent — not a precise
subject — but that is enough for the CRM's "which élu·e was contacted, and when".

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

### Ce qui est conservé (révisé en octobre 2026)

Jusqu'en octobre 2026, un enregistrement ne gardait que l'**élu·e, la date et
l'objet** : « minimisation », disait le commentaire. En pratique la liste
devenait illisible — impossible de distinguer un premier envoi d'un fil où
l'élu·e avait fini par répondre — et la minimisation était illusoire, puisque
c'est le citoyen lui-même qui met PauseIA en copie cachée de ce qu'il écrit :
un acte délibéré de sa part, pas une captation.

**Le corps est donc conservé**, dans `mail_bodies` et non dans `summary` —
`summary` garde l'étiquette « Mail d'un citoyen à … », qui alimente la colonne
« Origine » du suivi des échanges. Ce que ça implique et qu'il faut savoir : le
corps porte la signature du citoyen, donc son nom et parfois ses coordonnées,
dans une page que lit toute personne ayant le mot de passe partagé.

L'en-tête `From` reste, lui, ni lu ni stocké.

**Le sens de l'échange** (« Envoyé » / « Reçu ») se déduit de deux indices, du
plus sûr au moins sûr : les destinataires visibles (l'élu·e dans `To`/`Cc` =
premier envoi, nous dans `To`/`Cc` sans l'élu·e = le citoyen nous transmet
quelque chose), puis les préfixes d'objet `Re:` / `TR:` / `Fwd:`. Aucun des
deux ne prouve que c'est l'élu·e qui a répondu : le sens est corrigeable à la
main sur la fiche du courriel.

### Rattrapage des anciens corps (`backfill_campagne_corps.py`)

Les courriels importés avant octobre 2026 n'ont pas de corps, et aucun import
futur ne le leur donnera : la déduplication se fait sur le Message-ID, et le
leur est déjà noté dans `imported_mails`. Un `--backfill` ordinaire les
sauterait — précisément ceux qu'on veut enrichir.

Ce script relit la boîte et rapproche chaque message de sa ligne par (date,
objet, ensemble des élu·es destinataires) — `imported_mails` ne garde pas
l'identifiant du courriel créé, il n'y a pas de lien direct.

Les ambiguïtés sont assumées, pas devinées. Pour une campagne, date et objet
sont souvent identiques d'un citoyen à l'autre. Quand plusieurs messages et
plusieurs lignes partagent la même clé : si les lignes sont toutes intactes
(aucune pièce jointe, aucune validation, résumé encore égal à l'étiquette),
elles sont interchangeables et on attribue ; sinon on passe et on le dit.
Attribuer les mots d'un citoyen au mauvais enregistrement serait pire que de
ne rien faire.

```bash
python3 utils/backfill_campagne_corps.py --dry-run          # ne rien écrire
python3 utils/backfill_campagne_corps.py --sens             # + corriger Envoyé/Reçu
python3 utils/backfill_campagne_corps.py --refaire          # + reprendre les corps déjà là
```

Le pendant côté membres est `backfill_member_bodies.py --refaire`, qui relit
la boîte d'audit et rapproche par (sens, date, objet). Les deux sont
nécessaires : chaque pipeline a sa boîte, et `--refaire` côté citoyen ne
touche que les courriels portant l'étiquette « Mail d'un citoyen à … ».

`--refaire` sert quand l'extraction s'améliore : les premiers corps rattrapés
l'ont été par une version qui remplaçait chaque balise HTML par une espace et
ne décodait pas les entités. La source étant la même boîte, les repasser ne
peut qu'améliorer, et le corps n'est modifiable nulle part dans l'interface,
donc aucun travail humain n'est écrasé.

Le rapport final dit combien de corps ont été retrouvés, combien de lignes
n'ont plus de message dans la boîte (supprimé, ou objet/date qui ne
correspondent plus) et combien ont été laissées de côté par prudence.

> ⚠️ **Validate the `To:` test first.** If the Google Group rewrites the `To:`
> header, the address fallback breaks — check `X-Original-To` / `Delivered-To`
> in a real received message, or have the site inject the `X-Elu-Id` marker. This
> conditions the whole matching logic.

### Turnkey deployment (`deploy/deploy.sh`)

On the server, one script does the whole rollout — DB backup, email sync,
backfill, and the systemd timer. It runs the Python **inside the app container**
(via `docker cp` + `docker exec`) so it executes as the user that owns
`meetings.db`; running it on the host as another user fails with "attempt to
write a readonly database". The container needs outbound network (for IMAP and
the elus.json fetch), which it has:

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

## Pont CiviCRM (voir `AUTOMATISATION_CIVICRM.md`)

CiviCRM porte ~12 900 journalistes avec leur adresse et leur média. On ne les
recopie pas : une fiche est créée le jour où un membre échange avec la personne.
Les scripts ci-dessous ne parlent jamais à CiviCRM — ils lisent le JSON que `cv`
a écrit, en lecture seule, orchestrés depuis l'hôte.

| Script | Rôle |
|---|---|
| `backup_db.py` | Sauvegarde cohérente par `VACUUM INTO`, **relue** avant d'être annoncée (`integrity_check` + comptage). **La base étant en WAL, un `cp` ne suffit plus.** `--dir` et `--keep N` pour un usage planifié. |
| `importruns.py` | Trace de chaque exécution d'import ou de synchro (`import_runs`). `--record <script> --status ok\|error` permet à un orchestrateur hôte (civicrm-sync.sh) d'y déposer son résultat. |
| `civicrm.py` | Correspondance CiviCRM → CRM et **test de contrat**. Le seul fichier qu'une mise à jour de CiviCRM peut casser. |
| `civicrm_lookup.py` | File d'attente `civicrm_pending` et création des fiches : `--list-pending`, `--apply`, `--seed`, `--patterns`, `--apply-names`, `--prune`, `--retry-absent`, `--stats`. |
| `import_civicrm_medias.py` | Importe les 168 médias en une fois (organisations, aucun impact sur les sélecteurs de personnes). |
| `importruns.py` | Trace de chaque exécution d'import (`import_runs`) : l'app s'en sert pour afficher la fraîcheur des données et signaler un import en échec. Aucune donnée personnelle. |
| `maildomains.py` | Les domaines des organisations qu'on suit, **lus dans la base** au lieu d'être en dur. Servent au scan du corps des mails et à savoir si une adresse est déjà connue — rien à reporter dans Workspace. |
| `learn_conventions.py` | Apprend la convention d'adresses de **tous** les journalistes de CiviCRM (~12 900), pas seulement de nos fiches : `--file <export>`, `--commit`, `--show`. Ne crée **aucune fiche** — ne garde qu'un domaine, un média, un gabarit, un compteur ; les adresses sont jetées. C'est ce qui rend `cbouchouchi@nouvelobs.com` ou `emmanuel.pall@francetv.fr` reconnaissables alors qu'on n'a qu'une fiche sur ces rédactions. |
| `mailpatterns.py` | La convention d'adresses de chaque média, apprise sur les adresses connues. Sert à **reconnaître** une adresse, jamais à en construire une pour y écrire. Un gabarit est retenu s'il explique au moins 60 % des adresses du domaine (`MIN_SHARE`) : l'unanimité exigée au départ laissait une exception historique annuler une rédaction de 2 000 adresses. |
| `deploy/civicrm-seed.sh` | Amorçage, une fois : crée les fiches d'un groupe presse restreint, sans quoi aucune adresse de presse n'est rattachable ni aucune convention apprenable. |
| `deploy/backup-db.{sh,service,timer}` | Sauvegarde nocturne (03:17) : dans le conteneur, puis **sortie sur l'hôte**, puis hors machine si `RSYNC_DEST`. C'est le seul filet de l'outil. |
| `deploy/civicrm-sync.sh` | Le cycle quotidien : `cv` → `docker cp` → scripts. Planifié à 06:30 par `civicrm-sync.timer`, après l'import des mails de membres de 06:10. |

Ordre de mise en route : `civicrm-seed.sh --commit`, puis activer
`civicrm-sync.timer`. Rien à faire côté Google Workspace — sa règle copie déjà
toute la correspondance de l'association.
