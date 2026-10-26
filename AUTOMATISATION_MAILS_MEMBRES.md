# Automatisation du suivi des mails membres ↔ élu·es

Document de synthèse (Phase 2). Décrit l'alimentation automatique du CRM
(*Website Meeting*, `meetings.pauseia.fr`) à partir des mails que les **membres
de l'association** (`@pauseia.fr`) échangent avec les élu·es, **dans les deux
sens**, ainsi que la mise à jour automatique des **eurodéputé·es**.

> Phase 1 (mails citoyens → élu·es) : voir **`AUTOMATISATION_MAILS_CAMPAGNE.md`**.
> Détail technique et options des scripts : voir **`utils/README.md`**.

---

## 1. Objectif

Savoir **quel·le membre a écrit à quel·le élu·e (et inversement), quand, et sur
quel sujet**, sans aucune saisie manuelle — y compris pour les **nouveaux
membres** à venir, sans réglage par personne. Le corps des mails est conservé
(usage interne, très utile pour le suivi).

## 2. Principe — capture invisible côté Gmail

Une **règle de conformité du contenu / routage** Google Workspace copie, en **Cci
invisible**, tout message où un côté est une adresse d'élu·e
(`@senat.fr` / `@assemblee-nationale.fr` / `@europarl.europa.eu`) et l'autre un
compte `@pauseia.fr`, vers une **boîte d'audit** (`suivi-membres@pauseia.fr`).
La règle s'applique à toute l'organisation française → **aucun réglage par
membre**, les nouveaux venus sont couverts automatiquement, et la copie est
**invisible** pour l'expéditeur.

```
Membre @pauseia.fr  ⇄  élu·e (mail entrant OU sortant)
        │  règle de contenu Gmail (Cci invisible, périmètre @pauseia.fr)
        ▼
suivi-membres@pauseia.fr (boîte d'audit lue en IMAP)
        │  import_member_mails.py (timer toutes les 10 min)
        ▼
CRM : mails + mail_persons (élu·e) + mail_members (membre) + corps
```

Réglage de la règle : `admin.google.com → Apps → Google Workspace → Gmail →
Conformité → Conformité du contenu`. Périmètre = envoi **et** réception internes ;
condition = en-têtes complets contenant `@pauseia\.fr` (limite à l'orga française) ;
action = ajouter `suivi-membres@pauseia.fr` en Cci. Prérequis : 2FA activée sur la
boîte d'audit + mot de passe d'application Gmail.

## 3. Classement et rattachement

Pour chaque mail, le script détermine :

- **Le sens** : membre → élu·e = `sent` (envoyé), élu·e → membre = `received` (reçu).
- **Le·la membre** : créé·e à la volée depuis l'en-tête `Name <email>` la première
  fois qu'il/elle écrit. Les adresses de groupe (`campagne@`, `contact@`, `all@`,
  `dons@`, …) sont exclues. **Le format d'adresse n'a pas d'importance** : l'adresse
  complète est prise telle quelle (`prenom@`, `prenom.n@`, `p.nom@`, …).
- **L'élu·e** : matché par adresse (voir §4).
- **La relance prévue** : date du courriel + 10 jours, le même délai que les
  formulaires de saisie (`RELANCE_JOURS`, aligné sur `FOLLOW_UP_DEFAULT_DAYS`
  par un test). Sans elle, le CRM ne rappellerait jamais de relancer l'élu·e.
- **« Qui a reçu / envoyé le mail ? »** : l'utilisateurice du CRM dont le nom
  est celui du membre (même règle que pour une déclaration : majuscules et
  espaces ignorées). Vide si le membre n'est pas utilisateurice ; il reste
  rattaché au courriel comme membre.

> Avant le 26/10/2026, ces deux champs restaient vides. Les courriels déjà
> importés se rattrapent avec `utils/backfill_member_relances.py` (relance =
> date d'ajout dans le CRM + 10 jours) : voir `utils/README.md`, §6.

## 4. Matching robuste des élu·es (adresses non officielles incluses)

Une réponse d'élu·e vient souvent d'une adresse **non officielle** (perso,
cabinet, attaché). Quatre couches, du plus sûr au plus souple :

1. **Adresse + alias appris** — chaque adresse des en-têtes est comparée à
   `persons.email` **et** aux alias déjà appris (`person_emails`).
2. **Scan du corps** — une réponse cite en général le mail d'origine, qui porte
   l'adresse officielle de l'élu·e ; on la matche même si le `From` est autre.
3. **Chaînage de fil** — `In-Reply-To` / `References` héritent l'élu·e du mail
   auquel celui-ci répond (`thread_persons`).
4. **Motif de nom** (repli) — partie locale de l'adresse (`prenom.nom`, `p.nom`,
   préfixe du prénom + nom, avec/sans point) rapprochée d'un·e élu·e **unique** ;
   tout hit part **en modération**, jamais publié directement.

