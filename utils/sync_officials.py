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
  election; override it with AN_LEGISLATURE when that happens.
- The unzipped AN dump is large; it is removed after use to spare container disk.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
import zipfile

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

AN_LEGISLATURE = os.environ.get("AN_LEGISLATURE", "17")
AN_ZIP_URL = (
    f"https://data.assemblee-nationale.fr/static/openData/repository/"
    f"{AN_LEGISLATURE}/amo/deputes_actifs_mandats_actifs_organes/"
    f"AMO10_deputes_actifs_mandats_actifs_organes.json.zip"
)
SENAT_URL = "https://www.senat.fr/api-senat/senateurs.json"
SENAT_OUT = os.path.join(DATASET, "senateurices_actifs.json")


def log(msg):
    print(msg, flush=True)


def download(url, dest):
    log(f"  ↓ {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pauseia-crm-sync"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def run_script(name):
    """Run utils/<name> in its own process so a SystemExit guard inside one
    script can't abort the whole orchestration."""
    subprocess.run([sys.executable, os.path.join(HERE, name)], check=True)


def sync_deputes():
    zip_path = os.path.join(ROOT, "_amo10.zip")
    try:
        download(AN_ZIP_URL, zip_path)
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
            failures.append(label)
            log(f"!!! {label}: FAILED — {type(exc).__name__}: {exc}")

    if failures:
        # Skip reconciliation: without every list the union is incomplete and
        # would retire people who are actually still in office.
        log("\nin_office reconciliation skipped (a chamber failed).")
        raise SystemExit(
            "Chambers that failed (left untouched, others applied): "
            + ", ".join(failures)
        )

    reconcile_in_office()
    log("\nAll chambers refreshed.")


if __name__ == "__main__":
    main()
