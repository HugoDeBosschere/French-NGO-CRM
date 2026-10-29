# CLAUDE.md — carte du projet pour un agent IA

CRM interne de **PauseIA** (« Journal des rencontres et des médias ») : une app
**Flask + SQLite** (un seul fichier `app.py`, templates Jinja, pas de build front),
déployée en **Docker** derrière Caddy sur un serveur Hetzner. Ce fichier est le
point d'entrée pour une IA qui reprend le projet : il dit **où sont les choses**,
**ce qui est déjà automatisé**, et **ce qui reste à faire**.

> Docs de détail : `README.md` (app), `BACKWARD_COMPATIBILITY.md` (schéma & fusion
> des CRM), `utils/README.md` (tous les scripts), `AUTOMATISATION_MAILS_MEMBRES.md`
> et `AUTOMATISATION_MAILS_CAMPAGNE.md` (les deux pipelines mails),
> `AUTOMATISATION_CIVICRM.md` (le pont vers le second CRM de l'association, d'où
> viennent les adresses de journalistes). Lis-les avant de modifier la zone
> correspondante.

> **Deux CRM tournent sur le serveur** et c'est structurant : celui-ci, le journal
> des interactions, et **CiviCRM**, qui sert aux envois de masse et porte ~12 900
> journalistes. Le second alimente le premier en lecture seule, à la demande.
> Ne jamais écrire dans CiviCRM depuis ici.

## 1. Modèle de données (SQLite, `meetings.db`)

Tout tourne autour de **`persons`** (contacts) et **`organisations`**, reliés par
`person_organisations`. Migrations idempotentes dans `init_db()` (`app.py`) —
jamais de fichier de migration séparé, on ajoute des `ALTER TABLE … ADD COLUMN`
gardés par un test de présence de colonne.

- **`persons`** : un contact. Colonne clé **`contact_type`** ∈ `CONTACT_TYPES`
  (`Journaliste`, `Politique`, `Religieux·se`, `Membre d'une ONG`,
  `Membre d'une entreprise`, `Autre`). Champs notables : `role` (fonctions,
  cumulables), `political_group` (nullable depuis la fusion), `stance` (position
  sur PauseIA), `email`, `in_office` (0 = mandat terminé, fiche conservée), etc.
- **`organisations`** : `ORG_TYPES` = `Média`, `Groupe politique`, `Culte`, `ONG`,
  `Entreprise`, `Autre`. Correspondance type de contact ↔ type d'orga dans
  `ORG_TYPE_BY_CONTACT_TYPE` (`app.py`).
- **`import_runs`** : une ligne par exécution d'un importeur (script, début, fin,
  statut, nombre de courriels). Écrite par `utils/importruns.py`, lue par
  `import_status()` pour le bandeau de fraîcheur des pages d'échanges. C'est ce
  qui rend visible un import en panne : sans elle, un mot de passe IMAP expiré
  laissait l'interface parfaitement normale pendant que plus rien n'arrivait.
  Métadonnées d'exploitation uniquement — aucune adresse, aucun nom.
