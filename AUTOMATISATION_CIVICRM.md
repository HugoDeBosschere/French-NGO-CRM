# Automatisation : CiviCRM → CRM

PauseIA fait tourner un second CRM, **CiviCRM 6.15 Standalone**, sur le même
serveur, pour les envois de masse (newsletters, communiqués). Il contient
**~12 900 journalistes avec leur adresse e-mail et leur média** — des données que
ce CRM-ci n'a pas, et qui bloquaient le rapprochement automatique des courriels
avec la presse.

Ce document décrit le pont entre les deux. Il complète
`AUTOMATISATION_MAILS_MEMBRES.md` et `AUTOMATISATION_MAILS_CAMPAGNE.md`.

---

## Le principe : à la demande, pas en masse

On **ne recopie pas** les 12 900 journalistes. Deux raisons :

1. **L'interface casserait.** `new_meeting.html` et `new_mail.html` rendent un
   `<option>` *et* une case à cocher par personne. À 1 200 personnes ça passe ; à
   17 000, les écrans de saisie pèsent plusieurs mégaoctets et deviennent
   inutilisables.
2. **Ce CRM est un journal d'interactions**, pas un fichier d'envoi. Une fiche n'a
   de sens que pour quelqu'un avec qui on a réellement échangé.

Donc : **un journaliste obtient une fiche le jour où un membre lui écrit.** Les
adresses restent disponibles à 100 % côté CiviCRM sans être dupliquées ici — ce
qui est aussi beaucoup plus propre côté RGPD, les deux bases n'ayant pas la même
finalité.

**Exception : les médias.** 168 organisations, contre 165 déjà présentes ici.
Elles n'apparaissent dans aucun sélecteur de personnes, et une fiche journaliste
résolue a besoin de son média. Elles sont donc importées en une fois.

---

## Les trois règles qui protègent CiviCRM

| Règle | Pourquoi |
|---|---|
| **APIv4 uniquement, jamais MySQL** | Le schéma de CiviCRM change entre versions majeures ; son APIv4 est un contrat maintenu. Une lecture SQL directe casse silencieusement un jour. |
| **Le CLI `cv` en local, pas l'API REST** | Même serveur : aucune clé d'API à stocker, pas besoin d'activer `authx`, rien d'exposé sur Internet. |
| **Le périmètre presse par id de groupe** | `PRESS_GROUP_IDS` en tête de `civicrm-sync.sh` (12, 28, 69). Un groupe enfant hérite du filtre de son parent, donc les trois parents couvrent les ~40 groupes départementaux. |
| **Un seul sens, lecture seule** | CiviCRM porte les donateurs et les contributions Stripe / HelloAsso. Aucun script d'ici ne peut y écrire. Le pire qu'un bug puisse produire est « rien n'a été importé ». |

Les scripts de ce dépôt **ne parlent jamais à CiviCRM**. Ils lisent le JSON que
`cv` a écrit. Pas de réseau, pas d'identifiants, aucune dépendance aux entrailles
de CiviCRM.

---

## Le cycle complet

Les deux applications sont dans des conteneurs Docker séparés (`civicrm-web` et
`website-meeting-app`), et la base de ce CRM vit **dans** son conteneur. Aucun des
deux ne peut appeler l'autre — c'est voulu. L'hôte fait l'intermédiaire.

```
1. import_member_mails.py    rencontre une adresse qu'il ne sait pas rattacher
                             → civicrm_pending  (au lieu de l'ignorer)

2. civicrm_lookup.py --list-pending      → la liste des adresses

3. l'hôte : cv api4 Email.get             → quelle adresse appartient à qui
           puis cv api4 Contact.get       → les fiches (lecture seule)

4. civicrm_lookup.py --apply             → crée la fiche, son média, son alignement

5. import_member_mails.py --backfill     → le mail se rattache enfin
                                           (imported_mails évite tout doublon)
```

Tout est orchestré par **`utils/deploy/civicrm-sync.sh`**, qui tourne sur l'hôte :

