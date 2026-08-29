#!/usr/bin/env python3
"""Fill persons.email in the CRM from the sending tool's authoritative elus.json.

Why: the "Écrire à mes élus" site (repo pauseai-france) generates
`src/lib/data/elus.json` from official open data — the data.gouv Assemblée
nationale dataset for deputies and the Sénat ODSEN dataset for senators — with a
cross-checked confidence level per address. That file is the exact source of the
address the tool puts in the mail's `To:` header. Matching the campaign-mail
importer against the same addresses therefore makes matching reliable.

The CRM seeds `persons.email` statically at import time and only deputies were
filled; senators (and a few others) have none, so mails to senators never match.
This script backfills them from elus.json — the same, authoritative source — so
CRM addresses line up with what the tool actually sends.

Usage:
    # From a local checkout of the sending tool:
    python3 utils/sync_emails_from_elus.py \
        --elus ../pauseai-france/src/lib/data/elus.json --dry-run

    # Or fetch the committed file straight from GitHub (needs network):
    python3 utils/sync_emails_from_elus.py --dry-run

    # Apply (default: only fills empty emails, confidence >= high):
    python3 utils/sync_emails_from_elus.py --elus <path>

Options:
    --elus PATH          local elus.json (else fetched from --elus-url)
    --elus-url URL       raw URL to fetch when --elus is omitted
    --min-confidence C   high (default) | medium | low — lowest confidence to trust
    --overwrite          also replace an existing email that differs (default: no)
    --dry-run            report what would change without writing
    --db PATH            meetings.db (default: <repo>/meetings.db or IMAP_DB_PATH)

Matching is by normalised full name (accent/case/separator-insensitive). Both
datasets build the name as "Prénom Nom" from the same official sources, so this
is exact in practice; any person not matched to a single elus entry is reported,
never guessed.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("IMAP_DB_PATH", os.path.join(ROOT, "meetings.db"))
DEFAULT_ELUS_URL = (
    "https://raw.githubusercontent.com/Romain-Deleglise/pauseai-france/"
    "main/src/lib/data/elus.json"
)
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def norm_name(name):
    """Accent-strip, lower-case, collapse separators — for robust name matching."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", name or "")
        if not unicodedata.combining(c)
    )
    return re.sub(r"[\s'-]+", " ", stripped).strip().lower()


def load_elus(path, url):
    if path:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    log(f"Fetching elus.json from {url}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def log(msg):
    print(msg, flush=True)


def build_email_index(elus, min_rank):
    """normalised name -> (email, confidence). Skips blanks / low confidence.

    A name that maps to two different emails is dropped (ambiguous, never guess).
    """
    candidates = {}
    for elu in elus.get("deputes", []) + elus.get("senateurs", []):
        email = (elu.get("email") or "").strip().lower()
        conf = elu.get("emailConfidence", "none")
        if not email or CONFIDENCE_RANK.get(conf, 0) < min_rank:
            continue
        key = norm_name(elu.get("nom", ""))
        if not key:
            continue
        candidates.setdefault(key, {})[email] = conf

    index, dropped = {}, 0
    for key, emails in candidates.items():
        if len(emails) == 1:
            (email, conf), = emails.items()
            index[key] = (email, conf)
        else:
            dropped += 1  # same name, conflicting addresses -> ambiguous
    if dropped:
        log(f"Note: {dropped} name(s) had conflicting addresses and were skipped.")
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--elus", help="path to a local elus.json")
    parser.add_argument("--elus-url", default=DEFAULT_ELUS_URL,
                        help="raw URL to fetch when --elus is omitted")
    parser.add_argument("--min-confidence", choices=["high", "medium", "low"],
                        default="high", help="lowest confidence to trust")
    parser.add_argument("--overwrite", action="store_true",
                        help="also replace an existing email that differs")
    parser.add_argument("--dry-run", action="store_true",
                        help="report changes without writing")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to meetings.db")
    args = parser.parse_args()

    min_rank = CONFIDENCE_RANK[args.min_confidence]
    elus = load_elus(args.elus, args.elus_url)
    index = build_email_index(elus, min_rank)
    log(f"elus.json: {len(index)} usable address(es) at confidence "
        f">= {args.min_confidence}.")

    db = sqlite3.connect(args.db)
    persons = db.execute("SELECT id, name, email FROM persons").fetchall()

    filled, updated, already_ok, no_match, conflict_kept = 0, 0, 0, 0, 0
    unmatched = []
    for pid, name, current in persons:
        hit = index.get(norm_name(name))
        if not hit:
            # Only worth reporting people who lack an email and weren't matched.
            if not (current or "").strip():
                no_match += 1
                unmatched.append(name)
            continue
        email, conf = hit
        current = (current or "").strip()
        if not current:
            action = "fill"
        elif current.lower() == email:
            already_ok += 1
            continue
        elif args.overwrite:
            action = "update"
        else:
            conflict_kept += 1
            log(f"  [kept] {name}: CRM has {current!r}, elus.json has "
                f"{email!r} ({conf}) — use --overwrite to replace")
            continue

        if args.dry_run:
            log(f"  [dry-run] {action} {name} -> {email} ({conf})")
        else:
            db.execute("UPDATE persons SET email = ? WHERE id = ?", (email, pid))
        filled += action == "fill"
        updated += action == "update"

    if not args.dry_run:
        db.commit()
    db.close()

    log(f"\nDone. Filled: {filled} | updated: {updated} | already correct: "
        f"{already_ok} | kept (conflict): {conflict_kept} | no match & still "
        f"without email: {no_match}.")
    if unmatched:
        log("Persons still without an email (no elus.json match) — expected for "
            "government members, public figures, or senators who publish none:")
        for name in sorted(unmatched):
            log(f"  - {name}")


if __name__ == "__main__":
    main()
