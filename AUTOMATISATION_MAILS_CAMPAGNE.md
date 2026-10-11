# Automatisation du suivi des mails citoyens → élu·es

Document de synthèse. Décrit ce qui a été mis en place pour alimenter
automatiquement le CRM (application *Website Meeting*, `meetings.pauseia.fr`) à
partir des mails que les citoyens envoient à leurs élu·es dans le cadre de la
campagne « Écrire à mes élu·es ».

> Mails des membres de l'association ↔ élu·es (Phase 2) et sync eurodéputé·es :
> voir **`AUTOMATISATION_MAILS_MEMBRES.md`**.
> Détail technique et options des scripts : voir **`utils/README.md`**.

---

## 1. Objectif

Savoir **quel·le élu·e a reçu un mail de citoyen, et quand**, sans aucune saisie
manuelle, et alimenter le CRM en continu.

## 2. Principe

Quand un·e citoyen·ne écrit à son élu·e via le site, il/elle met en copie cachée
(CCI) une boîte de suivi, membre du groupe `campagne@pauseia.fr`. La copie reçue
sur `suivi-campagne@pauseia.fr` **conserve l'en-tête `To:` d'origine** (l'adresse
de l'élu·e). Un script lit cette boîte, retrouve l'élu·e par son adresse e-mail,
et enregistre le courriel dans le CRM.

```
Citoyen ──mail + BCC──▶ campagne@pauseia.fr (groupe)
                              │
                              ▼
                     suivi-campagne@pauseia.fr (boîte lue en IMAP)
                              │  import_campaign_mails.py (timer 06:00)
                              ▼
                     CRM : table `mails` + lien `mail_persons` → fiche élu·e
```

## 3. Composants (tout dans `utils/`, aucune modification de `app.py`)

| Script | Rôle |
|--------|------|
| **`import_campaign_mails.py`** | Lit la boîte IMAP, matche les destinataires sur `persons.email`, enregistre les mails. Modes : IMAP quotidien, `--mbox`, `--eml-dir`, `--pasted` (reprise historique), modération ou `--auto-publish`. |
| **`sync_emails_from_elus.py`** | Remplit `persons.email` depuis la source officielle `elus.json` (repo `Pause-IA/pauseai-france`, données data.gouv Assemblée nationale + Sénat ODSEN). C'est **la même source** que les adresses réellement utilisées par l'outil d'envoi → matching fiable. |
| **`deploy/`** | `deploy.sh` (déploiement clé en main) + unité `systemd` (timer quotidien). Exécution **dans le conteneur** Docker, en tant que l'utilisateur qui possède la base. |

## 4. Fonctionnement quotidien (automatique)

Un **timer systemd** se déclenche **chaque jour à 06:00 UTC** et, dans le conteneur :

1. **Sync des emails élu·es** depuis `elus.json` (non bloquant) — garde les
   adresses à jour pour les fiches existantes.
2. **Import des nouveaux mails** de la boîte de suivi (uniquement les UID IMAP
   plus récents que le dernier traité).
3. **Publication** directe dans la table `mails` (mode auto-publish activé via
   `IMPORT_AUTO_PUBLISH=1` dans le fichier de secrets).

Rien à faire au quotidien.

## 5. Matching des élu·es

- **Prioritaire** : marqueur `X-Elu-Id` / `X-Depute-Id` (si un jour le site en
  injecte un) → correspondance certaine.
- **Standard** : adresses `To` / `Cc` / `X-Original-To` / `Delivered-To`
  comparées à `persons.email`.
- **Historique uniquement** (`--match-body`) : adresse officielle citée dans le
  corps d'un mail transféré.

Les sénateur·ices n'avaient **aucun email** en base : le sync en a rempli **~334**
depuis la source officielle, ce qui a débloqué leur matching.

## 6. Anti-doublon

- Dernier **UID IMAP** traité mémorisé par boîte (import incrémental).
- **Message-ID** de chaque mail enregistré → jamais deux fois le même.
- Pour les reprises historiques : dédoublonnage supplémentaire par
  (élu·e, date) contre les mails déjà présents.

## 7. Ce qui est conservé

**L'en-tête `From` n'est ni lu ni stocké** : le CRM ne tient pas de registre
des expéditeurs.

> **Révisé en octobre 2026.** Jusque-là, on ne gardait que l'élu·e, la date et
> l'objet, au nom de la minimisation. Deux raisons de revenir dessus. D'abord
> la minimisation était illusoire : c'est le citoyen lui-même qui met PauseIA
> en copie cachée de ce qu'il écrit — un acte délibéré de sa part, pas une
> captation. Ensuite c'était inexploitable : sans le corps, impossible de
> distinguer un premier envoi d'un fil où l'élu·e avait fini par répondre.
>
> **Le corps est donc conservé**, dans `mail_bodies` et non dans `summary` —
> `summary` garde l'étiquette « Mail d'un citoyen à … », dont dépend la colonne
> « Origine » du suivi des échanges.
>
> Ce que ça implique, et qu'il faut savoir plutôt que découvrir : le corps
> porte la signature du citoyen, donc son nom et parfois ses coordonnées, dans
> une page que lit toute personne ayant le mot de passe partagé.
>
> Les courriels importés avant ce changement n'ont pas de corps, et aucun
> import futur ne le leur donnera (la déduplication se fait sur le
> Message-ID). `utils/backfill_campagne_corps.py` relit la boîte pour les
> rattraper — voir `utils/README.md`.

### Le sens de l'échange

Le pipeline ne voit qu'un côté. Deux indices, du plus sûr au moins sûr :

1. **À qui c'est adressé.** L'élu·e dans `To`/`Cc` : le citoyen lui écrit et
   nous met en copie cachée — premier envoi. Notre adresse dans `To`/`Cc` sans
   celle de l'élu·e : le citoyen s'adresse à nous, il nous transmet quelque
   chose. Structurel, indépendant de la langue et du client de messagerie.
2. **L'objet**, à défaut : `Re:`, `TR:`, `Fwd:`. Moins fiable — un citoyen peut
   se répondre à lui-même, et certains clients ne préfixent rien.

Aucun des deux ne prouve que c'est l'élu·e qui a répondu : le sens reste
corrigeable à la main sur la fiche du courriel, et le corps conservé permet de
trancher.

### La file d'identification reste asymétrique

Que le corps soit conservé ne change rien à la règle suivante : `queue_unknown_
recipients()` ne lit **que** les en-têtes de destinataires. Le pipeline des
membres, lui, met en file les deux côtés — c'est légitime là-bas, où les deux
correspondants sont identifiés. Ici, la même symétrie serait une fuite : elle
enregistrerait le citoyen. D'où deux fonctions distinctes plutôt qu'une seule
partagée, et un test qui vérifie explicitement que l'expéditeur n'entre jamais
en file.

## 7 bis. Destinataires inconnus : la file d'identification

Une adresse de destinataire qu'aucune fiche ne reconnaît était **abandonnée en
silence**. Elle entre désormais dans `civicrm_pending`, la même file que
l'import des membres alimente, et apparaît dans l'interface sur
`/echanges/a-rattacher`.

Le périmètre habituel s'applique (`maildomains.in_scope`) : il faut un domaine
que l'association a une raison de suivre — une rédaction connue, une institution
publique, un domaine déjà porté par une fiche. C'est par là que les **élu·es
locaux** entrent : `maire@mairie-nantes.fr` est retenue sans qu'aucune fiche ne
la connaisse, là où `contact@plombier-92.fr` ne l'est pas.

L'index des adresses lit par ailleurs `person_emails` (les adresses secondaires
apprises par fil de discussion ou par CiviCRM). Un courriel adressé à l'adresse
de cabinet d'un·e élu·e, alors que sa fiche porte l'adresse parlementaire, est
désormais rattaché — ce que le pipeline des membres savait déjà faire.

