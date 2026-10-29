#!/usr/bin/env python3
"""Weekly refresh of the sitting French elected officials in the CRM.

Orchestrates the existing one-off scripts into a single automatable job, one
chamber at a time, so a new/replacement deputy, senator or government member
appears in the CRM on its own — the same guarantee the eurodéputé sync already
gives (see extract_/insert_eurodeputes.py). Emails of existing rows are kept up
to date daily by sync_emails_from_elus.py; this job maintains the *list*.

Per chamber:
- Assemblée nationale: download the official open-data dump (zip) and unzip it in
  pure Python (no curl/unzip needed), run extract_deputes then insert_deputes.
- Sénat: download the Sénat API JSON, run insert_senateurices.
- Gouvernement: extract_gouvernement (self-fetching) then insert_gouvernement.

Each chamber is isolated: a network hiccup or a source-format change on one never
blocks the others. All inserts are idempotent (new rows added, departed members
reported but never deleted). Exit code is non-zero if any chamber failed, so a
systemd run surfaces the problem while still applying the chambers that worked.

Design notes:
- No third-party dependency (urllib + zipfile from the stdlib).
- The AN legislature number is in the dump URL and changes after a general
  election. It is discovered at run time (see legislature_an) so a dissolution
  does not stall the sync; AN_LEGISLATURE still forces a value by hand.
- The unzipped AN dump is large; it is removed after use to spare container disk.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importruns  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JSON_DIR = os.path.join(ROOT, "json")
DATASET = os.path.join(ROOT, "actual_dataset")
DB = os.path.join(ROOT, "meetings.db")
TIMEOUT = 120

# Role tokens (lower-case) of the officials these lists manage. A person whose
# role contains one of them is subject to the in_office reconciliation below;
# "Député·e européen·ne" contains "député", "Premier·e ministre" contains
# "ministre", "Ministre, Député·e" matches both — all covered.
MANAGED_ROLE_TOKENS = ("député", "sénateur", "ministre", "président")

# --- numéro de législature de l'Assemblée -----------------------------------
# L'URL du dump contient le numéro de législature, qui change à chaque
# dissolution ou élection générale. En dur, il condamne la synchro à échouer
# (404) jusqu'à ce que quelqu'un s'en aperçoive et pose la variable. On va donc
# le chercher : au lancement, on demande à la source si la législature suivante
# existe déjà, et on l'adopte si oui.
AN_LEGISLATURE_PLANCHER = 17  # la plus ancienne qu'on ait jamais vue publiée
# À côté de la base, pas dans actual_dataset/ : ce dossier est suivi par git et
# réécrit à chaque déploiement, alors que meetings.db survit — c'est le seul
# endroit du dépôt dont on sache qu'il persiste. Perdre ce fichier n'est de
# toute façon pas grave : la recherche repartirait du plancher et readopterait
# la législature publiée au passage suivant.
AN_LEGISLATURE_ETAT = os.path.join(ROOT, "an_legislature.json")


def an_zip_url(legislature):
    return (
        f"https://data.assemblee-nationale.fr/static/openData/repository/"
        f"{legislature}/amo/deputes_actifs_mandats_actifs_organes/"
        f"AMO10_deputes_actifs_mandats_actifs_organes.json.zip"
    )


def _existe(url):
    """L'URL est-elle publiée ? Distingue « absente » de « injoignable ».

    Un 404 est une réponse : cette législature n'existe pas (encore). Tout le
    reste — panne, coupure réseau, 500 — n'en est pas une, et doit remonter :
    confondre les deux ferait retomber sur la législature précédente le jour
    d'une panne passagère, donc réimporter 577 ancien·nes député·es comme s'ils
    siégeaient.
    """
    requete = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "pauseia-crm-sync"}
    )
    try:
        urllib.request.urlopen(requete, timeout=TIMEOUT).close()
        return True
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        if err.code in (403, 405, 501):
            # Le serveur refuse la méthode HEAD : on redemande en GET sans lire
            # le corps (urllib ouvre le flux sans le télécharger).
            requete = urllib.request.Request(
                url, headers={"User-Agent": "pauseia-crm-sync"}
            )
            try:
                urllib.request.urlopen(requete, timeout=TIMEOUT).close()
                return True
            except urllib.error.HTTPError as err2:
                if err2.code == 404:
                    return False
                raise
        raise


def _plancher_connu():
    """La plus haute législature déjà utilisée avec succès.

    Mémorisée pour ne JAMAIS redescendre : une fois la 18ᵉ publiée et importée,
    un 404 passager sur son dump ne doit pas faire réimporter la 17ᵉ.
    """
    try:
        with open(AN_LEGISLATURE_ETAT, encoding="utf-8") as f:
            return max(AN_LEGISLATURE_PLANCHER, int(json.load(f)["legislature"]))
    except (OSError, ValueError, KeyError, TypeError):
        return AN_LEGISLATURE_PLANCHER


def _retenir_plancher(legislature):
    os.makedirs(os.path.dirname(AN_LEGISLATURE_ETAT), exist_ok=True)
    with open(AN_LEGISLATURE_ETAT, "w", encoding="utf-8") as f:
        json.dump({"legislature": int(legislature)}, f)


def legislature_an(existe=None, journaliser=True):
    """Le numéro de législature à utiliser, et son URL.

    AN_LEGISLATURE, si elle est posée, gagne et coupe toute recherche : c'est
    la porte de sortie manuelle, elle doit rester prévisible.
    """
    forcee = os.environ.get("AN_LEGISLATURE")
    if forcee:
        if journaliser:
            log(f"  législature {forcee} (forcée par AN_LEGISLATURE)")
        return int(forcee)

    existe = existe or _existe
    plancher = _plancher_connu()
    suivante = plancher + 1
    try:
        publiee = existe(an_zip_url(suivante))
    except Exception as exc:                          # noqa: BLE001
        # Le sondage ne doit JAMAIS faire tomber la chambre. Le 13/10/2026 il
        # l'a fait : l'URL de la 18ᵉ législature, qui n'existe pas, a reçu un
        # « HTTP Error 503: Backend fetch failed » du cache de l'Assemblée au
        # lieu d'un 404 — et le téléchargement du dump de la 17ᵉ, parfaitement
        # disponible, n'a même pas été tenté.
        #
        # L'erreur de conception : j'avais traité « une panne n'est pas une
        # absence » comme une raison d'échouer. Elle est une raison de ne pas
        # ADOPTER une nouvelle législature, ce qui est le statu quo. Ce qu'il
        # fallait protéger — ne jamais redescendre sous la législature déjà
        # utilisée — l'est par le plancher, pas par cette exception.
        if journaliser:
            log(f"  sondage de la législature {suivante} impossible "
                f"({type(exc).__name__}: {exc}) — on garde {plancher}")
        return plancher
    if publiee:
        if journaliser:
            log(f"  législature {suivante} : nouveau dump publié, on l'adopte")
        _retenir_plancher(suivante)
        return suivante
    if journaliser:
        log(f"  législature {plancher}")
    return plancher

SENAT_URL = "https://www.senat.fr/api-senat/senateurs.json"
SENAT_OUT = os.path.join(DATASET, "senateurices_actifs.json")


def log(msg):
    print(msg, flush=True)


# Les sources officielles répondent par intermittence. Constaté le 12/10/2026 :
# le dump de l'Assemblée est passé à 18 h 08 et a répondu « HTTP Error 503:
# Backend fetch failed » à 18 h 09. Abandonner au premier refus, c'est perdre
# une semaine de mise à jour pour une panne de trente secondes.
ESSAIS = 3
ATTENTES = (5, 20)  # secondes avant le 2ᵉ puis le 3ᵉ essai


def _reessayable(exc):
    """Vrai si réessayer a une chance de servir.

    Un 404 ou un 403 ne changera pas dans vingt secondes : réessayer ne fait
    qu'ajouter du bruit et retarder l'alerte. Un 5xx, un timeout ou une coupure
    réseau, si.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def download(url, dest, dormir=time.sleep):
    log(f"  ↓ {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pauseia-crm-sync"})
    for essai in range(1, ESSAIS + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r, \
                    open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return
        except Exception as exc:                      # noqa: BLE001
            if essai == ESSAIS or not _reessayable(exc):
                raise
            attente = ATTENTES[essai - 1]
            log(f"    {type(exc).__name__}: {exc} — nouvel essai dans "
                f"{attente} s ({essai}/{ESSAIS})")
            dormir(attente)


def run_script(name, *args):
    """Run utils/<name> in its own process so a SystemExit guard inside one
    script can't abort the whole orchestration."""
    subprocess.run([sys.executable, os.path.join(HERE, name), *args], check=True)


def sync_deputes():
    zip_path = os.path.join(ROOT, "_amo10.zip")
    try:
        download(an_zip_url(legislature_an()), zip_path)
        # The dump extracts a top-level json/ (acteur/, organe/, …); start clean
        # so a removed deputy's stale file can't linger.
        if os.path.isdir(JSON_DIR):
            shutil.rmtree(JSON_DIR)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(ROOT)
        run_script("extract_deputes.py")
        run_script("insert_deputes.py")
    finally:
        # Reclaim disk: the raw dump is only needed during extraction.
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if os.path.isdir(JSON_DIR):
            shutil.rmtree(JSON_DIR, ignore_errors=True)


def sync_senateurices():
    os.makedirs(DATASET, exist_ok=True)
    download(SENAT_URL, SENAT_OUT)
    run_script("insert_senateurices.py")


def sync_gouvernement():
    run_script("extract_gouvernement.py")  # fetches from the API itself
    run_script("insert_gouvernement.py")


def sync_eurodeputes():
    # extract caches resolved MEPs and only calls the rate-limited detail
    # endpoint for newcomers (see extract_eurodeputes.py).
    run_script("extract_eurodeputes.py")
    run_script("insert_eurodeputes.py")


# --- in_office reconciliation ------------------------------------------------
# Each roster reader returns the set of names *exactly* as the matching insert
# script stores them in persons.name, so membership tests line up.

def _roster_deputes():
    data = json.load(open(os.path.join(DATASET, "deputes_officiel.json"),
                        encoding="utf-8"))
    return {x["depute"]["nom"] for x in data["deputes"]}


def _roster_senateurices():
    data = json.load(open(os.path.join(DATASET, "senateurices_actifs.json"),
                        encoding="utf-8"))
    return {f"{s['prenom']} {s['nom']}".strip() for s in data}


def _roster_gouvernement():
    data = json.load(open(os.path.join(DATASET, "gouvernement.json"),
                        encoding="utf-8"))
    return {m["membre"]["nom_complet"] for m in data["gouvernement"]}


def _roster_eurodeputes():
    data = json.load(open(os.path.join(DATASET, "eurodeputes_fr.json"),
                         encoding="utf-8"))
    return {r["nom_complet"] for r in data}


ROSTERS = {
    "Assemblée nationale": _roster_deputes,
    "Sénat": _roster_senateurices,
    "Gouvernement": _roster_gouvernement,
    "Eurodéputé·es": _roster_eurodeputes,
}


def reconcile_in_office():
    """Mark imported officials in_office=1 when they still sit in any current
    list, 0 otherwise (kept for history). Runs only when all chambers succeeded,
    so a missing list can never wrongly retire the people it should contain.
    Hand-entered rows (added_by/validated_by set) are never touched."""
    current = set()
    for reader in ROSTERS.values():
        current |= reader()

    db = sqlite3.connect(DB)
    # The app writes to this same file. Wait for it rather than failing
    # with "database is locked" on the first contention.
    db.execute("PRAGMA busy_timeout = 30000")
    try:
        rows = db.execute(
            "SELECT id, name, role, in_office FROM persons "
            "WHERE added_by IS NULL AND validated_by IS NULL AND role IS NOT NULL"
        ).fetchall()
        changed = 0
        for pid, name, role, was in rows:
            if not any(tok in role.lower() for tok in MANAGED_ROLE_TOKENS):
                continue
            now_in = 1 if name in current else 0
            if now_in != was:
                db.execute("UPDATE persons SET in_office = ? WHERE id = ?",
                           (now_in, pid))
                changed += 1
        db.commit()
        left = db.execute(
            "SELECT COUNT(*) FROM persons WHERE in_office = 0"
        ).fetchone()[0]
        log(f"\nin_office reconciled: {changed} change(s); {left} person(s) now "
            f"marked no longer in office.")
    finally:
        db.close()


def main():
    # Une ligne dans `import_runs`, comme les imports de courriels : sans elle,
    # une synchro qui échoue toutes les semaines reste parfaitement invisible —
    # les fiches cessent simplement d'être à jour, sans que rien ne le dise.
    db = sqlite3.connect(DB)
    db.execute("PRAGMA busy_timeout = 30000")
    tracker = importruns.track(db, "sync_officials")
    run = tracker.__enter__()
    try:
        # Pas de `else:` ici — un `return` dans le `try` saute la clause `else`
        # et la ligne de suivi resterait « en cours » à chaque succès.
        resultat = _main(run)
        tracker.__exit__(None, None, None)
        return resultat
    except BaseException as exc:                     # noqa: BLE001 — tracé puis relancé
        run.detail = run.detail or f"{type(exc).__name__}: {exc}"
        tracker.__exit__(type(exc), exc, None)
        raise
    finally:
        db.close()


def signaler_echecs(failures, run):
    """Récapitule les causes à la FIN du journal, puis sort en erreur.

    L'alerte Discord ne garde que la queue du journal (voir
    utils/deploy/alerte.sh). Or la cause d'un échec de chambre n'était
    imprimée qu'à l'endroit de l'échec, des centaines de lignes plus haut :
    le 12/10/2026, l'alerte nommait les deux chambres en échec (Assemblée
    nationale, Sénat) sans dire pourquoi. On la réécrit donc en dernier, et
    on la conserve dans `import_runs.detail`.
    """
    log("\nCause de chaque échec :")
    for label, message in failures:
        log(f"  - {label} : {message}")
    run.detail = "Chambres en échec : " + "; ".join(
        f"{label} ({message})" for label, message in failures
    )
    raise SystemExit(
        "Chambers that failed (left untouched, others applied): "
        + ", ".join(label for label, _ in failures)
    )


def _main(run):
    os.makedirs(DATASET, exist_ok=True)  # every extract writes its JSON here
    chambers = [
        ("Assemblée nationale", sync_deputes),
        ("Sénat", sync_senateurices),
        ("Gouvernement", sync_gouvernement),
        ("Eurodéputé·es", sync_eurodeputes),
    ]
    failures = []
    for label, fn in chambers:
        log(f"\n=== {label} ===")
        try:
            fn()
            log(f"=== {label}: OK ===")
        except Exception as exc:  # isolate: one chamber must not sink the others
            failures.append((label, f"{type(exc).__name__}: {exc}"))
            log(f"!!! {label}: FAILED — {type(exc).__name__}: {exc}")

    if failures:
        # Skip reconciliation: without every list the union is incomplete and
        # would retire people who are actually still in office.
        log("\nin_office reconciliation skipped (a chamber failed).")
        signaler_echecs(failures, run)

    reconcile_in_office()
    # A new élu·e arrives without a gender; the datasets just refreshed say it.
    run_script("backfill_genre.py", "--commit")
    run.detail = f"{len(chambers)} chambres rafraîchies."
    log("\nAll chambers refreshed.")


if __name__ == "__main__":
    main()
