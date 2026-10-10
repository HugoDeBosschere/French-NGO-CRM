#!/usr/bin/env python3
"""Règles communes aux listes d'élu·es (sénateurices, députées, eurodéputées).

Les trois scripts d'insertion partageaient deux défauts, l'un et l'autre
silencieux, découverts en octobre 2026 après le renouvellement sénatorial du
27 septembre.

1. UN GROUPE INCONNU FAISAIT DISPARAÎTRE LA PERSONNE.

   Le code lisait le libellé court du groupe, le cherchait dans sa table de
   correspondance, et sur un échec faisait `continue` : la personne n'était
   jamais insérée. Une ligne de journal, rien de plus, et le travail se
   déclarait réussi.

   Or l'API du Sénat renvoie « Aucun » pour un·e sénateur·ice fraîchement élu·e
   qui n'a pas encore rejoint de groupe — un état parfaitement normal pendant
   les semaines qui suivent un scrutin. Au 5 octobre 2026 ils étaient 58 dans ce
   cas : 290 traités + 58 abandonnés = 348, soit exactement l'effectif du Sénat.
   Le CRM n'en connaissait que 289.

   Un élu sans groupe déclaré est donc désormais inséré, avec le groupe
   d'attente de sa chambre. Il apparaît dans le CRM, on peut lui écrire, et son
   groupe se corrigera de lui-même au passage suivant. Seul un libellé
   VRAIMENT inconnu (ni dans la table, ni vide) reste une anomalie.

2. UN CHANGEMENT DE MANDAT N'ÉTAIT JAMAIS PRIS EN COMPTE.

   Le test d'existence portait sur le seul nom, toutes fiches confondues :

       existing = {r[0] for r in db.execute("SELECT name FROM persons")}
       if name in existing: skipped += 1; continue

   Quelqu'un déjà fiché comme président de région ou maire, puis élu au Sénat,
   était donc compté « déjà présent » et gardait son ancien rôle pour toujours.
   Dans la liste du 5 octobre : Renaud Muselier, François Rebsamen, Édouard
   Fritch, Rodolphe Alexandre…

   `insert_gouvernement.py` savait déjà traiter ce cas, avec `merge_roles` : les
   rôles sont une liste séparée par des virgules (« Ministre, Député·e »). Cette
   logique est reprise ici plutôt que recopiée une quatrième fois, et les trois
   listes s'en servent enfin.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE_SEP = ", "

#: Groupe donné à un élu dont la liste officielle ne déclare pas le groupe.
#: Doit exister à l'identique dans POLITICAL_GROUPS d'app.py, sinon la
#: vérification au démarrage de chaque script refuse de tourner.
GROUPE_ATTENTE = {
    "Assemblée nationale": "Sans groupe déclaré (Assemblée nationale)",
    "Sénat": "Sans groupe déclaré (Sénat)",
    "Parlement européen": "Sans groupe déclaré (Parlement européen)",
}

#: Libellés que les sources emploient pour « pas encore de groupe ». Comparés
#: sans casse ni espaces ; `None` et la chaîne vide comptent aussi.
LIBELLES_SANS_GROUPE = {"aucun", "aucune", "sans groupe", "non renseigné", "nr", "-"}


def app_constant(name):
    """Lit une constante de haut niveau d'app.py sans l'importer (l'app tire
    des dépendances lourdes). Garde-fou : un libellé renommé dans app.py ne peut
    pas produire en silence des fiches que le formulaire ne saurait pas éditer."""
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} introuvable dans app.py")


def merge_roles(existing, new_role, order):
    """Ajoute `new_role` à une liste de rôles séparés par des virgules."""
    roles = {r.strip() for r in (existing or "").split(",") if r.strip()}
    roles.add(new_role)
    known = [r for r in order if r in roles]
    # Conserve ce qui n'est pas dans la liste de référence (saisi à la main
    # avant une évolution des rôles) : on n'efface jamais le travail d'un humain.
    return ROLE_SEP.join(known + sorted(roles - set(order)))


def sans_groupe(libelle):
    """Vrai si la source dit « cette personne n'a pas (encore) de groupe ».

    Le `strip()` avant le test d'absence n'est pas cosmétique : un champ rempli
    d'espaces est vide pour un humain, et sans lui il passait pour un libellé
    inconnu, donc pour une anomalie qui aurait fait échouer tout l'import.
    """
    nettoye = ("" if libelle is None else str(libelle)).strip()
    return not nettoye or nettoye.lower() in LIBELLES_SANS_GROUPE


def resoudre_groupe(libelle, correspondance, chambre):
    """(groupe, anomalie) pour un libellé de groupe venu d'une source officielle.

    `anomalie` vaut None quand tout va bien, sinon le libellé fautif : un
    libellé qu'on ne connaît pas n'est PAS un élu sans groupe, c'est le signe
    que la source a changé de vocabulaire et que la table est à compléter.
    """
    groupe = correspondance.get(libelle)
    if groupe is not None:
        return groupe, None
    if sans_groupe(libelle):
        return GROUPE_ATTENTE[chambre], None
    return GROUPE_ATTENTE[chambre], libelle


def verifier_groupes(chambre, correspondance):
    """Refuse de tourner si une cible n'existe pas dans app.py. Appelé au
    démarrage : mieux vaut ne rien faire que remplir la base de valeurs que
    l'interface ne sait pas afficher."""
    valides = set(app_constant("POLITICAL_GROUPS")[chambre])
    cibles = set(correspondance.values()) | {GROUPE_ATTENTE[chambre]}
    manquantes = sorted(cibles - valides)
    if manquantes:
        raise SystemExit(
            f"Groupes absents de POLITICAL_GROUPS[{chambre!r}] dans app.py : "
            + repr(manquantes)
        )
