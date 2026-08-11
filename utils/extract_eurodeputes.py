#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Downloads the French members of the European Parliament from the Parliament's
official open-data API and writes them to actual_dataset/eurodeputes_fr.json:

    python3 utils/extract_eurodeputes.py

Re-run it after a European election (or after a mid-term replacement) and it
picks up the new intake on its own: it asks for `meps/show-current`, which is
the Parliament's list of members *sitting today*, so there is no term number or
seat count baked into this file. Insert the result with
utils/insert_eurodeputes.py.

Why this source: data.europarl.europa.eu is the Parliament's own open-data
service, and unlike the Sénat it publishes each member's email address
directly (`hasEmail`), so nothing here is guessed from a naming convention. It
also publishes gender (`hasGender`), which the spreadsheet export uses.

Two calls per run: one list request, then one detail request per MEP (81 for
France as of the 2024-2029 term) to pick up the email and gender the list
endpoint omits. Be patient and don't hammer it — see PAUSE below.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "actual_dataset", "eurodeputes_fr.json")

API = "https://data.europarl.europa.eu/api/v2"
COUNTRY = "FR"
PAUSE = 0.2  # seconds between detail calls — courtesy, the API is not rate-limited
TIMEOUT = 40

# EU vocabulary URIs -> the value we store. Anything unlisted is left empty
# rather than guessed; the summary at the end counts those.
SEX = {
    "http://publications.europa.eu/resource/authority/human-sex/MALE": "Homme",
    "http://publications.europa.eu/resource/authority/human-sex/FEMALE": "Femme",
}


def get(path, **params):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/ld+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        if r.status == 204:  # the API answers "no match" with an empty body
            return {"data": []}
        return json.loads(r.read().decode("utf-8"))


def main():
    listing = get("meps/show-current", **{"country-of-representation": COUNTRY})
    meps = listing.get("data", [])
    if not meps:
        raise SystemExit(
            f"The API returned no sitting MEP for country-of-representation="
            f"{COUNTRY}. It expects a 2-letter code (FR, not FRA); if that is "
            f"still right, check whether the endpoint has changed."
        )
    print(f"{len(meps)} député·es européen·nes français·es — fetching details…")

    out, no_email, no_gender = [], [], []
    for i, m in enumerate(meps, start=1):
        ident = m["identifier"]
        try:
            detail = get(f"meps/{ident}")["data"][0]
        except (urllib.error.URLError, KeyError, IndexError) as exc:
            # One unreachable member must not cost us the other 80.
            print(f"  ! {m.get('label', ident)}: detail unavailable ({exc})")
            detail = {}

        email = (detail.get("hasEmail") or "").removeprefix("mailto:") or None
        genre = SEX.get(detail.get("hasGender"), "")
        prenom = detail.get("givenName") or m.get("givenName") or ""
        nom = detail.get("familyName") or m.get("familyName") or ""

        if not email:
            no_email.append(f"{prenom} {nom}")
        if not genre:
            no_gender.append(f"{prenom} {nom}")

        out.append(
            {
                "identifier": ident,
                "prenom": prenom,
                "nom": nom,
                "nom_complet": f"{prenom} {nom}".strip(),
                "groupe": m.get("api:political-group"),
                "genre": genre,
                "email": email,
                "date_naissance": detail.get("bday"),
                "url": f"https://www.europarl.europa.eu/meps/fr/{ident}",
            }
        )
        if i % 20 == 0:
            print(f"  …{i}/{len(meps)}")
        time.sleep(PAUSE)

    out.sort(key=lambda p: (p["nom"], p["prenom"]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print(f"\nWrote {OUT} — {len(out)} MEPs")
    if no_email:
        print(f"  Sans email ({len(no_email)}): {', '.join(no_email)}")
    if no_gender:
        print(f"  Sans genre ({len(no_gender)}): {', '.join(no_gender)}")


if __name__ == "__main__":
    main()