- **`meetings`** / `mails` (+ `mail_persons`, `mail_members`, `mail_bodies`,
  `mail_thread`), **`members`** (membres @pauseia.fr, auto-créés par l'import),
  **`interventions`** / `contents` (médiatiques), **`pending_*`** (file de
  modération), `moderators` (les « Utilisateurices »).
- **Pont CiviCRM** : `mail_conventions` (conventions d'adresses apprises sur CiviCRM),
  `civicrm_pending` (file des adresses à identifier :
  `pending` / `resolved` / `absent`) et `person_emails` (autres adresses d'une
  personne, apprises par fil de discussion ou par CiviCRM — c'est la table que
  le rapprochement des mails consulte).
- **`veille_passages`** : passages sur l'IA de chaque élu·e trouvés par la veille
  des élu·es (veille.pauseia.fr), réécrits en entier chaque matin par
  `utils/import_veille.py` et affichés sur la fiche. Jamais saisis à la main.
- **Provenance** : `added_by` / `validated_by` NULL = importé par script ;
  renseigné = saisi/validé à la main (les backfills ne touchent jamais ces
  dernières).

## 2. Interface

Nav (voir `templates/base.html`) : TODO · Fait · Répartition · Calendrier ·
**Suivi des échanges** · Rencontres · Personnes · Organisations ·
Interventions · Contenus · Modération · Utilisateurices.

- **Suivi des échanges** (`/echanges`) : vue unifiée type boîte mail (citoyens +
  membres + élu·es). Quatre sous-onglets : *Échanges · Membres ·
  **À rattacher** · Déposer*. L'entrée « Courriels » du menu principal a
  disparu, et le sous-onglet « Tous les courriels » avec elle : c'étaient
  quatre portes vers deux **lectures** des mêmes courriels. Un interrupteur
  sous les onglets passe de *Par conversation* (fils regroupés) à *Un par un*
  (/mails, à plat, paginé) — chaque vue garde ses filtres, sa recherche et ses
  actions. Détail d'un fil = `conversation.html` (cartes
  expéditeur→destinataires), corps mis en forme par le filtre `corps_mail`.
  Les quatre pages portent un **bandeau de fraîcheur** (macro `import_banner`) :
  « Dernière synchronisation il y a 4 minutes » en vert, l'avertissement en
  orange au-delà de 30 min (trois créneaux manqués), en rouge si le dernier
  import a échoué. Répond à « mon échange est-il enregistré, ou faut-il
  attendre ? » sans demander de faire confiance.
- **Périmètre de la file (important, RGPD)** : `maildomains.in_scope()`. La file
  fonctionnait par **exclusion** (tout ce qui n'est ni un membre, ni un robot,
  ni une adresse générique). Or la règle Workspace copie **toute** la
  correspondance externe de l'association, mails personnels des membres
  compris : médecin, banque, famille finissaient dans une page consultable par
  quiconque a le mot de passe. Il faut désormais une **raison positive**
  d'entrer : un domaine déjà porté par **une** fiche (seuil à 1, pas 2 : il ne
  s'agit pas de décider à qui appartient le domaine, seulement de savoir qu'il
  nous concerne), un domaine de média attesté par CiviCRM (`mail_conventions`),
  ou une **institution publique** (`.gouv.fr`, `mairie-*`, `senat.fr`… +
  `CRM_PUBLIC_DOMAINS`). Jamais de messagerie grand public. Contrepartie
  assumée : un journaliste écrivant depuis son gmail n'entre pas en file, rien
  ne le distinguant du médecin d'un membre. `civicrm_lookup.py --prune` applique
  la règle aux adresses déjà en file, `resolved` exclues.
- **`/echanges/a-rattacher`** : `civicrm_pending` sorti de la ligne de commande.
  Les adresses que l'import n'a pas su rattacher, avec le nombre de courriels
  concernés ; « Rattacher à » écrit un alias dans `person_emails` (jamais dans
  `persons.email` : on écrit depuis plusieurs adresses, celle vue ici n'est pas
  forcément la principale) et le prochain import rattache les courriels.
  « Ignorer » pose le statut **`ignored`**, qu'aucun script ne réexamine —
  `--retry-absent` ne touche qu'`absent` et `enqueue()` ne réécrit jamais le
  statut, donc une décision humaine n'est jamais défaite par la synchro de nuit.
  Compteur dans l'onglet. **C'est là qu'il faut chercher un échange qui
  n'apparaît pas.**
- **`/echanges/deposer`** : dépôt manuel d'un `.eml` (glisser-déposer, ou
  sélecteur de fichiers sans JavaScript). Passe par
  `import_member_mails.handle_one_message()`, donc par **le même classifieur**
  que la capture automatique — une seconde implémentation divergerait au premier
  correctif. Deux différences voulues : publication directe (sans modération, la
  personne qui dépose son échange sait ce qu'elle dépose) et **périmètre non
  appliqué** (`enforce_scope=False`) — un dépôt volontaire est précisément le
  consentement qui manque à la capture, donc le journaliste sur gmail y entre.
  `app.py` importe `utils/` **tardivement** et tolère son absence : le
  Dockerfile ne copie pas ce répertoire, un import au chargement ferait planter
  toute l'application sur une image fraîche.
- **Filtres** : l'origine (membre / campagne citoyenne) **et** le type
  d'interlocuteur (`contact_type`) sont deux questions distinctes — avant la
  fusion presse, un échange avec un journaliste s'affichait « Membre » sans plus.
  Un sélecteur « Je suis… » mémorise un `members.id` en cookie pour ne montrer
  que ses propres échanges : c'est un confort, **pas une permission** (un seul
  mot de passe partagé, tout reste visible de tout le monde), et l'interface le
  dit explicitement.
