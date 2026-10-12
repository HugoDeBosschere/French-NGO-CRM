# Déploiement

L'application tourne dans un conteneur Docker (`website-meeting-app`) sur le
serveur Hetzner, derrière Traefik. Les tâches automatiques (synchro des élus,
imports de courriels, fusion des doublons, sauvegarde) sont des unités systemd
de l'hôte qui lancent `docker exec` dans ce conteneur : voir `utils/deploy/`.

## Le code voyage en un seul morceau

`app.py` et `utils/` doivent arriver dans le conteneur **par le même chemin**,
donc tous les deux dans l'image. Sinon ils divergent, et le 12/10/2026 ils ont
divergé : l'image portait un `app.py` de la veille, tandis que `utils/` venait
d'un checkout à jour. `insert_senateurices.py` connaissait les nouveaux groupes
du Sénat (UCI, UNT) ; le `POLITICAL_GROUPS` qu'il lit dans `app.py` ne les
connaissait pas. Le garde-fou `verifier_groupes()` a refusé de tourner — ce qui
est son travail — et la synchro a échoué deux fois de suite pour une raison qui
n'existait que dans l'assemblage.

Le `Dockerfile` doit donc copier les deux :

    COPY app.py ./
    COPY templates/ ./templates/
    COPY static/ ./static/
    COPY utils/ ./utils/
    COPY actual_dataset/ ./actual_dataset/

Corollaire : les fichiers écrits à l'exécution sous `actual_dataset/`
(`gouvernement.json`, `eurodeputes_fr.json`) et `an_legislature.json` à la
racine sont perdus à chaque reconstruction. C'est sans conséquence, ils sont
tous régénérés au passage suivant — la mémoire de législature est conçue pour
ça (voir `legislature_an()` dans `utils/sync_officials.py`).

## Déployer

    cd /opt/volunteer-apps/apps/website-meeting
    git fetch origin main && git checkout main && git pull origin main
    sudo docker compose up -d --build

`sudo` n'est pas facultatif : le fichier de secrets n'est lisible que par root,
et sans lui la construction s'arrête sur
`open /opt/volunteer-apps/secrets/website-meeting.env: permission denied`.
L'image est alors inchangée, le conteneur continue de tourner avec l'ancien
code, et **rien ne le signale** : c'est ainsi que les deux moitiés se sont
désynchronisées. Vérifiez après coup :

    git log -1 --oneline
    docker exec -i website-meeting-app ls /app/utils | head

## Tester une branche avant de merger

    git fetch <url du fork> <branche>
    git checkout FETCH_HEAD
    sudo docker compose up -d --build

Un `FETCH_HEAD` laisse le dépôt en HEAD détaché et ne dit pas d'où il vient :
à faire suivre d'un retour sur `main` dès le merge.

## Ce qui n'est pas ici

Le `Dockerfile` et le fichier de secrets. Le premier est à verser depuis le
serveur (il n'a jamais été dans le dépôt) ; le second ne doit jamais l'être.
