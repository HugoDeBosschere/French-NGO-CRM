#!/usr/bin/env python3
"""One-off maintenance script — NOT part of the web app.

Downloads the canonical orthodox bishops of France from the annuaire the
Assemblée des évêques orthodoxes de France publishes itself, and writes them to
actual_dataset/eveques_orthodoxes.json:

    python3 utils/extract_eveques_orthodoxes.py
    python3 utils/insert_eveques_orthodoxes.py

Why this source: the AEOF is the coordinating body of the orthodox episcopate
in France, and its « Annuaire Regroupé » is its own official membership list —
every bishop with his title, his jurisdiction, his address and usually an email
and a website. It is the orthodox counterpart of the CEF annuaire that
utils/extract_eveques.py reads, and the only other French culte that publishes
its clergy at all: the Consistoire's « corps rabbinique » page and the UBF's
member directory are both rendered client-side with no names in the HTML, and
nothing whatever is published for Islam.

The annuaire is a PDF, and its URL carries the year, so the page at AEOF_PAGE
is scraped for the link rather than the URL being hard-coded — a 2025 edition
will be picked up with no edit.

The PDF is read in pure Python (zlib + the WinAnsi text operators), so this
needs no poppler and no third-party package: it is a small, plainly-generated
document with a text layer, standard fonts and Flate-compressed streams.
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "actual_dataset", "eveques_orthodoxes.json")

AEOF_PAGE = "https://aeof.fr/site/239/eveques.htm"
UA = "Mozilla/5.0 (compatible; PauseIA-CRM/1.0)"

# A bishop's block starts at his name; « Son Eminence » is a métropolite,
# « Son Excellence » an évêque.
NAME_RE = re.compile(r"^(Son\s+Eminence|Son\s+Excellence|Sa\s+Béatitude)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"https?://[^\s,)]+")
# Lines that are address, contact or page furniture rather than jurisdiction.
NOISE_RE = re.compile(
    r"^(TEL|FAX|TEL-FAX|E-?mail|Site|Adresse|Courriel|\d|-+$|France$|"
    r"Annuaire|Siège AEOF|www\.aeof\.fr|Cathédrale Saint Stéphane)", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def find_pdf(page_html):
    """The *newest* annuaire's URL, from the links on the AEOF's évêques page.

    The page keeps older editions alongside the current one — the 2016 annuaire
    is still linked above the 2024 one — so the first match is the wrong one and
    would import an episcopate a decade out of date. The year in the filename is
    what decides.
    """
    found = []
    for m in re.finditer(r'href="([^"]+\.pdf)"', page_html, re.I):
        href = html.unescape(m.group(1))
        if "annuaire" not in urllib.parse.unquote(href).lower():
            continue
        year = re.findall(r"(19|20)(\d\d)", urllib.parse.unquote(href))
        found.append((int("".join(year[-1])) if year else 0,
                      urllib.parse.urljoin(AEOF_PAGE, href)))
    if not found:
        return None
    year, url = max(found)
    print(f"édition retenue : {year or 'année inconnue'}")
    return url


def unescape_pdf_string(raw):
    """A PDF literal string's bytes, with its backslash escapes resolved."""
    out, i = bytearray(), 0
    simple = {0x6e: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
              0x28: 40, 0x29: 41, 0x5c: 92}
    while i < len(raw):
        c = raw[i]
        if c != 0x5c or i + 1 >= len(raw):
            out.append(c)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif 0x30 <= nxt <= 0x37:          # octal escape
            j, digits = i + 1, b""
            while j < len(raw) and len(digits) < 3 and 0x30 <= raw[j] <= 0x37:
                digits += bytes([raw[j]])
                j += 1
            out.append(int(digits, 8) & 0xFF)
            i = j
        else:                               # escaped newline, or a literal char
            out.append(nxt)
            i += 2
    return bytes(out)


# Text-showing operators, plus the Tm/Td that reposition the cursor. A PDF has
# no concept of a line: a page is a bag of positioned runs, so the position is
# the only thing that says what belongs with what.
TOKEN_RE = re.compile(
    rb"(?:([\d.\-]+)\s+([\d.\-]+)\s+(?:Tm|Td|TD))|(\[.*?\]\s*TJ)|(\(.*?\)\s*Tj)",
    re.S)
STRING_RE = re.compile(rb"\((.*?)(?<!\\)\)", re.S)

