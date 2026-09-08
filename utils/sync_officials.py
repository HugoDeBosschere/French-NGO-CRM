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
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JSON_DIR = os.path.join(ROOT, "json")
DATASET = os.path.join(ROOT, "actual_dataset")
TIMEOUT = 120

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


def main():
    chambers = [
        ("Assemblée nationale", sync_deputes),
        ("Sénat", sync_senateurices),
        ("Gouvernement", sync_gouvernement),
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
        raise SystemExit(
            "Chambers that failed (left untouched, others applied): "
            + ", ".join(failures)
        )
    log("\nAll chambers refreshed.")


if __name__ == "__main__":
    main()