```bash
sudo /opt/scripts/civicrm-sync.sh            # dry run, n'écrit rien
sudo /opt/scripts/civicrm-sync.sh --commit
```

### Planification

`civicrm-sync.service` + `civicrm-sync.timer`, à **06:30 UTC** — après
l'import des mails de membres de 06:10, qui est précisément ce qui remplit la
file que cette synchronisation vide. L'ordre compte.

```bash
sudo cp utils/deploy/civicrm-sync.sh /opt/scripts/ && sudo chmod +x /opt/scripts/civicrm-sync.sh
sudo cp utils/deploy/civicrm-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now civicrm-sync.timer
systemctl list-timers civicrm-sync.timer
journalctl -u civicrm-sync -n 40        # après le premier passage
```

Lancez d'abord `sudo /opt/scripts/civicrm-sync.sh` **sans `--commit`** : rien
n'est écrit et la sortie dit exactement ce qui serait créé.

### Si des alertes « database is locked » arrivent

Le 29/10/2026, `import-member-mails.service` échouait toutes les dix minutes sur
`sqlite3.OperationalError: database is locked`, et `civicrm-sync.service` était
coupé à 30 minutes (« start operation timed out »). Il y avait trois causes :

- le balayage complet de l'étape 5 (`import_member_mails.py --backfill`, plus de
  4 400 courriels) gardait une transaction d'écriture ouverte pendant qu'il
  téléchargeait les courriels. Désormais, chaque courriel est validé aussitôt
  (`tests/test_import_verrou.py`) ;
- ce balayage ne tenait plus dans les 30 minutes du service : la limite passe à
  2 h ;
- quand systemd coupe le service, il ne tue que le client `docker exec`, pas le
  Python lancé dans le conteneur, qui continuait seul. L'étape 5 tourne donc
  sous `timeout 110m` dans le conteneur.

Pour vérifier qu'aucun balayage orphelin ne tourne encore dans le conteneur :

```bash
sudo docker top website-meeting-app | grep import_member_mails
```

Une ligne `--backfill` en dehors du créneau de 06:30 UTC est un orphelin. On
peut l'arrêter sans risque (`sudo kill <PID>`, le PID de la 2e colonne) :
chaque courriel déjà validé reste acquis, et `imported_mails` évite les
doublons au passage suivant.

Après la mise à jour du code, recopier les deux fichiers de déploiement modifiés :

