#!/usr/bin/env bash
# Seed the CRM with one narrow CiviCRM group, once, to get the cycle started.
#
# What it breaks: the on-demand path can only resolve an address against fiches
# that exist, and can only learn a média's address convention from addresses
# already held. With an empty press half every journalist mail is queued and
# none is attributed. Seeding one group puts real journalists in the CRM with
# their médias, and civicrm-sync.sh resolves the rest on demand from there.
#
# NB: this is not about Workspace. The rule feeding the audit mailbox matches
# `@pauseia\.fr` over full headers, so press mail has always been copied there.
#
# Deliberately NOT the bulk import: group 12 (Presse - Nationale) alone, not the
# 12 900 journalists, which would make the rencontre and courriel pickers
# unusable. civicrm_lookup.py refuses past its soft cap unless forced.
#
# Read-only on CiviCRM, like everything else here: `cv api4 ….get`.
#
#   sudo /opt/scripts/civicrm-seed.sh              # dry run, writes nothing
#   sudo /opt/scripts/civicrm-seed.sh --commit
#   sudo SEED_GROUP_ID=28 /opt/scripts/civicrm-seed.sh --commit
#
set -euo pipefail

# 12 = "Presse - Nationale". The narrowest group worth seeding; 28 (Régionale)
# and 69 (Par thématique) carry their département children with them and are
# much larger, so take them one at a time if at all.
SEED_GROUP_ID="${SEED_GROUP_ID:-12}"

CIVI_CONTAINER="${CIVI_CONTAINER:-civicrm-web}"
CRM_CONTAINER="${CRM_CONTAINER:-website-meeting-app}"
CIVI_CWD="${CIVI_CWD:-/var/www/html}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

COMMIT=""
[ "${1:-}" = "--commit" ] && COMMIT="--commit"

say() { printf '\n== %s\n' "$*"; }
civi() { docker exec "$CIVI_CONTAINER" cv api4 "$1" "$2" --cwd="$CIVI_CWD" 2>/dev/null; }

# utils/ est dans l'image (deploy/Dockerfile) : rien à recopier.

say "1/3  Médias first — a seeded journalist needs their média to exist"
"$(dirname "$0")/civicrm-sync.sh" --medias-only ${COMMIT:+--commit} || true

say "2/3  Journalists of group $SEED_GROUP_ID"
# Only contacts with an address: a fiche without one teaches the domain list
# nothing, which is the whole point of seeding.
civi Contact.get \
  "{\"select\":[\"id\",\"display_name\",\"contact_sub_type\",\"email_primary.email\",\"employer_id.display_name\",\"Analyse_strat_gique_Pause_IA.Alignement\",\"Analyse_strat_gique_Pause_IA.Niveau_d_influence\",\"Description_courte.Description_courte\",\"Compte_R_seaux_Sociaux.Twitter\",\"Compte_R_seaux_Sociaux.LinkedIn\"],\"where\":[[\"groups\",\"IN\",[$SEED_GROUP_ID]],[\"contact_sub_type\",\"CONTAINS\",\"Journaliste\"],[\"email_primary.email\",\"IS NOT EMPTY\"],[\"is_deleted\",\"=\",false]],\"limit\":0}" \
  > "$WORK/civi-seed.json"
echo "   $(grep -c '"id"' "$WORK/civi-seed.json" || true) contact(s) exporté(s)"

docker cp "$WORK/civi-seed.json" "$CRM_CONTAINER:/tmp/civi-seed.json"
docker exec "$CRM_CONTAINER" python3 /app/utils/civicrm_lookup.py \
  --seed /tmp/civi-seed.json --verbose $COMMIT

say "3/3  The domains the CRM now recognises"
if [ -n "$COMMIT" ]; then
  docker exec "$CRM_CONTAINER" python3 /app/utils/maildomains.py --list
  echo
  echo "   ^ these are used to spot a quoted address in a mail body and to"
  echo "     decide whether an address is already known. Nothing to paste into"
  echo "     Workspace: its rule matches @pauseia.fr and copies everything."
else
  echo "   (dry-run : rien n'a été créé, donc rien de nouveau à lister)"
fi

say "Terminé."
