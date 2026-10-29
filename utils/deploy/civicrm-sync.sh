#!/usr/bin/env bash
# Bridge CiviCRM -> the CRM, from the host. Read-only on the CiviCRM side.
#
# The two apps run in separate Docker containers on the same server: CiviCRM in
# `civicrm-web`, this CRM in `website-meeting-app` with its SQLite inside the
# container. Neither can call the other, and we deliberately keep it that way —
# no API key, no `authx`, nothing exposed. So the host plays go-between: it asks
# CiviCRM with `cv` (which needs no credentials locally), hands the JSON over
# with `docker cp`, and lets this CRM's own scripts do the writing.
#
# Everything here is `cv api4 … .get` — reads only. Nothing in this file, and
# nothing it calls, can write to CiviCRM.
#
# Run order matters: médias first, so a journalist resolved in step 3 finds their
# média already there.
#
#   sudo /opt/scripts/civicrm-sync.sh            # dry run, writes nothing
#   sudo /opt/scripts/civicrm-sync.sh --commit
#
set -euo pipefail

# The press scope, by group id. Ids rather than titles is Romain's call as the
# CiviCRM maintainer, and it matches how mosaicotweaks already pins them:
#   12  Presse - Nationale
#   28  Presse - Régionale      (+ its ~40 département children)
#   69  Presse - Par thématique (+ its children)
# A child group inherits its parent's contacts in CiviCRM's `groups` filter, so
# the three parents cover the lot. If the press groups are ever renumbered, this
# line and mosaicotweaks.php are the two places to change.
PRESS_GROUP_IDS="${PRESS_GROUP_IDS:-12,28,69}"

CIVI_CONTAINER="${CIVI_CONTAINER:-civicrm-web}"
CRM_CONTAINER="${CRM_CONTAINER:-website-meeting-app}"
CIVI_CWD="${CIVI_CWD:-/var/www/html}"
REPO="${REPO:-/opt/volunteer-apps/apps/website-meeting}"
WORK="$(mktemp -d)"

# `set -e` interrompt le script à la première erreur : sans ce piège, une
# synchro morte en cours de route ne laisserait aucune trace, ni dans la base
# ni à l'écran de qui que ce soit.
echec() {
  code=$?
  rm -rf "$WORK"
  if [ "$code" -ne 0 ] && [ -n "${COMMIT:-}" ]; then
    docker exec "$CRM_CONTAINER" python3 /app/utils/importruns.py \
      --record civicrm_sync --status error \
      --detail "arrêt sur erreur (code $code)" || true
  fi
}
trap echec EXIT

COMMIT=""
MEDIAS_ONLY=""
for arg in "$@"; do
  case "$arg" in
    --commit)      COMMIT="--commit" ;;
    # Used by civicrm-seed.sh: a seeded journalist needs their média to exist
    # first, and there is no point sweeping the queue during a seed.
    --medias-only) MEDIAS_ONLY="1" ;;
    *) echo "argument inconnu : $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }

# cv prints PHP startup warnings on stderr; keep stdout clean for the JSON.
civi() { docker exec "$CIVI_CONTAINER" cv api4 "$1" "$2" --cwd="$CIVI_CWD" 2>/dev/null; }

say "0/5  Refreshing utils/ inside $CRM_CONTAINER"
docker exec "$CRM_CONTAINER" rm -rf /app/utils
docker cp "$REPO/utils" "$CRM_CONTAINER:/app/utils"

# --------------------------------------------------------------------------- #
say "1/5  Médias (organisations) from CiviCRM"
# Sub-types are machine names, never the interface labels: "M_dia", not "Média".
civi Contact.get \
  "{\"select\":[\"id\",\"display_name\",\"contact_sub_type\"],\"where\":[[\"contact_type\",\"=\",\"Organization\"],[\"contact_sub_type\",\"CONTAINS\",\"M_dia\"],[\"groups\",\"IN\",[$PRESS_GROUP_IDS]],[\"is_deleted\",\"=\",false]],\"limit\":0}" \
  > "$WORK/civi-medias.json"
# NB: scoped to the press groups *and* the M_dia sub-type. Those groups also hold
# ~2 300 organisations with no sub-type at all — almost certainly médias too, but
# that is a CiviCRM data-quality fix, not something to guess at here.
echo "   $(grep -c '"id"' "$WORK/civi-medias.json" || true) média(s) exporté(s)"

