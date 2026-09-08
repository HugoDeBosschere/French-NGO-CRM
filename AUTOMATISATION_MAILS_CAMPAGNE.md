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

## 7. RGPD

Minimisation : on ne stocke que **l'élu·e, la date et l'objet** du mail.
**L'identité du citoyen n'est jamais enregistrée** (l'en-tête `From` n'est pas lu).

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
