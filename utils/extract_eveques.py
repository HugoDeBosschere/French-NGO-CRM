#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Downloads the French bishops from the *Guide de l'Église catholique en France*,
the annuaire the Conférence des évêques de France publishes itself, and writes
them to actual_dataset/eveques.json:

    python3 utils/extract_eveques.py
    python3 utils/insert_eveques.py

Why this source: it is the CEF's own directory, it is regenerated continuously
(nominations land within days), and it gives each person's exact title and see
— « Mgr Marc Aillet / Évêque de Bayonne » — which is precisely the fonction and
the « Territoire assigné » the CRM wants. It is also the only French culte with
a machine-readable official directory: nothing equivalent is published for
Islam (the CFCM lapsed and FORIF's membership was never released), and the
protestant, orthodox and jewish annuaires stop at the level of organisations.

The page is plain server-rendered HTML with one `bloc-adel-list-item` container
per person, holding a `bloc-adel-list-title` (the name, linked to their fiche)
and a `bloc-adel-list-subtitle` (the title and the see). Each container is
parsed on its own rather than by a regex running across the document: a pattern
that spans containers would silently pair one person's name with the next
person's see, and nothing in the output would look wrong.
"""
import html
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "actual_dataset", "eveques.json")

URL = ("https://eglise.catholique.fr/guide-eglise-catholique-france"
       "/eveques-dioceses-france/liste-eveques-de-france/")
# The site answers 403 to the default urllib agent.
UA = "Mozilla/5.0 (compatible; PauseIA-CRM/1.0)"

ITEM = "bloc-adel-list-item"
NAME_RE = re.compile(r'bloc-adel-list-link"[^>]*>(.*?)</a>', re.S)
LINK_RE = re.compile(r'bloc-adel-list-link"[^>]*?href="([^"]+)"', re.S)
HREF_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*class="bloc-adel-list-link"', re.S)
SUBTITLE_RE = re.compile(r'bloc-adel-list-subtitle">(.*?)</h4>', re.S)


def text(fragment):
    """Inner text of an HTML fragment: tags out, entities resolved."""
    return " ".join(
        html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()
    )


def parse(page):
    """[{nom, titre, profil}] — one entry per person, in page order."""
    out = []
    for chunk in page.split(ITEM)[1:]:
        name = NAME_RE.search(chunk)
        if not name:
            continue
        subtitle = SUBTITLE_RE.search(chunk)
        href = HREF_RE.search(chunk)
        out.append({
            "nom": text(name.group(1)),
            "titre": text(subtitle.group(1)) if subtitle else "",
            "profil": html.unescape(href.group(1)) if href else None,
        })
    return out


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        page = resp.read().decode("utf-8", "replace")

    entries = parse(page)
    if len(entries) < 80:
        # The annuaire has held 110-160 entries for years. A sudden collapse
        # means the markup changed, and writing the file anyway would let
        # insert_eveques.py quietly import a fraction of the episcopate.
        sys.exit(f"Seulement {len(entries)} évêques trouvés — le balisage de "
                 f"la page a probablement changé. Rien n'a été écrit.")
    missing = [e["nom"] for e in entries if not e["titre"]]
    if missing:
        print(f"! {len(missing)} entrée(s) sans titre : {missing[:5]}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"source": URL, "eveques": entries}, fh,
                  ensure_ascii=False, indent=2)
    print(f"{len(entries)} évêques écrits dans {OUT}")


if __name__ == "__main__":
    main()
