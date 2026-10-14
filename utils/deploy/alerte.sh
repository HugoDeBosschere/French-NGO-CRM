#!/usr/bin/env bash
# Envoie une alerte quand un travail planifié du CRM a échoué.
#
# Appelé par alerte@.service, lui-même déclenché par OnFailure= sur chaque
# service. Reçoit en argument le nom de l'unité fautive.
#
# Configuration, dans /etc/pauseia-crm-alerte.env (fichier non versionné,
# chmod 600 — il contient une URL secrète) :
#
#     ALERT_WEBHOOK=https://discord.com/api/webhooks/…
#     ALERT_EMAIL=romain@pauseia.fr
#
# Sans ALERT_WEBHOOK le script se contente du journal : il ne casse jamais le
# système d'alerte par son absence de configuration.
#
# Le format JSON envoyé porte à la fois "content" (Discord) et "text" (Slack),
# comme le fait déjà scripts/update-elus-server.sh côté site : un seul script
# pour les deux destinations possibles.
#
# Un webhook Discord n'appartient à personne : plusieurs intégrations peuvent
# poster dans le même salon avec la même URL, et c'est le cas ici — celle de
# Pause IA sert déjà à autre chose. Sans "username", les alertes du CRM
# arriveraient sous le nom de l'autre intégration et se confondraient avec
# elle. Discord l'affiche à la place du nom du webhook ; Slack l'ignore.
set -uo pipefail

UNITE="${1:-inconnue}"
HOTE="$(hostname)"
ALERT_WEBHOOK="${ALERT_WEBHOOK:-}"
ALERT_EMAIL="${ALERT_EMAIL:-}"

# Les 40 dernières lignes du journal : sans elles l'alerte ne dit que « ça a
# raté », et il faut ouvrir une session sur le serveur pour savoir quoi.
JOURNAL="$(journalctl -u "$UNITE" -n 40 --no-pager 2>/dev/null || echo '(journal illisible)')"
MSG="[CRM Pause IA] Échec de ${UNITE} sur ${HOTE}

${JOURNAL}"

# Discord refuse un contenu de plus de 2000 caractères. On garde la FIN et non
# le début : l'erreur est la dernière chose que le journal écrit, et tronquer
# par le début livre une alerte qui ne dit pas pourquoi. Constaté au premier
# échec réel de sync-officials, dont l'alerte n'a transmis que la liste des
# ministres et pas la panne.
if [ "${#MSG}" -gt 1800 ]; then
	QUEUE="${MSG: -1600}"
	MSG="[CRM Pause IA] Échec de ${UNITE} sur ${HOTE}
(début du journal coupé)
...${QUEUE}"
fi
echo "$MSG" >&2

json_str() {
	printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' |
		awk 'BEGIN{printf "\""} {printf "%s%s", sep, $0; sep="\\n"} END{printf "\""}'
}

if [[ -n "$ALERT_WEBHOOK" ]]; then
	curl -s -m 15 -H 'Content-Type: application/json' \
		-d "$(printf '{"username":"Alertes CRM Pause IA","content":%s,"text":%s}' \
			"$(json_str "$MSG")" "$(json_str "$MSG")")" \
		"$ALERT_WEBHOOK" >/dev/null || echo "alerte.sh : envoi webhook échoué" >&2
fi

if [[ -n "$ALERT_EMAIL" ]] && command -v mail >/dev/null 2>&1; then
	printf '%s\n' "$MSG" | mail -s "[CRM Pause IA] Échec ${UNITE}" "$ALERT_EMAIL" 2>/dev/null ||
		echo "alerte.sh : envoi mail échoué" >&2
fi

# Toujours 0 : un échec d'alerte ne doit pas se transformer en second échec
# systemd, qui déclencherait à son tour une alerte, en boucle.
exit 0