docker cp "$WORK/civi-medias.json" "$CRM_CONTAINER:/tmp/civi-medias.json"
docker exec "$CRM_CONTAINER" python3 /app/utils/import_civicrm_medias.py \
  --file /tmp/civi-medias.json $COMMIT

if [ -n "$MEDIAS_ONLY" ]; then
  say "Médias seulement — terminé."
  exit 0
fi

# --------------------------------------------------------------------------- #
say "1b/5  Learning every média's address convention from CiviCRM"
# All the journalists CiviCRM holds an address for — ~12 900, not just the press
# groups: a convention is a fact about a newsroom, and the more addresses back it
# the safer it is. Three fields only, and the file never leaves this machine: the
# script keeps a domain, a média and a template, then throws the addresses away.
# No fiche is created here.
#
# This is what makes a newsroom we hold one fiche for — nouvelobs.com,
# francetv.fr — recognisable all the same.
civi Contact.get \
  "{\"select\":[\"display_name\",\"email_primary.email\",\"employer_id.display_name\"],\"where\":[[\"contact_sub_type\",\"CONTAINS\",\"Journaliste\"],[\"email_primary.email\",\"IS NOT EMPTY\",true],[\"is_deleted\",\"=\",false]],\"limit\":0}" \
  > "$WORK/civi-journalists.json"
echo "   $(grep -c '\"display_name\"' "$WORK/civi-journalists.json" || true) journaliste(s) avec adresse"

docker cp "$WORK/civi-journalists.json" "$CRM_CONTAINER:/tmp/civi-journalists.json"
docker exec "$CRM_CONTAINER" python3 /app/utils/learn_conventions.py \
  --file /tmp/civi-journalists.json $COMMIT
# The export carries every journalist's address; it has served its purpose.
docker exec "$CRM_CONTAINER" rm -f /tmp/civi-journalists.json

# --------------------------------------------------------------------------- #
say "2/5  Addresses awaiting a lookup"
docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py --list-pending \
  > "$WORK/pending.txt"
PENDING=$(wc -l < "$WORK/pending.txt" | tr -d ' ')
echo "   $PENDING adresse(s) en file"

if [ "$PENDING" -eq 0 ]; then
  say "Rien à résoudre. Terminé."
  exit 0
fi

# --------------------------------------------------------------------------- #
say "3/5  Asking CiviCRM about them"
# One query for the whole batch. `on_hold`/`do_not_email`/`is_opt_out` are NOT
# filtered here on purpose: we are identifying a person we already exchanged mail
# with, not deciding whether to mail them. A bounced address still names someone.
EMAILS=$(python3 - "$WORK/pending.txt" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.dumps([l.strip() for l in fh if l.strip()]))
PY
)
# Two steps on purpose. CiviCRM holds ~1.6 addresses per journalist (21 401 for
# 12 987 contacts), and the one a member wrote to is often NOT the primary — so
# filtering Contact.get on `email_primary.email` silently misses them. Ask the
# Email entity which contact each address belongs to, then fetch those contacts.
civi Email.get \
  "{\"select\":[\"email\",\"contact_id\"],\"where\":[[\"email\",\"IN\",$EMAILS]],\"limit\":0}" \
  > "$WORK/civi-emails.json"
echo "   $(grep -c '"email"' "$WORK/civi-emails.json" || true) adresse(s) reconnue(s)"

IDS=$(python3 - "$WORK/civi-emails.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    rows = json.load(fh)
print(json.dumps(sorted({r["contact_id"] for r in rows if r.get("contact_id")})))
PY
)
civi Contact.get \
  "{\"select\":[\"id\",\"display_name\",\"contact_sub_type\",\"email_primary.email\",\"employer_id.display_name\",\"Analyse_strat_gique_Pause_IA.Alignement\",\"Analyse_strat_gique_Pause_IA.Niveau_d_influence\",\"Description_courte.Description_courte\",\"Compte_R_seaux_Sociaux.Twitter\",\"Compte_R_seaux_Sociaux.LinkedIn\"],\"where\":[[\"id\",\"IN\",$IDS],[\"is_deleted\",\"=\",false]],\"limit\":0}" \
  > "$WORK/civi-contacts.json"
echo "   $(grep -c '"id"' "$WORK/civi-contacts.json" || true) contact(s) trouvé(s)"

# --------------------------------------------------------------------------- #
say "4/5  Creating the fiches"
docker cp "$WORK/civi-contacts.json" "$CRM_CONTAINER:/tmp/civi-contacts.json"
docker cp "$WORK/civi-emails.json" "$CRM_CONTAINER:/tmp/civi-emails.json"
docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py \
  --apply /tmp/civi-contacts.json --emails /tmp/civi-emails.json $COMMIT