**Auto-apprentissage** : quand une réponse est rattachée à un·e élu·e par le fil
mais vient d'une nouvelle adresse non officielle, cette adresse est **enregistrée
comme alias** → tous les mails suivants la matchent directement. Le système se
fiabilise seul avec le temps.

## 5. Affichage — « Suivi des échanges »

Nouvel onglet **Suivi des échanges** : une vue unifiée (citoyens + membres +
élu·es), présentée comme une boîte mail.

- Les échanges successifs d'un même fil sont **regroupés sur une seule ligne**,
  avec un **compteur discret** (« N messages ») ; le clic ouvre le fil complet.
- Chaque ligne indique le **type** (👥 Membre / Citoyen) pour distinguer un membre
  de l'association d'un·e citoyen·ne lambda.
- Le **corps** de chaque message est consultable dans le fil.
- Filtres par objet / élu·e / membre et par type.

La liste des **membres** est un **sous-onglet de « Suivi des échanges »**
(onglets *Échanges* | *Membres*), pour rester dans le même univers fonctionnel
sans alourdir le menu du haut. Les fiches membre et les fiches **Personnes**
(élu·es) affichent leurs échanges de la même façon. Une conversation s'ouvre en
**vue boîte mail** : chaque message en carte (expéditeur → destinataires, objet,
corps), anciens messages repliés, le dernier ouvert.

## 5 bis. Ce qui entre dans la file « à rattacher », et ce qui n'y entre pas

La boîte d'audit reçoit **toute** la correspondance externe de l'association :
la règle Workspace est une regex sur `@pauseia.fr`, sans distinction. Les mails
personnels d'un membre y sont donc aussi.

Mettre en file toute adresse non reconnue revenait à recopier ces tiers — le
médecin, la banque, la famille — avec leur nom, dans une page que consulte
toute l'équipe. La file n'admet donc une adresse que pour une **raison
positive** (`maildomains.in_scope`) :

| Entre en file | N'entre pas |
|---|---|
| Un domaine déjà porté par une fiche (`lefigaro.fr`) | Une messagerie grand public (`gmail.com`, `orange.fr`) |
| Un domaine de média attesté par CiviCRM (`mail_conventions`, 788 domaines) | Une entreprise quelconque (`plombier-92.fr`) |
| Une institution publique (`.gouv.fr`, `mairie-*`, `senat.fr`…) | Une adresse générique (`contact@`, `redaction@`) ou un robot |

Le seuil est d'**une** fiche sur le domaine, pas deux comme pour
`maildomains.collect()` : il ne s'agit pas de décider à qui appartient un
domaine, seulement de savoir s'il nous concerne.

**Contrepartie assumée** : un journaliste qui écrit depuis son gmail personnel
n'est pas mis en file, parce que rien ne l'y distingue du médecin d'un membre.
Sa fiche se crée à la main, et l'adresse est apprise ensuite.

L'import compte ce qu'il écarte (`outside the queue's scope`) sans le stocker.

## 5 ter. Déposer un courriel à la main

`/echanges/deposer` : on glisse un ou plusieurs `.eml`, ils sont traités par le
même classifieur que l'import automatique
(`import_member_mails.handle_one_message`). Deux différences, voulues :

- **publication directe**, sans modération : qui dépose son propre échange sait
  ce qu'il dépose, et la modération n'ajouterait qu'une friction ;
- **le périmètre restrictif ne s'applique pas**. Un dépôt volontaire vaut
  consentement, là où la capture automatique reçoit sans avoir demandé. C'est
  ainsi qu'un journaliste écrivant depuis son gmail entre dans le CRM.

Un `Message-ID` déjà connu est ignoré : on peut redéposer un fil entier sans
créer de doublon. Un fichier illisible n'emporte pas les autres du même dépôt.

**Dépendance à connaître** : le Dockerfile ne copie pas `utils/`, injecté à
l'exécution par `docker cp`. `app.py` l'importe donc tardivement et affiche un
message explicite s'il manque, au lieu de refuser de démarrer.

## 6. Fonctionnement en continu (automatique)