# The annuaire is laid out in two columns: the bishop and his jurisdiction on
# the left, his address and contact details on the right. Runs must be split by
# x before being joined by y — otherwise « Patriarcat de » and « 1 bd du
# Général Leclerc » land on one line and the jurisdiction is destroyed.
COLUMN_SPLIT_X = 300.0
# Two runs within this many points of the same baseline are the same line.
LINE_TOLERANCE = 3.0


def stream_runs(stream):
    """[(x, y, text)] — every positioned text run of one content stream."""
    runs, x, y = [], 0.0, 0.0
    for m in TOKEN_RE.finditer(stream):
        if m.group(1) is not None:
            x, y = float(m.group(1)), float(m.group(2))
            continue
        text = "".join(
            unescape_pdf_string(raw).decode("cp1252", "replace")
            for raw in STRING_RE.findall(m.group(3) or m.group(4))
        )
        if text.strip():
            runs.append((x, y, text))
    return runs


def runs_to_lines(runs):
    """Positioned runs -> text lines: left column first, each top to bottom.

    Within a column, runs sharing a baseline are joined left to right; the two
    columns are read one after the other, so a jurisdiction is never spliced
    together with the postal address sitting beside it.
    """
    lines = []
    for column in (
        [r for r in runs if r[0] < COLUMN_SPLIT_X],
        [r for r in runs if r[0] >= COLUMN_SPLIT_X],
    ):
        rows = {}
        for x, y, text in column:
            # Bucket by baseline, tolerating the sub-point drift a font change
            # introduces mid-line.
            hit = next((k for k in rows if abs(k - y) <= LINE_TOLERANCE), y)
            rows.setdefault(hit, []).append((x, text))
        for y in sorted(rows, reverse=True):          # PDF y grows upward
            lines.append("".join(t for _, t in sorted(rows[y])))
    return lines


def pdf_lines(data):
    """Every text line of the PDF, page by page, left column then right."""
    lines = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            stream = zlib.decompress(m.group(1))
        except zlib.error:
            continue            # an image (DCTDecode) or another binary stream
        if b"TJ" not in stream and b"Tj" not in stream:
            continue
        lines += runs_to_lines(stream_runs(stream))
    return [re.sub(r"\s+", " ", l).strip() for l in lines if l.strip()]


def blocks(lines):
    """Split the annuaire into one list of lines per bishop."""
    out, current = [], None
    for line in lines:
        if NAME_RE.match(line):
            if current:
                out.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current:
        out.append(current)
    return out


def parse_block(block):
    name_line = block[0]
    body = block[1:]
    email = next((m.group(0) for l in body for m in [EMAIL_RE.search(l)] if m), None)
    site = next((m.group(0) for l in body for m in [URL_RE.search(l)] if m), None)
    # The jurisdiction is what is left once the address, the contact details
    # and the page furniture are taken out. Fragments are joined because the
    # PDF breaks a line wherever the font changes.
    juridiction = " ".join(
        l for l in body
        if not NOISE_RE.match(l)
        and not EMAIL_RE.search(l) and not URL_RE.search(l)
        and not re.match(r"^\d{4,5}\s", l)          # postcode + town
        and not re.match(r"^\d+[,\s]", l)           # street number
        and len(l) > 2                              # stray font-run fragments
    )
    return {
        "nom": name_line,
        "juridiction": re.sub(r"\s+", " ", juridiction).strip(" -,"),
        "email": email,
        "site": site,
    }


def main():
    page = fetch(AEOF_PAGE).decode("utf-8", "replace")
    pdf_url = find_pdf(page)
    if not pdf_url:
        sys.exit(f"Aucun lien vers l'annuaire PDF trouvé sur {AEOF_PAGE} — "
                 f"la page a probablement changé. Rien n'a été écrit.")
    print(f"annuaire : {pdf_url}")

    entries = [parse_block(b) for b in blocks(pdf_lines(fetch(pdf_url)))]
    if len(entries) < 8:
        # The AEOF has had a dozen-odd members for years. Far fewer means the
        # PDF's shape changed, and writing the file anyway would let the
        # importer create a handful of fiches and call the episcopate done.
        sys.exit(f"Seulement {len(entries)} évêque(s) trouvé(s) — le format du "
                 f"PDF a probablement changé. Rien n'a été écrit.")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"source": pdf_url, "eveques": entries}, fh,
                  ensure_ascii=False, indent=2)
    print(f"{len(entries)} évêques orthodoxes écrits dans {OUT}")


if __name__ == "__main__":
    main()
