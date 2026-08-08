#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Downloads the members of the sitting government from the *Annuaire de
l'administration* (DILA / service-public.gouv.fr open data) and writes them to
actual_dataset/gouvernement.json:

    python3 utils/extract_gouvernement.py

Why this source: it is the official directory published by the Direction de
l'information légale et administrative (services du Premier ministre), it names
each member with their exact fonction, and most entries carry the JORF decree
that appointed them. gouvernement.fr blocks scripts (403) and the data.gouv.fr
"composition des gouvernements" datasets stop in 2014.

The API is OpenDataSoft Explore v2.1. Note that its `like` operator is
tokenised (it matches whole words, not substrings), so this script pulls every
central-administration / institution record that names someone and filters the
fonctions locally — that is the only reliable way to isolate the ~40 government
members from the ~5 200 named senior civil servants in the same dataset.
"""
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "actual_dataset", "gouvernement.json")

API = (
    "https://api-lannuaire.service-public.gouv.fr/api/explore/v2.1"
    "/catalog/datasets/api-lannuaire-administration/records"
)
# Records that can hold a government member: ministries, plus "Institution"
# for the Présidence de la République.
WHERE = (
    'affectation_personne is not null and '
    '(type_organisme="Administration centrale (ou Ministère)" '
    'or type_organisme="Institution")'
)
SELECT = "nom,type_organisme,affectation_personne,site_internet,adresse_courriel"
PAGE = 100

# Exact `fonction` (accent/case-insensitive, see _key) -> app ROLES label.
FONCTION_TO_ROLE = {
    "president de la republique": "Président·e de la République",
    "premier ministre": "Premier·e ministre",
    "premiere ministre": "Premier·e ministre",
    "ministre": "Ministre",
    "ministre d'etat": "Ministre",
    "garde des sceaux, ministre": "Ministre",
}
# Prefixes, for the long portfolio titles used by ministres délégués and
# secrétaires d'État ("Ministre déléguée auprès du ministre du travail…").
PREFIX_TO_ROLE = [
    ("ministre delegue", "Ministre délégué·e"),
    ("ministre deleguee", "Ministre délégué·e"),
    ("secretaire d'etat", "Secrétaire d'État"),
    ("secretaire d etat", "Secrétaire d'État"),
]

# Must mirror PORTFOLIO_ROLES in app.py: only these roles get a `portefeuille`.
# The Président·e and the Premier·e ministre have no portfolio, and the app's
# form would hide (and then strip) the field for them anyway.
PORTFOLIO_ROLES = {"Ministre", "Ministre délégué·e", "Secrétaire d'État"}

# Name particles kept lowercase, to match how the AN/Sénat imports spell names
# already in `persons` ("Charles de Courson", but "Aurélien Le Coq").
PARTICLES = {"de", "du", "des", "da", "van", "von", "di", "della"}


def _key(s):
    """Lowercase, accent-stripped form used to compare fonctions."""
    s = unicodedata.normalize("NFD", (s or "").strip().lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def role_for(fonction):
    k = _key(fonction)
    if k in FONCTION_TO_ROLE:
        return FONCTION_TO_ROLE[k]
    for prefix, role in PREFIX_TO_ROLE:
        if k.startswith(prefix):
            return role
    return None


def tidy_name(surname):
    """"DE COURSON" -> "de Courson", "D'INTORNI" -> "D'Intorni".

    The annuaire stores surnames in caps; `persons` holds them in normal case.
    """
    words = []
    for i, word in enumerate(surname.strip().split()):
        low = word.lower()
        if i > 0 and low in PARTICLES:
            words.append(low)
            continue
        # Capitalise after hyphens and apostrophes too (Jean-Noël, D'Intorni).
        words.append(re.sub(r"(^|[-'’])(\w)", lambda m: m.group(1) + m.group(2).upper(), low))
    return " ".join(words)


def fetch_all():
    rows, offset = [], 0
    while True:
        query = urllib.parse.urlencode(
            {"where": WHERE, "select": SELECT, "limit": PAGE, "offset": offset}
        )
        with urllib.request.urlopen(f"{API}?{query}", timeout=60) as resp:
            page = json.load(resp)
        rows += page["results"]
        offset += PAGE
        if offset >= page["total_count"]:
            return rows


def first_value(raw):
    """The annuaire stores several fields as a JSON *string* of a list."""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    for item in items:
        value = (item.get("valeur") or "").strip() if isinstance(item, dict) else ""
        if value:
            return value
    return None


def main():
    membres, skipped = [], 0
    for record in fetch_all():
        for affectation in json.loads(record["affectation_personne"] or "[]"):
            fonction = (affectation.get("fonction") or "").strip()
            role = role_for(fonction)
            if role is None:
                skipped += 1
                continue
            personne = affectation.get("personne") or {}
            prenom = (personne.get("prenom") or "").strip()
            nom = tidy_name(personne.get("nom") or "")
            jorf = (personne.get("texte_reference") or [{}])[0]
            membres.append(
                {
                    "membre": {
                        "nom_complet": f"{prenom} {nom}".strip(),
                        "prenom": prenom,
                        "nom": nom,
                        "civilite": personne.get("civilite") or None,
                        "role": role,
                        "fonction": fonction,
                        "portefeuille": record["nom"] if role in PORTFOLIO_ROLES else None,
                        "email": first_value(record.get("adresse_courriel")),
                        "site": first_value(record.get("site_internet")),
                        "jorf": jorf.get("libelle") or None,
                        "jorf_url": jorf.get("valeur") or None,
                    }
                }
            )

    # A member can hold two portfolios (e.g. porte-parole *and* a ministry), so
    # they show up under two records. Keep one entry per person, joining the
    # portfolios — `persons.name` is the key the insert is idempotent on, so a
    # second entry would otherwise be silently dropped.
    merged = {}
    for entry in membres:
        m = entry["membre"]
        seen = merged.get(m["nom_complet"])
        if seen is None:
            merged[m["nom_complet"]] = entry
            continue
        for field in ("fonction", "portefeuille"):
            if not m[field]:
                continue
            if not seen["membre"][field]:
                seen["membre"][field] = m[field]
            elif m[field] not in seen["membre"][field]:
                seen["membre"][field] += f" ; {m[field]}"
        for field in ("email", "site", "jorf", "jorf_url"):
            seen["membre"][field] = seen["membre"][field] or m[field]
    membres = sorted(
        merged.values(), key=lambda e: (e["membre"]["role"], e["membre"]["nom"])
    )
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"gouvernement": membres}, fh, ensure_ascii=False, indent=2)

    print(f"Wrote {len(membres)} government members to {OUT}")
    print(f"(ignored {skipped} other named civil servants in the same records)")
    by_role = {}
    for m in membres:
        by_role.setdefault(m["membre"]["role"], []).append(m["membre"]["nom_complet"])
    for role, names in sorted(by_role.items()):
        print(f"\n{role} ({len(names)}):")
        for name in names:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
