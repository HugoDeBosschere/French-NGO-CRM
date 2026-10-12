#!/usr/bin/env python3
"""Mesurer la convention d'adresses du Sénat contre les pages senat.fr.

Le Sénat ne publie aucune adresse dans son API : celles de ses 348 membres sont
calculées depuis le nom (voir senat_email dans insert_senateurices.py). La
règle avait été mesurée sur un échantillon de 45 sénateur·ices — 43
correspondances, 0 erreur, 2 sans adresse publiée — mais avant le
renouvellement de septembre 2026 et ses 58 arrivée·es.

Ce script refait la mesure sur la liste complète, en lisant la page personnelle
de chaque sénateur·ice. Il ne modifie RIEN dans la base : il imprime un
rapport. À lancer depuis le serveur, qui a accès à senat.fr :

    docker exec -i website-meeting-app python3 utils/verifier_emails_senat.py
    docker exec -i website-meeting-app python3 utils/verifier_emails_senat.py --max 40

Trois issues par personne :
- « conforme »   : la page publie exactement l'adresse déduite ;
- « divergente » : la page publie une AUTRE adresse — c'est celle-là qui est
  bonne, et la ligne est à reporter dans EMAIL_OVERRIDES ;
- « muette »     : la page ne publie pas d'adresse, la déduction reste le seul
  recours et peut rebondir.

Si AUCUNE page ne publie d'adresse en clair, le script le dit et conclut que la
vérification n'est pas concluante — plutôt que de laisser croire que la
convention est fausse à 100 %.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from insert_senateurices import SRC, senat_email  # noqa: E402

BASE = "https://www.senat.fr"
TIMEOUT = 30
PAUSE = 0.3  # on reste poli : 348 pages, pas de rafale
ADRESSE = re.compile(r"[\w.+-]+@senat\.fr", re.I)
# Les adresses de service, qui ne sont pas celles d'un·e sénateur·ice.
GENERIQUES = {"webmaster@senat.fr", "contact@senat.fr", "presse@senat.fr"}


def page(url):
    requete = urllib.request.Request(
        url, headers={"User-Agent": "pauseia-crm-verification"}
    )
    with urllib.request.urlopen(requete, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def adresses_publiees(html):
    """Les adresses @senat.fr d'une page, hors adresses de service."""
    trouvees = []
    for brute in ADRESSE.findall(html):
        propre = brute.lower()
        if propre not in GENERIQUES and propre not in trouvees:
            trouvees.append(propre)
    return trouvees


def verifier(senateurices, lire=page, pause=PAUSE):
    """(conformes, divergentes, muettes, injoignables) — sans toucher la base."""
    conformes, divergentes, muettes, injoignables = [], [], [], []
    for s in senateurices:
        nom = f"{s['prenom']} {s['nom']}".strip()
        deduite = (senat_email(s["prenom"], s["nom"]) or "").lower()
        chemin = s.get("url") or ""
        url = chemin if chemin.startswith("http") else BASE + chemin
        try:
            publiees = adresses_publiees(lire(url))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            injoignables.append((nom, f"{type(exc).__name__}: {exc}"))
            continue
        finally:
            if pause:
                time.sleep(pause)
        if not publiees:
            muettes.append((nom, deduite))
        elif deduite in publiees:
            conformes.append(nom)
        else:
            divergentes.append((nom, deduite, publiees[0]))
    return conformes, divergentes, muettes, injoignables


def rapport(conformes, divergentes, muettes, injoignables):
    lignes = []
    total = len(conformes) + len(divergentes) + len(muettes)
    lignes.append(
        f"{len(conformes)} conforme(s)  |  {len(divergentes)} divergente(s)  |  "
        f"{len(muettes)} muette(s)  |  {len(injoignables)} injoignable(s)"
    )
    if divergentes:
        lignes.append(
            "\nÀ reporter dans EMAIL_OVERRIDES (la page senat.fr fait foi) :"
        )
        for nom, deduite, publiee in divergentes:
            lignes.append(f'    "{nom}": "{publiee}",   # déduite à tort : {deduite}')
    if total and not conformes and not divergentes:
        lignes.append(
            "\nAucune page ne publie d'adresse en clair : la vérification n'est "
            "PAS concluante. Elle ne dit pas que la convention est fausse, elle "
            "dit que senat.fr ne permet plus de la contrôler ainsi."
        )
    elif conformes or divergentes:
        verifiables = len(conformes) + len(divergentes)
        lignes.append(
            f"\nSur les {verifiables} adresse(s) vérifiables, la convention en "
            f"donne {len(conformes)} juste(s), soit "
            f"{100 * len(conformes) // verifiables} %."
        )
    if muettes:
        lignes.append(
            f"\n{len(muettes)} sénateur·ice(s) ne publient aucune adresse : la "
            "déduction reste le seul recours, et peut rebondir. Exemples : "
            + ", ".join(nom for nom, _ in muettes[:5])
            + (" …" if len(muettes) > 5 else "")
        )
    if injoignables:
        lignes.append("\nPages injoignables :")
        for nom, erreur in injoignables[:10]:
            lignes.append(f"    {nom} : {erreur}")
    return "\n".join(lignes)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max", type=int, default=0,
                    help="n'en vérifier que N (échantillon, pour aller vite)")
    args = ap.parse_args()

    senateurices = json.load(open(SRC, encoding="utf-8"))
    if args.max:
        senateurices = senateurices[: args.max]
    print(f"Vérification de {len(senateurices)} page(s) sur senat.fr…\n",
          flush=True)
    print(rapport(*verifier(senateurices)))


if __name__ == "__main__":
    main()