```bash
sudo cp utils/deploy/civicrm-sync.sh /opt/scripts/ && sudo chmod +x /opt/scripts/civicrm-sync.sh
sudo cp utils/deploy/civicrm-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Pourquoi deux requêtes pour les adresses

CiviCRM porte **~1,6 adresse par journaliste** (21 401 adresses pour 12 987
contacts), et celle à laquelle un membre a écrit n'est souvent **pas la
primaire**. Filtrer `Contact.get` sur `email_primary.email` les manque en
silence — l'adresse est alors classée « inconnue de CiviCRM » alors qu'elle y
est. D'où `Email.get` d'abord, pour savoir à quel contact appartient chaque
adresse, puis `Contact.get` sur ces identifiants.

Quand la fiche existe déjà avec une autre adresse, celle qui a été rencontrée
est enregistrée dans **`person_emails`** — la table d'alias que l'import de
mails consulte déjà. Sans ça, le prochain mail venant de cette adresse
repartirait en file indéfiniment.

---

## Amorçage : casser l'œuf et la poule

> **Correction (24/09/2026).** Ce document a longtemps affirmé que la règle
> Google Workspace ne copiait que les mails touchant un domaine parlementaire,
> et qu'il fallait l'élargir pour la presse. **C'est faux.** Son expression est
> une seule regex sur les en-têtes complets — `@pauseia\.fr` — en entrant et en
> sortant : elle copie toute la correspondance externe de l'association. Les
> échanges avec les journalistes atteignaient donc déjà la boîte d'audit, et
> **rien n'est à faire côté Workspace**.

`civicrm-seed.sh` reste utile, pour une autre raison : sans fiches journalistes,
une adresse de presse arrivant dans la boîte d'audit n'est rattachable à
personne, et aucune convention d'adresses n'est apprise. Il crée les fiches
d'**un seul groupe restreint** (12 « Presse - Nationale » par défaut).

```bash
sudo /opt/scripts/civicrm-seed.sh                      # dry run
sudo /opt/scripts/civicrm-seed.sh --commit
sudo SEED_GROUP_ID=28 /opt/scripts/civicrm-seed.sh --commit   # un autre groupe
```

**Ce n'est pas l'import de masse qu'on a refusé.** `civicrm_lookup.py --seed`
refuse au-delà de **1 500 fiches** (`SEED_SOFT_CAP`) sans `--force` : au-delà,
les sélecteurs de personnes des formulaires de rencontre et de courriel
deviennent inutilisables. Un amorçage doit rester étroit.

Vérifié sur un jeu réaliste — 40 journalistes répartis sur 8 médias : 40 fiches,
10 organisations Média, 42 liens, et la liste de domaines dérivée passe de 3
à 11.
Avec seulement une ou deux fiches par média, rien ne sortirait : un domaine ne
compte qu'à partir de **deux personnes connues**, ce qui est la règle qui écarte
les adresses personnelles.

La dernière étape de `civicrm-seed.sh` affiche la liste des domaines connus.
Elle n'a **pas** à être reportée dans Workspace (voir la correction plus haut) ;
elle sert à voir d'un coup d'œil quels médias le CRM sait désormais reconnaître,
et alimente le scan du corps des mails côté code (`maildomains.py`).

## Apprendre la convention de chaque média

L'amorçage ne sert pas qu'à remplir quelques fiches. Les adresses qu'il apporte
valent plus qu'elles-mêmes : **une rédaction suit presque toujours une seule
convention**, donc quelques adresses réelles disent comment *toutes* les
adresses de ce média sont construites.

```
tvey@lefigaro.fr        Tristan Vey
ebastie@lefigaro.fr     Eugénie Bastié      →  lefigaro.fr = <initiale><nom>
cdemalet@lefigaro.fr    Caroline De Malet
```

Dès lors, un mail venant de `mlefebvre@lefigaro.fr` — une adresse que **ni le
CRM ni CiviCRM ne connaissent** — se remonte jusqu'à une « M… Lefebvre » qui
écrit pour Le Figaro, personne que CiviCRM connaît par son **nom**.

C'est l'étape `4b/5` de `civicrm-sync.sh`, et le dernier recours : elle
n'intervient que sur les adresses que la correspondance exacte n'a pas résolues.

### Ce que ça ne fait pas

**Ça reconnaît des adresses, ça n'en invente jamais.** Construire une adresse à
partir d'un nom pour y écrire serait deviner les coordonnées de quelqu'un, avec
un risque réel d'écrire à un inconnu. Les modèles ne servent que dans un sens :
reconstruire l'adresse d'une personne **connue** et la comparer à une adresse
réellement apparue dans la boîte d'audit.

### Les garde-fous

- **Deux exemples minimum, et une nette majorité (60 %).** Une seule adresse
  correspond toujours à plusieurs modèles ; en faire une convention
  mésattribuerait tous les mails suivants. Un média qui mélange réellement les
  conventions n'en reçoit aucune et retombe sur le rapprochement générique.
  La règle était d'abord l'unanimité, et elle s'est effondrée à l'échelle :
  `francetv.fr` porte 2 055 adresses dans CiviCRM, massivement `prenom.nom`, et
  une poignée d'exceptions historiques faisait taire toute la rédaction. La part
  effectivement expliquée est enregistrée (colonne `share`) et affichée.
- **L'ambiguïté est refusée, jamais tranchée.** `pdupont@lefigaro.fr` avec un
  Pierre Dupont *et* un Paul Dupont à la rédaction : l'adresse reste en file
  avec les deux noms affichés, pour qu'un humain décide. L'ambiguïté se juge sur
  les **noms**, pas sur les identifiants CiviCRM : celui-ci porte des doublons
  (`pierre.debaudouin@francetv.fr` correspondait à cinq fiches nommées « Pierre
  de Baudouin »), et refuser de trancher entre quelqu'un et lui-même rendait tout
  journaliste dupliqué définitivement irrattachable.
- **Chaque fiche le dit.** Ses notes portent « Adresse reconnue par la
  convention de LE FIGARO (« pnom ») — à confirmer ». Une convention est une
  habitude, pas une règle.

Vérifié de bout en bout : 4 journalistes du Figaro semés → convention `pnom`
apprise → `mlefebvre@lefigaro.fr` reconnue, `pdupont@lefigaro.fr` refusée pour
homonymie, `zzzinconnu@lefigaro.fr` laissée en file.

```bash
python3 utils/civicrm_lookup.py --patterns        # les conventions apprises
python3 utils/learn_conventions.py --show         # celles apprises sur CiviCRM
```

### Apprendre sur les 12 900, pas sur nos 587

Déduire la convention du Figaro de nos propres fiches suppose d'avoir des fiches
du Figaro. Or l'amorçage n'en a créé que pour un groupe presse restreint : 587
journalistes, 44 conventions. Pour les rédactions dont nous ne tenons **qu'une**
fiche — `nouvelobs.com`, `francetv.fr` — il n'y avait rien à apprendre, et leurs
adresses étaient donc écartées à chaque fois.

`learn_conventions.py` (étape `1b/5`) apprend sur **tous** les journalistes de
CiviCRM qui ont une adresse, indépendamment des groupes presse : des centaines de
médias au lieu de quelques dizaines, et chaque convention adossée à des dizaines
d'adresses au lieu de deux. Une convention est un fait sur une rédaction, pas sur
nos fiches.

**Et ça ne crée aucune fiche.** Le script lit des noms et des adresses, n'en
retient que ce qu'il en déduit — un domaine, un média, un gabarit, un compteur —
et jette le reste ; l'export est supprimé du conteneur à la fin de l'étape. Les
12 900 journalistes restent dans CiviCRM, où ils ont leur place : ce qui arrive
ici, c'est la forme de `<initiale><nom>@lefigaro.fr`, pas les personnes. La table
`mail_conventions` ne contient ni nom ni adresse — un test le vérifie.

Deux effets, les mêmes que ci-dessus mais bien plus loin :

- `maildomains` compte désormais comme connu tout domaine attesté par CiviCRM,
  sans attendre nos deux fiches — c'est ce qui a débloqué les adresses de
  `francetv.fr` et `nouvelobs.com` qui partaient à la poubelle. Les messageries
  grand public en restent exclues, la règle vaut plus que la table.
- le rapprochement par convention (`4b/5`) dispose de centaines de médias.

**Les candidats sont cherchés par domaine, pas par nom de média.** CiviCRM
étiquette les employeurs par région : `francetv.fr` ressort en « FRANCE 3 PARIS
ILE-DE-FRANCE », `centrefrance.com` en « LE POPULAIRE DU CENTRE ». Interroger par
employeur ne comparait donc une adresse en attente qu'aux quelques dizaines de
journalistes du libellé majoritaire, au lieu des 2 055 de `francetv.fr`. Le
domaine est ce qui est stable ; le nom d'employeur ne sert plus que de filet
supplémentaire, pour un ou une pigiste dont l'adresse personnelle est ailleurs.

Là où les deux sources se contredisent, CiviCRM l'emporte : plus d'exemples.

```bash
# dans le conteneur, à partir de l'export produit par civicrm-sync.sh
python3 utils/learn_conventions.py --file /tmp/civi-journalists.json          # dry-run
python3 utils/learn_conventions.py --file /tmp/civi-journalists.json --commit
```

---

## Qui mérite une fiche, et qui n'en mérite pas

CiviCRM ne contient pas que des journalistes : il porte aussi **les bénévoles,
adhérent·es et sympathisant·es de l'association**. Une recherche par adresse les
trouve tout aussi bien. Le premier passage réel l'a montré crûment — sur 26
contacts identifiés, **21 étaient des membres ou des allié·es**, dont un collègue
de l'équipe.

Ce CRM suit les relations **externes**. Une fiche de contact pour son propre
coéquipier n'a pas de sens.

Règle appliquée : un contact dont le sous-type CiviCRM ne correspond à aucun
`CONTACT_TYPE` d'ici (donc qui atterrirait en `Autre`) est **écarté** et marqué
`absent`. `--include-other` lève la restriction si besoin.

Conséquence assumée : un journaliste que CiviCRM n'aurait pas typé passe à la
trappe. C'est le bon compromis — il finira en file, visible dans `--report`, et
un humain pourra le typer dans CiviCRM. L'inverse (remplir le CRM de bénévoles)
serait bien plus pénible à défaire.

---

## Correspondance des données

| CiviCRM | Ici | Note |
|---|---|---|
| Sous-type `Journaliste` | `contact_type = 'Journaliste'` | |
| Sous-type organisation `M_dia` | `organisations.org_type = 'Média'` | |
| `employer_id` | `person_organisations` | le journaliste et son média |
| `Analyse_strat_gique_Pause_IA.Alignement` | `stance` | lu par **valeur** (`1`–`5`), jamais par libellé : ceux-ci contiennent des emoji et sont modifiables dans l'interface |
| `Analyse_strat_gique_Pause_IA.Niveau_d_influence` | `notes` | aucune colonne ici ne le porte |
| `Description_courte.Description_courte` | `notes` | |
| `Compte_R_seaux_Sociaux.Twitter` / `.LinkedIn` | `social_links` | |

**Alignement → stance** : `1` Neutre → *Neutre / indécis* · `2` Partiel → *Plutôt
favorable* · `3` Total → *Favorable* · `4` Opposé → *Opposé* · `5` Indéterminé et
absent → *Inconnu*. Ce CRM a un degré de plus (*Plutôt opposé*) que CiviCRM n'a
pas ; sans conséquence puisqu'on ne fait que lire.

### Pièges vérifiés sur les données réelles

- **Les sous-types remontent en noms techniques** : `M_dia`, `Expert_IA`,
  `Recherche_ducation`, `B_n_vole` — jamais les libellés de l'interface. Et le
  champ est **multivalué** : un contact peut porter
  `["B_n_vole","Expert_IA","Influenceur"]`.
- **Les médias sont en capitales** dans CiviCRM (`LE FIGARO`) contre une casse
  normale ici (`Le Figaro`). Le rapprochement se fait sur un nom replié (sans
  accents, sans casse, sans ponctuation), sinon le premier passage crée 165
  doublons.
- **`LE FIGARO` et `LE FIGARO ECONOMIE` restent deux organisations distinctes.**
  On garde ce que dit CiviCRM ; deviner une hiérarchie de rédactions par le nom
  finit toujours mal. Un modérateur peut fusionner à la main.
- **Un homonyme dans un autre type reste une fiche séparée** (« Laurent
  Alexandre » est à la fois député LFI et chroniqueur), comme dans tous les
  scripts `insert_*`.

### Ce qui n'est jamais écrasé

Une fiche existante n'est **jamais remplacée**, seulement complétée :

- une adresse, des réseaux sociaux ou des notes déjà renseignés sont conservés ;
- `stance` n'est repris que s'il vaut encore `Inconnu` — personne n'a choisi cette
  valeur-là ;
- `added_by` / `validated_by` restent `NULL`, la convention du dépôt pour « amené
  par un script ».

Concrètement, les **133 journalistes** hérités de l'ancien CRM presse (un nom, et
rien d'autre) se complètent tout seuls au lieu d'être dupliqués.

---

## La file d'attente

Table `civicrm_pending` :

| statut | sens |
|---|---|
| `pending` | vue dans un mail, en attente de recherche CiviCRM |
| `resolved` | une fiche existe |
| `absent` | CiviCRM ne la connaît pas non plus — gardée pour ne pas la rechercher chaque nuit |

```bash
python3 utils/civicrm_lookup.py --stats
python3 utils/civicrm_lookup.py --retry-absent --commit   # après avoir enrichi CiviCRM
```

### Ce qui entre dans la file — et le piège évité

Une adresse **inconnue sur un domaine de média connu** est le meilleur candidat
possible à une recherche CiviCRM : c'est un journaliste du Figaro dont on n'a
pas la fiche. Elle doit donc entrer en file.

Ça n'a pas toujours été le cas. `queue_unknown_counterparts` écartait
`is_official(address)` — qui teste le **domaine**, pas l'adresse. Le raccourci
se défendait quand « connu » désignait les trois domaines parlementaires : toute
adresse d'élu·e y était déjà dans l'index, donc une non reconnue était une
adresse de cabinet, traitée par l'apprentissage d'alias via les fils. Dès que
les domaines de médias ont rejoint la liste, il a commencé à jeter précisément
ce que cette file existe pour attraper.

Symptôme : le premier balayage complet a mis 195 adresses en file, **aucune sur
un domaine de presse**. Seul le côté membre est écarté désormais.

### Ce qui n'entre jamais dans la file

Le premier passage réel sur la boîte d'audit a produit **quatre adresses, toutes
des robots, aucun journaliste** :

```
automated@airbnb.com
notify@mail.notion.com
notify@mail.notion.so
bonjour@fresquedesrisquesdelia.org
```

D'où trois filtres, du plus solide au plus littéral :

1. **Les en-têtes d'envoi en masse** (`is_bulk`). Une newsletter, une
   notification Notion ou un reçu Airbnb portent `List-Unsubscribe`, `List-Id`,
   `Precedence: bulk` ou `Auto-Submitted` ; un message écrit par une personne
   n'en porte aucun. C'est le filtre qui tient dans le temps — une liste de mots
   aura toujours un train de retard sur le prochain robot SaaS.
2. **Nos propres domaines** (`OWN_DOMAINS`, surchargeable par `CRM_OWN_DOMAINS`) :
   `pauseia.fr`, `fresquedesrisquesdelia.org`, `pauseai.info`, sous-domaines
   compris. Un mail venant de la Fresque est interne, pas un contact à ficher.
3. **Les boîtes partagées et les robots par nom** (`GENERIC_LOCALPARTS`) :
   `redaction@`, `contact@`, `presse@`, `notify@`, `automated@`, `bonjour@`…

Quand les filtres se resserrent, la file garde les adresses jugées selon les
anciennes règles. `--prune` les repasse au crible :

```bash
python3 utils/civicrm_lookup.py --prune             # montre
python3 utils/civicrm_lookup.py --prune --commit    # retire
```

---

## Après une mise à jour de CiviCRM

Tout ce qui dépend de CiviCRM est dans **`utils/civicrm.py`**. C'est le seul
fichier qu'une montée de version peut casser, et il commence par un **test de
contrat** : si un champ personnalisé a été renommé ou supprimé, `assert_contract`
lève une erreur et **rien n'est écrit**.

```
ABANDON — contrat CiviCRM non respecté : contact export is missing 1 expected
field(s): Analyse_strat_gique_Pause_IA.Alignement. …
```

La marche à suivre est alors : comparer les `select` de
`utils/deploy/civicrm-sync.sh` avec `CONTACT_FIELDS` dans `utils/civicrm.py`, et
retrouver le nouveau nom technique avec

```bash
docker exec civicrm-web cv api4 CustomField.get '{"select":["name","label","custom_group_id:name"],"limit":50}' --cwd=/var/www/html
```

Avec lecture seule + arrêt sur contrat non satisfait, le pire qu'une mise à jour
puisse produire est **une synchronisation qui ne tourne plus**. Jamais de données
abîmées.

### Champs requis et champs facultatifs

Tous les champs ne valent pas un arrêt. `CONTACT_FIELDS_REQUIRED` — nom, média,
alignement, description — manquants, une fiche serait *fausse* et non seulement
incomplète : la synchro s'arrête. `CONTACT_FIELDS_OPTIONAL` (Twitter, LinkedIn)
ne produit qu'un avertissement.

La distinction vient d'un cas réel : sur l'export de 593 contacts du groupe 12,
`Compte_R_seaux_Sociaux.Twitter` et `.LinkedIn` étaient **absents de toutes les
lignes**. CiviCRM stocke ce groupe de champs personnalisés en **multi-valeurs**
(« repeating »), et la syntaxe pointée d'APIv4 ne les expose pas du tout — ils
forment une entité séparée. Perdre un compte Twitter n'est pas une raison de
refuser 593 journalistes.

Pour vérifier de quel type est un groupe :

```bash
docker exec civicrm-web cv api4 CustomGroup.get '{"select":["name","title","is_multiple","extends"],"limit":50}' --cwd=/var/www/html
```

`is_multiple: true` confirme qu'il faut passer par l'entité dédiée
(`Custom_<nom_du_groupe>.get`, filtrée sur `entity_id`) plutôt que par le
`select` de `Contact.get`. Non implémenté à ce jour : les réseaux sociaux sont
simplement ignorés.

Le contrat lit par ailleurs les clés d'un **échantillon** d'enregistrements
(`CONTRACT_SAMPLE`), pas seulement du premier — une ligne inhabituelle ne doit
pas faire échouer tout un export.

---

## Points ouverts

- **`Influenceur` et `Expert IA`** n'existent pas dans `CONTACT_TYPES` ici : ces
  contacts arrivent en `Autre`, avec leur sous-type d'origine conservé dans les
  notes. CiviCRM en compte **104 experts IA et 4 influenceurs** — les experts
  méritent probablement leur propre type, les influenceurs peuvent attendre.
- **Les rebonds fonctionnent.** Vérifié : **3 746 rebonds enregistrés**
  (`MailingEventBounce`) et **63 391 envois en file** (`MailingEventQueue`). Le
  mécanisme tourne, les adresses ont bien été sollicitées. Sur les 21 403
  adresses de journalistes, **75 sont suspendues** (73 en `on_hold = 1` après
  rebonds, 2 manuellement). Le taux est réellement bas, ce n'est pas un défaut de
  mesure — CiviCRM ne pose `on_hold` qu'au-delà d'un seuil de rebonds durs, donc
  une partie des 3 746 rebonds n'a pas encore atteint ce seuil.

  **On ne filtre volontairement pas dessus ici** : il s'agit d'identifier
  quelqu'un avec qui on a *déjà* échangé, pas de décider si on peut lui écrire.
  Une adresse morte désigne quand même une personne. Le sujet compte en revanche
  beaucoup pour les envois de CP.

### Suivre l'état des adresses

```bash
# Suspensions, sur TOUTES les adresses (pas seulement les primaires)
docker exec civicrm-web cv api4 Email.get '{"select":["on_hold","COUNT(id) AS total"],"where":[["contact_id.contact_sub_type","CONTAINS","Journaliste"]],"groupBy":["on_hold"]}' --cwd=/var/www/html

# Rebonds enregistrés, indépendamment du seuil qui déclenche on_hold
docker exec civicrm-web cv api4 MailingEventBounce.get '{"select":["COUNT(id) AS total"]}' --cwd=/var/www/html
```
- **Les relances en double.** CiviCRM porte aussi `Suivi_Activit_s_Pause_IA`
  (Résultat, Suivi Requis, Score Impact, Date Prochain Contact), qui recouvre le
  `/todo` d'ici. Il faudra décider qui fait référence pour « quand relancer qui »,
  sinon deux dates contradictoires coexisteront sur la même personne.
- **2 312 organisations non typées** dans les groupes presse de CiviCRM (contre
  168 portant le sous-type `M_dia`). Ce sont presque certainement des médias ; ça
  se corrige dans CiviCRM, pas ici.