# --------------------------------------------------------------------------- #
say "4b/5  Addresses left over: try each newsroom's own convention"
# A newsroom follows one convention (Le Figaro: <initiale><nom>), learned in
# 1b/5 over all of CiviCRM. So an address CiviCRM holds no record of can still
# be traced to a journalist it knows BY NAME. Recognition only — nothing here
# invents an address to write to.
# stdout is the JSON, stderr the human summary — and stderr is NOT silenced:
# hiding it is what let a parse failure here pass for "no convention applies".
docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py --patterns \
  > "$WORK/patterns.json" || echo "{}" > "$WORK/patterns.json"

# Candidates are fetched BY DOMAIN, the employer name being only an extra net.
# Asking by employer alone was the original design and it under-reached badly:
# CiviCRM labels employers regionally, so francetv.fr's 2 055 journalists are
# spread over "FRANCE 3 PARIS ILE-DE-FRANCE", "FRANCE 3 OCCITANIE" and dozens
# more, and only the majority label was ever queried.
PARAMS=$(python3 - "$WORK/patterns.json" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except (ValueError, OSError):
    data = {}
if not data:
    print("")
    raise SystemExit(0)
clauses = [["email_primary.email", "LIKE", "%@" + d] for d in sorted(data)]
medias = sorted({v["media"] for v in data.values() if v.get("media")})
if medias:
    clauses.append(["employer_id.display_name", "IN", medias])
print(json.dumps({
    "select": ["id", "display_name", "contact_sub_type", "email_primary.email",
               "employer_id.display_name",
               "Analyse_strat_gique_Pause_IA.Alignement",
               "Analyse_strat_gique_Pause_IA.Niveau_d_influence",
               "Description_courte.Description_courte",
               "Compte_R_seaux_Sociaux.Twitter",
               "Compte_R_seaux_Sociaux.LinkedIn"],
    "where": [["OR", clauses],
              ["contact_sub_type", "CONTAINS", "Journaliste"],
              ["is_deleted", "=", False]],
    "limit": 0,
}, ensure_ascii=False))
PYEOF
)

if [ -z "$PARAMS" ]; then
  # Not "seed me": --patterns lists only the conventions the *queued* addresses
  # need. An empty result usually means those addresses sit on gmail, proton and
  # the like, where no convention exists or ever could.
  echo "   aucune adresse en attente sur un domaine dont la convention est connue"
  echo "   (les conventions apprises : learn_conventions.py --show)"
else
  civi Contact.get "$PARAMS" > "$WORK/civi-names.json"
  echo "   $(grep -c '"id"' "$WORK/civi-names.json" || true) journaliste(s) candidat(e)s"
  docker cp "$WORK/civi-names.json" "$CRM_CONTAINER:/tmp/civi-names.json"
  docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py \
    --apply-names /tmp/civi-names.json $COMMIT
  docker exec "$CRM_CONTAINER" rm -f /tmp/civi-names.json
fi

# --------------------------------------------------------------------------- #
say "5/5  Re-linking the mails that were waiting"
if [ -n "$COMMIT" ]; then
  # A full sweep: the mails skipped earlier now match. `imported_mails` dedups,
  # so nothing is recorded twice.
  # `timeout` DANS le conteneur : quand systemd coupe ce service, il ne tue que
  # le client `docker exec` ; le Python continuait seul dans le conteneur et
  # gardait la base occupée (alertes « database is locked » du 29/10/2026).
  # 110 min, juste sous TimeoutStartSec=2h (civicrm-sync.service).
  docker exec "$CRM_CONTAINER" timeout 110m python3 /app/utils/import_member_mails.py --backfill
else
  echo "   (dry-run : import_member_mails.py --backfill non lancé)"
fi

# --------------------------------------------------------------------------- #
# Laisser une trace, comme les imports de courriels. Ce script orchestre des
# `docker exec` depuis l'hôte : aucun processus Python ne le couvre de bout en
# bout, donc son échec serait la seule panne de synchro totalement invisible —
# alors que c'est la plus fragile, puisqu'elle dépend d'un second logiciel.
if [ -n "$COMMIT" ]; then
  docker exec "$CRM_CONTAINER" python3 /app/utils/importruns.py \
    --record civicrm_sync --status ok \
    --detail "synchro complète (médias, conventions, file d'attente)" || true
fi

say "Terminé."