Un **timer systemd** se déclenche **toutes les 10 minutes** et, dans le
conteneur, lit la boîte d'audit (uniquement les UID IMAP plus récents que le
dernier traité), classe, matche, et **publie** (mode auto-publish). Les cas de
faible confiance (motif de nom) partent en **modération**.

## 7. Mise à jour automatique des élu·es (les 4 chambres)

Les listes d'élu·es étaient remplies par des scripts **one-off** lancés à la main,
jamais rejoués — d'où, par exemple, les eurodéputé·es **absent·es de la prod**
(déployer le code ≠ appliquer les seeds ; `init_db` ne re-remplit pas une base
existante). C'est désormais **automatique** pour toutes les chambres.

> Rappel : les **emails** des fiches existantes sont, eux, rafraîchis chaque jour
> par `sync_emails_from_elus.py` (timer 06:00). Les jobs ci-dessous maintiennent
> la **liste** (arrivées / remplacements).

**Un seul job** — timer `sync-officials` (**lundi 05:30 UTC**), orchestrateur
`sync_officials.py`, qui enchaîne les **4 chambres** :

1. **AN** : télécharge le dump open-data officiel (zip), puis `extract_deputes` +
   `insert_deputes`.
2. **Sénat** : télécharge le JSON de l'API du Sénat, puis `insert_senateurices`.
3. **Gouvernement** : `extract_gouvernement` (API de l'annuaire de
   l'administration) + `insert_gouvernement`.
4. **Eurodéputé·es** : `extract_eurodeputes` + `insert_eurodeputes` — liste des
   MEP *siégeant aujourd'hui* depuis l'API du Parlement européen, emails
   **publiés** (jamais devinés) ; **anti-throttling** (HTTP 429) par **cache** :
   l'endpoint de détail n'est appelé que pour un·e **nouveau·lle** eurodéputé·e.

Téléchargements en Python pur (pas de `curl`/`unzip`), **chaque chambre isolée**
(un échec réseau/format sur l'une n'empêche pas les autres), dump AN supprimé
après usage (disque). Le numéro de législature de l'URL AN est configurable
(`AN_LEGISLATURE`, défaut `17`) — à incrémenter après une législative.

**Statut « en poste » (`persons.in_office`).** Après un run **complet** (les 4
chambres OK), une **réconciliation** met `in_office = 1` pour toute personne
présente dans au moins une liste courante, `0` sinon. Elle gère le cas
multi-rôles du gouvernement (un·e ex-ministre resté·e député·e reste « en
poste ») et **ne touche jamais** les fiches saisies à la main (`added_by` /
`validated_by`). Si une chambre échoue, la réconciliation est **sautée** (une
union incomplète retirerait des gens à tort). L'UI affiche un badge **« Non
élu·e actuellement »** sur la liste des personnes et la fiche.

Pour les 4 chambres :

- ✅ **Automatique** : arrivée / remplacement d'un·e élu·e (avec email, créé·e
  automatiquement), changement de groupe politique, passage « en poste » ↔
  « non élu·e actuellement ».