- Les listes filtrent par `contact_type` ; badge « Non élu·e actuellement » quand
  `in_office = 0`.

## 3. Ce qui est DÉJÀ automatisé

### a) Peuplement des contacts (scripts `utils/`, idempotents, `--commit`/`--dry-run`)
- **Élu·es** : `extract_deputes` + `insert_deputes`, `insert_senateurices`,
  `extract_gouvernement` + `insert_gouvernement`, `extract_eurodeputes` +
  `insert_eurodeputes`. Orchestrés par **`sync_officials.py`** (les 4 chambres) +
  réconciliation `in_office`.
- **Emails des élu·es** : `sync_emails_from_elus.py` (depuis `elus.json`).
- **Médias & journalistes** : `insert_medias_journalistes.py` (données embarquées).
- **Religieux·ses** : `insert_cultes.py` + `extract_eveques`/`insert_eveques`
  (+ `extract_eveques_orthodoxes`).
- **Journalistes & médias** : voir le pont CiviCRM ci-dessous — c'est **là** que
  les adresses de presse arrivent, pas dans `insert_medias_journalistes.py`, qui
  ne porte que des noms.

### b) Intégration des mails (récurrent, timers systemd)
- **Citoyens → élu·es** : `import_campaign_mails.py` (BCC campagne). Voir
  `AUTOMATISATION_MAILS_CAMPAGNE.md`. L'en-tête `From` n'est ni lu ni stocké —
  et c'est ce qui explique la seule asymétrie restante avec le pipeline des
  membres : `queue_unknown_recipients()` ne met en file que les **destinataires**
  inconnus, jamais l'expéditeur, qui est le citoyen. Pour le reste les deux
  pipelines sont alignés : même index d'adresses (`person_emails` compris), même
  file `civicrm_pending`, même périmètre `maildomains.in_scope`.
  Depuis octobre 2026 le **corps est conservé** (`mail_bodies`, jamais
  `summary`, qui garde l'étiquette « Mail d'un citoyen à … » dont dépend la
  colonne « Origine »). Le commentaire d'origine invoquait la minimisation
  RGPD ; en pratique elle était illusoire, puisque c'est le citoyen lui-même
  qui met PauseIA en copie cachée. Conséquence à connaître : le corps porte sa
  signature. Le **sens** (Envoyé / Reçu) se déduit des destinataires visibles,
  puis des préfixes d'objet — déduction corrigeable à la main.
  Rattrapage des anciens courriels : `backfill_campagne_corps.py`.
