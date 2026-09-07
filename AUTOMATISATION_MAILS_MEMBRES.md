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
        │  import_member_mails.py (timer 06:10)
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

Les fiches **Membres** (nouvel onglet) et les fiches **Personnes** (élu·es)
affichent leurs échanges de la même façon.

## 6. Fonctionnement quotidien (automatique)

Un **timer systemd** se déclenche **chaque jour à 06:10 UTC** et, dans le
conteneur, lit la boîte d'audit (uniquement les UID IMAP plus récents que le
dernier traité), classe, matche, et **publie** (mode auto-publish). Les cas de
faible confiance (motif de nom) partent en **modération**.

## 7. Eurodéputé·es — mise à jour automatique (hebdomadaire)

Les eurodéputé·es étaient **absent·es de la base de prod** : le script d'insertion
one-off n'y avait jamais été lancé (déployer le code ≠ appliquer les seeds de
données ; `init_db` ne re-remplit pas une base existante). Corrigé, **et**
automatisé.

Un **timer systemd** (`sync-eurodeputes`, **lundi 05:50 UTC**) enchaîne :

1. **`extract_eurodeputes.py`** — liste des eurodéputé·es *siégeant aujourd'hui*
   depuis l'API officielle du Parlement européen (`data.europarl.europa.eu`),
   emails **publiés** par le Parlement (jamais devinés).
2. **`insert_eurodeputes.py`** — upsert dans `persons` (rôle *Député·e
   européen·ne*, email inclus), idempotent.

**Anti-throttling** : l'endpoint de détail limite le débit (HTTP 429). Comme les
emails ne changent pas, `extract` réutilise le dataset précédent en **cache** et
n'appelle le détail que pour un·e **nouveau·lle** eurodéputé·e. Semaine normale =
1 appel, zéro détail. Une garde interdit d'écrire un dataset dégradé si l'API
throttle.

- ✅ **Automatique** : arrivée/remplacement d'un·e eurodéputé·e (avec email),
  changement de groupe politique.
- ✋ **Manuel volontaire** : suppression d'un·e eurodéputé·e ayant quitté le
  Parlement (conservé·e pour l'historique) ; réécriture d'un email officiel
  existant (jamais écrasé automatiquement).

## 8. Composants

| Fichier | Rôle |
|---------|------|
| **`utils/import_member_mails.py`** | Import des mails membres ↔ élu·es : sens, membre, matching robuste des élu·es, alias appris, publication/modération. |
| **`utils/extract_eurodeputes.py`** | Récupère les eurodéputé·es depuis l'API du Parlement (cache, anti-429). |
| **`utils/insert_eurodeputes.py`** | Upsert idempotent des eurodéputé·es dans `persons`. |
| **`app.py` + templates** | Onglets **Membres** et **Suivi des échanges**, regroupement des fils, affichage des corps ; tables `members`, `mail_members`, `mail_bodies`, `mail_thread`. |
| **`utils/deploy/import-member-mails.{service,timer}`** | Timer quotidien 06:10 (mails de membres). |
| **`utils/deploy/sync-eurodeputes.{service,timer}`** | Timer hebdo lundi 05:50 (eurodéputé·es). |

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

**Sync eurodéputé·es** :
```bash
sudo cp utils/deploy/sync-eurodeputes.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sync-eurodeputes.timer
```

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