- ✋ **Manuel volontaire** : suppression d'un·e élu·e ayant quitté son mandat
  (conservé·e + badgé·e pour l'historique) ; réécriture d'un email existant (jamais écrasé
  automatiquement).

## 8. Composants

| Fichier | Rôle |
|---------|------|
| **`utils/import_member_mails.py`** | Import des mails membres ↔ élu·es : sens, membre, matching robuste des élu·es, alias appris, publication/modération. |
| **`utils/sync_officials.py`** | Orchestrateur des **4 chambres** (AN, Sénat, gouvernement, eurodéputés) : fetch + extract + insert, chambres isolées, + réconciliation `in_office`. |
| **`utils/extract_eurodeputes.py` / `insert_eurodeputes.py`** | Récupère (cache anti-429) et upsert les eurodéputé·es ; appelés par `sync_officials.py`. |
| **`app.py` + templates** | Sous-onglet **Membres** dans **Suivi des échanges**, vue **boîte mail** des conversations, badge **« Non élu·e actuellement »** (`persons.in_office`) ; tables `members`, `mail_members`, `mail_bodies`, `mail_thread`. |
| **`utils/deploy/import-member-mails.{service,timer}`** | Timer toutes les 10 min (mails de membres). |
| **`utils/deploy/sync-officials.{service,timer}`** | Timer hebdo lundi 05:30 (les 4 chambres + réconciliation `in_office`). |

## 9. Déploiement / exploitation

**Secrets** (`/opt/volunteer-apps/secrets/website-meeting.env`, jamais en dur) :
`MEMBER_IMAP_USER`, `MEMBER_IMAP_APP_PASSWORD` (boîte d'audit). Partage
`IMAP_DB_PATH` et `IMPORT_AUTO_PUBLISH` avec l'import de campagne.

**Import des mails de membres** :
```bash
# aperçu (n'écrit rien)
docker exec -i --env-file …/website-meeting.env -e IMAP_DB_PATH=/app/meetings.db \
  website-meeting-app python3 /app/utils/import_member_mails.py --backfill --dry-run --verbose
# activation du timer quotidien
sudo cp utils/deploy/import-member-mails.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now import-member-mails.timer
```

**Sync des élu·es (les 4 chambres, un seul timer)** :
```bash
sudo cp utils/deploy/sync-officials.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sync-officials.timer
```
> `sync-officials` couvre désormais les eurodéputés : l'ancien timer
> `sync-eurodeputes` est **obsolète** (`sudo systemctl disable --now
> sync-eurodeputes.timer`).

> ⚠️ Le conteneur ne contient ni `utils/` ni `actual_dataset/` (le Dockerfile ne
> copie que `app.py`/templates/static). Les services les recopient (`docker cp`)
> avant chaque exécution — d'abord `rm -rf` la cible pour éviter l'imbrication.
> Les changements d'`app.py` / templates nécessitent un `docker-compose build`.

**Logs** :
`journalctl -u import-member-mails.service -n 50` ·
`journalctl -u sync-eurodeputes.service -n 50`

## 10. RGPD

Différence assumée avec les mails citoyens : les échanges de membres sont
**internes à l'association**, donc l'identité du membre et le **corps** du mail
sont conservés (utiles au suivi). Les mails citoyens restent, eux, minimisés
(élu·e + date + objet uniquement).

## 11. Limites connues

- Un·e membre écrivant à une adresse d'élu·e **non officielle jamais vue** n'est
  matché·e qu'au premier échange qui la relie à un fil (puis apprise comme alias).
- Le motif de nom exige une correspondance **unique** ; les homonymes partent en
  modération plutôt que d'être devinés.
- Suppression d'élu·e (national ou européen) ayant quitté son mandat : volontaire,
  pour préserver l'historique.

---

*Code : branche `claude/automate-member-mails-crm`, dossier `utils/`.
Référence complète des options : `utils/README.md`.*


---

## Les domaines connus sont lus dans la base (septembre 2026)

Le script portait trois domaines en dur :

```python
OFFICIAL_DOMAINS = ("senat.fr", "assemblee-nationale.fr", "europarl.europa.eu")
```

Depuis la fusion du CRM presse, les journalistes sont des `persons` comme les
autres, mais répartis sur des centaines de domaines de médias que personne ne
maintiendra à la main. La liste est donc **calculée à partir des adresses déjà
présentes en base** (`utils/maildomains.py`) et s'élargit toute seule à mesure
que les fiches arrivent.

**La règle : un domaine partagé par au moins deux personnes connues appartient à
une organisation.** Une seule adresse sur un domaine ne prouve rien.

**L'exception qui compte : les domaines grand public ne sont jamais des
organisations.** `gmail.com`, `orange.fr`, `free.fr`, `laposte.net`… Plusieurs
journalistes utilisent une adresse perso, ce qui ne dit rien sur le propriétaire
d'une *nouvelle* adresse gmail. Sur ces domaines, seule l'adresse complète
identifie quelqu'un. La liste est dans `FREEMAIL_DOMAINS`.

Les trois domaines parlementaires restent un **plancher** : ils sont connus même
face à une base vide, donc une installation neuve se comporte exactement comme
avant.

### Ce que ça débloque

Le rapprochement par **scan du corps** fonctionne maintenant pour la presse. Une
réponse envoyée depuis une autre adresse de la rédaction (`desk@lefigaro.fr`),
qui cite le message d'origine, est rattachée au bon journaliste — ce qui était
impossible tant que seuls les trois domaines parlementaires comptaient.

### La règle Google Workspace

C'est la même liste qui doit alimenter la boîte d'audit :

```bash
docker exec website-meeting-app python3 /app/utils/maildomains.py --google-rule
docker exec website-meeting-app python3 /app/utils/maildomains.py --google-rule --exclude-seed
```

**La règle réelle de PauseIA ne liste aucun domaine.** Vérifié dans la console
le 24/09/2026 : une seule expression, `Location: Full headers` /
`Matches regex: @pauseia\.fr`, en Inbound et Outbound, livrée à
`suivi-membres@pauseia.fr`. Elle copie donc **toute** la correspondance externe
de l'association, presse comprise, et ce depuis toujours.

Conséquences, dans les deux sens :

- **Rien à faire dans Workspace** pour suivre la presse. Ce document a
  longtemps dit le contraire, et `--google-rule` existait pour ça ; l'option
  reste utile seulement pour une installation dont la règle énumère des
  domaines.
- **La capture est large**, et l'équipe devrait le savoir : le mail personnel
  d'un membre vers un ami journaliste est copié comme le reste, et le corps des
  mails membres est stocké (`mail_bodies`). Si ce périmètre pose question, c'est
  la règle Workspace qu'il faut restreindre — par exemple à `presse@pauseia.fr`
  — pas le code d'ici.

---

## `init_db()` est désormais sérialisé entre workers (septembre 2026)

Le déploiement de `44b4311` a planté au démarrage en boucle : `init_db()` tourne
à l'import du module, donc **les quatre workers gunicorn l'exécutent en même
temps**. Le `RENAME COLUMN details` d'un worker est passé pendant qu'un autre
lançait `UPDATE … details`, et le second a vu une colonne qui n'existait plus.

Un **verrou de fichier** (`flock` sur `meetings.db.migrate.lock`) sérialise
maintenant les migrations. Un verrou de fichier et non une transaction SQLite,
parce que `executescript()` valide toute transaction en cours avant de
s'exécuter : un `BEGIN IMMEDIATE` autour de ce bloc serait simplement ignoré.
`flock` est en outre tenu par le noyau, donc un worker qui meurt en pleine
migration le relâche au lieu de bloquer le démarrage suivant.

Les workers qui attendent derrière le verrou rejouent ensuite les migrations —
toutes sont gardées (`IF NOT EXISTS`, test de `PRAGMA table_info`), donc ce sont
des opérations vides à ce moment-là. **Ce qui compte est de sérialiser, pas de
sauter.** Si le verrou ne peut pas être pris (répertoire en lecture seule), le
démarrage continue : un processus unique est le cas normal en développement.

Les tests (`tests/test_init_db_concurrency.py`) démarrent de **vrais processus**
en parallèle sur une base neuve, comme gunicorn, et vérifient qu'un second
processus attend bien que le premier ait fini.

`CRM_DB_PATH` permet au passage de pointer la base ailleurs, comme les scripts
`utils/` le font déjà avec `IMAP_DB_PATH`.


---

## Concurrence avec l'application (septembre 2026)

Un `--backfill` sur une boîte de 2 079 messages est mort sur
`sqlite3.OperationalError: database is locked`. Deux causes, corrigées
ensemble :

**Personne n'attendait.** Ni les connexions de l'app (`get_db`), ni les scripts
`utils/` ne posaient de `busy_timeout` : à la première contention, SQLite
échouait immédiatement au lieu de patienter. Désormais 15 s côté app, 30 s côté
scripts.

**L'import gardait une seule transaction du début à la fin.** Sur des milliers
de messages, ça verrouille la base en écriture pendant plusieurs minutes :
l'application ne peut plus rien écrire, et la moindre écriture de sa part tuait
le script. L'import valide maintenant **tous les 50 messages**
(`COMMIT_EVERY`).

Conséquence à connaître : **un run interrompu n'est plus annulé en totalité.**
Avant, un Ctrl-C ramenait la base exactement à son état initial. Maintenant la
première moitié reste importée — ce qui est voulu : `last_uid` avance avec
chaque validation, donc une relance reprend où on s'était arrêté au lieu de tout
refaire, et `imported_mails` empêche tout doublon.

Ces deux correctifs n'ont **pas suffi** : l'import échouait encore, au bout de
30 s d'attente, au moment de valider. La base est donc passée en **journal WAL**
(`PRAGMA journal_mode=WAL`), où lecteurs et écrivain ne se bloquent plus.

**Et ça change la façon de sauvegarder.** En WAL, les écritures récentes vivent
dans `meetings.db-wal` jusqu'au prochain checkpoint : `cp meetings.db` perd
donc les dernières, en produisant un fichier d'apparence valide. Utiliser
`utils/backup_db.py` (`VACUUM INTO`), qui écrit un fichier unique et cohérent
sans arrêter l'application.