- **Membres ↔ élu·es** : `import_member_mails.py` (règle Gmail invisible →
  boîte d'audit IMAP). Matching robuste (adresse/alias/corps/fil/motif de nom),
  membres auto-créés. Voir `AUTOMATISATION_MAILS_MEMBRES.md`.

### c) Pont CiviCRM — lecture seule (`AUTOMATISATION_CIVICRM.md`)

CiviCRM 6.15 Standalone tourne **sur le même serveur** et porte ~12 900
journalistes avec leur adresse et leur média. On ne les recopie pas : les
formulaires de rencontre et de courriel rendent un `<option>` **et** une case à
cocher par personne, donc 17 000 `persons` rendraient les écrans de saisie
inutilisables — et un journal d'interactions n'a que faire de 12 900 personnes à
qui personne n'a jamais écrit.

- **À la demande** : `import_member_mails.py` met en file (`civicrm_pending`)
  l'adresse qu'il ne sait pas rattacher ; l'hôte interroge CiviCRM ;
  `civicrm_lookup.py --apply` crée la fiche avec son média et son alignement ;
  un `--backfill` rattache enfin le mail.
- **En masse, l'exception** : les 168 médias (`import_civicrm_medias.py`) —
  organisations, donc aucun impact sur les sélecteurs.
- **Amorçage** : `deploy/civicrm-seed.sh` crée les fiches d'un groupe presse
  restreint (12 « Presse - Nationale »), plafonné à 1 500 par `SEED_SOFT_CAP`.
  Nécessaire une fois, pour casser l'œuf et la poule décrit plus bas.
- **Conventions d'adresses** : `mailpatterns.py` apprend sur les adresses réelles
  comment chaque rédaction construit les siennes (Le Figaro = `<initiale><nom>`),
  pour **reconnaître** une adresse jamais vue. Jamais pour en fabriquer une.
  `learn_conventions.py` (étape `1b/5`) fait cet apprentissage sur les **12 900
  journalistes de CiviCRM** et non sur nos seules fiches, et range le résultat
  dans `mail_conventions` (domaine, média, gabarit, nombre d'exemples — aucune
  adresse, aucun nom, aucune fiche créée). C'est ce qui rend reconnaissable une
  adresse chez une rédaction dont on ne tient qu'une fiche (`nouvelobs.com`,
  `francetv.fr`) : `maildomains` compte le domaine comme connu et
  `civicrm_lookup --apply-names` peut y rattacher un nom.

