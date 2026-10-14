#!/usr/bin/env python3
"""Extract currently-sitting deputies from the official Assemblée nationale
open-data dump (json/acteur + json/organe) into the nosdeputes.fr-style
`{"deputes": [{"depute": {...}}, ...]}` format.

A deputy is considered *currently in office* when they hold an ASSEMBLEE-type
mandate whose dateFin is null.
"""
import glob
import json
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTEUR_DIR = os.path.join(ROOT, "json", "acteur")
ORGANE_DIR = os.path.join(ROOT, "json", "organe")
OUT = os.path.join(ROOT, "actual_dataset", "deputes_officiel.json")

# --- organe cache: resolve organeRef -> (libelle, libelleAbrege) -----------
_organe_cache = {}


def organe(ref):
    if ref not in _organe_cache:
        try:
            o = json.load(open(os.path.join(ORGANE_DIR, ref + ".json")))["organe"]
            _organe_cache[ref] = (o.get("libelle"), o.get("libelleAbrege"))
        except FileNotFoundError:
            _organe_cache[ref] = (None, None)
    return _organe_cache[ref]


def as_list(x):
    """Official dump uses a bare dict for single-element collections."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def texte(valeur):
    """Le texte d'un champ du dump, qu'il soit nu ou enveloppé.

    Le dump de l'Assemblée est du XML converti en JSON : un même champ arrive
    tantôt comme chaîne, tantôt comme objet — `{"#text": "Paris"}` quand il
    porte un attribut, `{"@xsi:nil": "true"}` quand il est vide. C'est déjà
    traité pour `uid` ; le 12/10/2026 la synchro est tombée sur `villeNais`
    (« TypeError: unsupported operand type(s) for +=: 'dict' and 'str' »), donc
    la règle est désormais écrite une fois pour toutes.
    """
    if isinstance(valeur, dict):
        valeur = valeur.get("#text", "")
    return (valeur or "").strip() if isinstance(valeur, str) else ""


def active(mandat):
    return mandat.get("dateFin") in (None, "")


def build(acteur):
    ident = acteur["etatCivil"]["ident"]
    naiss = acteur["etatCivil"].get("infoNaissance", {}) or {}
    prenom = texte(ident.get("prenom"))
    nom = texte(ident.get("nom"))
    uid = texte(acteur["uid"])  # le dump enveloppe les ids : {"#text": "PA840235"}

    mandats = as_list(acteur.get("mandats", {}).get("mandat"))

    # The parliamentary mandate that makes them a sitting deputy.
    assemblee = next(
        (m for m in mandats if m.get("typeOrgane") == "ASSEMBLEE" and active(m)),
        None,
    )
    if assemblee is None:
        return None  # not a current deputy

    # Active parliamentary group (GP) and financing party (PARPOL).
    gp = next((m for m in mandats if m.get("typeOrgane") == "GP" and active(m)), None)
    parpol = next(
        (m for m in mandats if m.get("typeOrgane") == "PARPOL" and active(m)), None
    )
    groupe_sigle = None
    if gp:
        groupe_sigle = organe(gp["organes"]["organeRef"])[1]
    parti_ratt = organe(parpol["organes"]["organeRef"])[0] if parpol else None

    lieu = (assemblee.get("election") or {}).get("lieu", {}) or {}
    mandature = assemblee.get("mandature") or {}

    lieu_naissance = texte(naiss.get("villeNais"))
    departement_naissance = texte(naiss.get("depNais"))
    if lieu_naissance and departement_naissance:
        lieu_naissance += f" ({departement_naissance})"

    # Contact info lives in the flat "adresses" collection, split by @xsi:type.
    emails, postales, sites_web = [], [], []
    twitter = None
    for a in as_list(acteur.get("adresses", {}).get("adresse")):
        xtype = a.get("@xsi:type", "")
        valeur = texte(a.get("valElec"))
        if xtype == "AdresseMail_Type" and valeur:
            emails.append({"email": valeur})
        elif xtype == "AdresseSiteWeb_Type" and valeur:
            sites_web.append({"site": valeur})
            if a.get("typeLibelle") == "Twitter":
                twitter = valeur.lstrip("@")
        elif xtype == "AdressePostale_Type":
            champ = lambda cle: texte(a.get(cle))  # noqa: E731
            parts = [
                champ("intitule"),
                " ".join(p for p in [champ("numeroRue"), champ("nomRue")] if p),
                champ("complementAdresse"),
                " ".join(p for p in [champ("codePostal"), champ("ville")] if p),
            ]
            txt = ", ".join(p.strip(",") for p in parts if p)
            if txt:
                postales.append({"adresse": txt})

    # Parliamentary collaborators are nested inside the ASSEMBLEE mandate. The
    # dump wraps them as {"collaborateur": [...]}, but uses a bare list when the
    # wrapper is absent.
    collab_raw = assemblee.get("collaborateurs") or {}
    if isinstance(collab_raw, dict):
        collab_raw = collab_raw.get("collaborateur")
    collaborateurs = []
    for c in as_list(collab_raw):
        if not isinstance(c, dict):
            continue
        full = f"{texte(c.get('prenom'))} {texte(c.get('nom'))}".strip()
        if full:
            collaborateurs.append({"collaborateur": full})

    num_circo = lieu.get("numCirco")
    try:
        num_circo = int(num_circo)
    except (TypeError, ValueError):
        pass

    slug = slugify(f"{prenom} {nom}")
    return {
        "id": None,  # filled after sorting
        "nom": f"{prenom} {nom}".strip(),
        "nom_de_famille": nom,
        "prenom": prenom,
        "sexe": "H" if ident.get("civ") == "M." else "F",
        "date_naissance": naiss.get("dateNais"),
        "lieu_naissance": lieu_naissance,
        "num_deptmt": lieu.get("numDepartement"),
        "nom_circo": lieu.get("departement"),
        "num_circo": num_circo,
        "mandat_debut": assemblee.get("dateDebut"),
        "mandat_fin": None,
        "ancien_depute": 0,
        "groupe_sigle": groupe_sigle,
        "parti_ratt_financier": parti_ratt,
        "sites_web": sites_web,
        "emails": emails,
        "adresses": postales,
        "collaborateurs": collaborateurs,
        "autres_mandats": [],
        "anciens_autres_mandats": [],
        "anciens_mandats": [],
        "profession": (acteur.get("profession") or {}).get("libelleCourant"),
        "place_en_hemicycle": mandature.get("placeHemicycle"),
        "url_an": f"https://www2.assemblee-nationale.fr/deputes/fiche/OMC_{uid}",
        "id_an": uid[2:] if uid.startswith("PA") else uid,
        "slug": slug,
        "url_nosdeputes": f"https://www.nosdeputes.fr/{slug}",
        "url_nosdeputes_api": f"https://www.nosdeputes.fr/{slug}/json",
        "nb_mandats": 0,
        "twitter": twitter,
    }


def main():
    deputes = []
    for f in glob.glob(os.path.join(ACTEUR_DIR, "*.json")):
        acteur = json.load(open(f))["acteur"]
        rec = build(acteur)
        if rec:
            deputes.append(rec)

    # Stable ordering by surname, then assign sequential ids.
    deputes.sort(key=lambda d: (d["nom_de_famille"] or "", d["prenom"] or ""))
    for i, d in enumerate(deputes, 1):
        d["id"] = i

    json.dump({"deputes": [{"depute": d} for d in deputes]},
              open(OUT, "w"), ensure_ascii=False, indent=1)

    # --- report ---
    from collections import Counter
    print(f"Currently-sitting deputies extracted: {len(deputes)}")
    print(f"Written to: {OUT}")
    missing = [d["nom"] for d in deputes if not d["groupe_sigle"]]
    print(f"Without a resolved GP group: {len(missing)}")
    if missing:
        print("  ", missing[:10])
    print("Per group (sigle):")
    for sig, n in Counter(d["groupe_sigle"] for d in deputes).most_common():
        print(f"  {n:4d}  {sig}")


if __name__ == "__main__":
    main()