## 8. Reprise de l'historique (déjà effectuée)

L'historique du groupe n'était pas exportable proprement (pas de licence Vault,
export admin limité aux boîtes utilisateurs, abonnements en « résumé »). Il a été
récupéré via :

- **`--eml-dir`** : quelques mails transférés exportés en `.eml`.
- **`--pasted`** : copier-coller de l'archive web du groupe, dont le script
  extrait le destinataire, la date et l'objet (en ignorant les médias).

Résultat : ~84 mails en base au moment de la rédaction, historique compris.

## 9. Déploiement / exploitation

**Déploiement complet** (sur le serveur, depuis `/opt/volunteer-apps/apps/website-meeting`) :

```bash
bash utils/deploy/deploy.sh            # sync + backfill + installe le timer (modération)
AUTO=1 bash utils/deploy/deploy.sh     # idem en publication directe
DRY_RUN=1 … bash utils/deploy/deploy.sh   # aperçu, n'écrit rien
```

**Reprise historique ponctuelle** : `MBOX=…`, `EMLDIR=…` ou `PASTED=…` devant la
commande `deploy.sh`.

**Basculer le run quotidien en publication directe** : ajouter
`IMPORT_AUTO_PUBLISH=1` au fichier de secrets
`/opt/volunteer-apps/secrets/website-meeting.env`.

**Configuration** (dans le fichier de secrets, jamais en dur) :
`IMAP_HOST`, `IMAP_USER`, `IMAP_APP_PASSWORD` (mot de passe d'application Gmail).

**Logs** : `journalctl -u import-campaign-mails.service -n 50`

## 10. Limites connues

- Le sync **met à jour** les fiches existantes mais n'**ajoute pas** un·e élu·e
  absent·e du CRM : l'ajout de nouvelles personnes reste manuel (scripts
  `insert_*`).
- Une quinzaine de sénateur·ices ne publient aucun email → non matchables par
  adresse (l'outil d'envoi utilise pour eux un formulaire de contact).
- La reprise `--pasted` dépend de la qualité du copier-coller (destinataire +
  date + objet ; pas le corps).

---

*Code et détails : branche `claude/automate-citizen-emails-crm-n8qfr0`,
dossier `utils/`. Voir `utils/README.md` pour la référence complète des options.*