Trois règles non négociables : **APIv4 uniquement, jamais MySQL** (le schéma
bouge entre versions majeures, l'API non) ; **`cv` en local, pas REST** (aucune
clé à stocker, rien d'exposé) ; **un seul sens, lecture seule** — CiviCRM porte
les donateurs et les contributions Stripe/HelloAsso. Aucun script d'ici ne peut
y écrire. `utils/civicrm.py` concentre tout ce qui est CiviCRM-dépendant et
s'ouvre sur un **test de contrat** : un champ renommé arrête la synchro au lieu
d'écrire des données fausses.

### d) Domaines connus, lus dans la base (`maildomains.py`)

`OFFICIAL_DOMAINS` n'est plus en dur. La liste est dérivée des adresses en base :
un domaine partagé par **au moins deux personnes connues** appartient à une
organisation. Les messageries grand public (`gmail.com`, `orange.fr`…) en sont
exclues par construction — plusieurs journalistes y ont une adresse perso, ce qui
ne dit rien du propriétaire d'une *nouvelle* adresse gmail. Les trois domaines
parlementaires restent un plancher.

### e) Planification (`utils/deploy/*.timer`, UTC)
- `sync-officials` (lun. 05:30, les 4 chambres + `in_office`) : remplace l'ancien
  `sync-eurodeputes`, dont les unités ont été retirées du dépôt (les laisser
  désactivées revenait à proposer l'installation de deux synchros concurrentes
  sur les mêmes fiches). Trace son exécution dans `import_runs`.
- `backup-db` (03:17, avant toutes les synchros) : `backup_db.py` dans le
  conteneur, **puis copie sur l'hôte** et, si `RSYNC_DEST` est renseigné, hors
  de la machine. Une sauvegarde restée dans le conteneur ne protège de rien.
  Rotation : 2 copies dans le conteneur, 30 sur l'hôte.
- `import-campaign-mails` (06:00, sync emails + import citoyens).
- `import-member-mails` (**toutes les 10 min**, `OnUnitActiveSec=10min`) — la
  capture Workspace est instantanée, seul cet import faisait attendre ; sans
  `--backfill` il ne lit que les UID nouveaux, donc un passage à vide ne coûte
  rien. Le service ne recopie `utils/` que si elle manque (sinon il effacerait
  ce répertoire sous les pieds de `civicrm-sync`).
- `civicrm-sync` (06:30) — **après** l'import des mails de membres, qui est
  précisément ce qui remplit la file que cette synchro vide.
- `sync-elus` (05:55, adresses + portraits + fiches officielles depuis
  `elus.json`). Était un `ExecStartPre` de l'import citoyen, précédé d'un
  « - » : son échec était avalé, donc la synchro pouvait s'arrêter des mois
  sans que personne l'apprenne. Unité séparée, avec `OnFailure`.
- `fusion-doublons` (07:10, **après** `civicrm-sync`) — le seul travail
  planifié qui SUPPRIME des lignes, d'où la sauvegarde en `ExecStartPre` et la
  limite de 50 fiches par passage (`--max`), au-delà de laquelle il s'arrête
  et alerte plutôt que de nettoyer en masse sans témoin. Les noms seulement
  ressemblants ne sont jamais fusionnés automatiquement : `--approchants` les
  signale, un humain tranche.
- Chaque service renvoie à `alerte@%n.service` par `OnFailure=` : une panne que
  personne ne voit n'est pas une panne, c'est une donnée fausse qui s'installe.

## 4. Ce qui n'est PAS (encore) automatisé — pistes

Le peuplement/intégration ci-dessus est **partiel**. Chantiers ouverts :

- **Organisations / groupes politiques** : les `persons` élu·es sont
  auto-synchronisées, mais les **organisations** (groupes politiques, médias,
  cultes) et les liens `person_organisations` ne sont **pas** rafraîchis par un
  timer — peuplés une fois par les `insert_*`. → automatiser leur mise à jour.
- **Types de contact sans source auto** : **ONG**, **Entreprises**, et la plupart
  des **cultes** hors diocèses n'ont pas d'extract/sync (pas de dataset public
  exploitable, cf. commentaires de `insert_cultes.py`). → identifier des sources.
- **Élus locaux** (maires, conseillers régionaux/départementaux, EPCI) : ~500 000,
  **absents**. Le RNE (data.gouv) existe mais **sans emails** → matching impossible
  par adresse. Piste retenue (non implémentée) : *capture assistée par modération*
  — les mails de membres vers une adresse externe inconnue vont dans une file
  « élu local à confirmer », un humain valide, l'adresse est apprise. La capture
  Gmail attrape **déjà** ces mails (règle sur `@pauseia.fr`), ils sont juste
  ignorés faute de fiche. → **la moitié existe maintenant** : `civicrm_pending`
  est exactement cette file, alimentée par `queue_unknown_counterparts`. Il reste
  à lui donner une source pour les élus locaux, là où la presse a CiviCRM.
- **Interventions / Contenus** : entités présentes, mais aucune ingestion
  automatique (ex. veille médias). → à concevoir. Le difficile n'est pas de
  trouver les articles (un flux RSS par média suffit) mais de rattacher un
  article à **la bonne fiche journaliste** — les signatures sont incohérentes,
  souvent absentes — et de qualifier la position vis-à-vis de l'IA, qui est un
  jugement, pas une extraction.
- **La règle Google Workspace ne filtre rien** — et il faut le savoir dans les
  deux sens. Son expression est une seule regex sur les en-têtes complets,
  `@pauseia\.fr`, en entrant et en sortant : elle copie donc **toute** la
  correspondance externe de l'association dans `suivi-membres@pauseia.fr`, y
  compris les mails personnels d'un membre. Rien n'est à changer côté Workspace
  pour suivre la presse — ça arrivait déjà. En revanche l'ampleur de la capture
  mérite d'être connue de l'équipe, et le corps des mails membres est stocké
  (`mail_bodies`).
- **`Expert IA` (104 contacts) et `Influenceur` (4)** existent comme sous-types
  CiviCRM mais pas dans `CONTACT_TYPES` : ils arrivent en `Autre`, sous-type
  d'origine conservé dans les notes. Les ajouter suppose de décider à quel
  `ORG_TYPE` les rattacher (CiviCRM a un `Recherche/Éducation` qui n'existe pas
  ici).
- **Les relances en double** : CiviCRM porte aussi `Suivi_Activit_s_Pause_IA`
  (Résultat, Suivi Requis, Date Prochain Contact), qui recouvre le `/todo` d'ici.
  À trancher : qui fait référence pour « quand relancer qui » ?

## 5. Conventions

- **Scripts `utils/`** : sans dépendance tierce (stdlib), **idempotents**,
  `--dry-run` par défaut ou explicite puis `--commit`, DB = `ROOT/meetings.db`
  (= `/app/meetings.db` en conteneur). Matching par **nom au sein d'un même type**
  (un homonyme dans un autre type reste une fiche séparée).
- **Ne jamais supprimer** un contact qui quitte ses fonctions : `in_office = 0`.
- **Secrets** uniquement en variables d'env (`/opt/volunteer-apps/secrets/…`),
  jamais dans le dépôt. IMAP : `IMAP_*` (citoyens), `MEMBER_IMAP_*` (membres).
  Le pont CiviCRM n'a **aucun** secret : `cv` tourne en local dans le conteneur.
- **Chemin de base surchargeable** : `CRM_DB_PATH` pour l'app, `IMAP_DB_PATH`
  pour les scripts. C'est ce qui rend les tests de démarrage concurrents possibles.
- **`init_db()` est sérialisé** par un verrou de fichier
  (`meetings.db.migrate.lock`), parce qu'il tourne à l'import donc dans les
  quatre workers gunicorn à la fois — c'est ce qui avait fait planter le
  déploiement de `44b4311` en boucle. Un verrou de fichier et **non** une
  transaction SQLite : `executescript()` valide toute transaction en cours avant
  de s'exécuter, donc un `BEGIN IMMEDIATE` serait simplement ignoré.
- **Tests** : `python3 -m unittest discover -s tests` (couvre le classifieur mails).

## 6. Déploiement (Docker)

La base **vit dans le conteneur** (`/app/meetings.db`) — ne pas la manipuler
depuis l'hôte.

**Elle est en journal WAL** depuis septembre 2026, pour que l'app et les
importeurs de mails cessent de se bloquer mutuellement. Conséquence à ne pas
rater : `cp meetings.db` **ne suffit plus** comme sauvegarde. Les écritures
récentes vivent dans `meetings.db-wal` jusqu'au prochain checkpoint, et une
copie du seul fichier principal les perd — en ayant l'air parfaitement valide.
Utiliser `utils/backup_db.py`, qui fait un `VACUUM INTO` : un fichier unique et
cohérent, pris à chaud, sans arrêter l'application. Le `Dockerfile` ne copie que `app.py` + `templates/` + `static/` ;
`utils/` et `actual_dataset/` sont injectés à l'exécution par `docker cp`.

```bash
cd /opt/volunteer-apps/apps/website-meeting
sudo docker exec website-meeting-app python3 /app/utils/backup_db.py   # PAS un cp, voir ci-dessous
git fetch <remote> && git checkout -f <remote>/main    # la prod suit main
sudo docker-compose build && sudo docker-compose up -d  # init_db applique les migrations
sudo docker logs --tail 15 website-meeting-app          # vérifier: pas de traceback
# peuplement éventuel :
sudo docker exec website-meeting-app rm -rf /app/utils
sudo docker cp utils website-meeting-app:/app/utils
sudo docker cp actual_dataset website-meeting-app:/app/actual_dataset   # si le script lit un JSON
sudo docker exec website-meeting-app python3 /app/utils/<script>.py [--commit]
```

## 7. Où regarder en premier

| Besoin | Fichier |
|---|---|
| Routes, schéma, migrations, constantes (`CONTACT_TYPES`…) | `app.py` |
| Un script de peuplement / sync | `utils/` + `utils/README.md` |
| Comment le schéma a évolué (fusion CRM) | `BACKWARD_COMPATIBILITY.md` |
| Les pipelines mails | `AUTOMATISATION_MAILS_*.md` |
| Le pont CiviCRM (règles, correspondances, pièges) | `AUTOMATISATION_CIVICRM.md` |
| Ce que CiviCRM renvoie et comment on le mappe | `utils/civicrm.py` |
| La file d'attente et la création des fiches | `utils/civicrm_lookup.py` |
| Les domaines connus, les conventions d'adresses | `utils/maildomains.py`, `utils/mailpatterns.py`, `utils/learn_conventions.py` |
| Timers / unités systemd | `utils/deploy/` |
| Fusionner des fiches en double | `utils/fusionner_doublons.py` |
| Retrouver le corps d'anciens courriels citoyens | `utils/backfill_campagne_corps.py` |
| Portraits et fiches parlementaires | `utils/sync_emails_from_elus.py`, macro `personne_lien` |
| Mise en forme du corps d'un courriel | filtre `corps_mail` dans `app.py` |
