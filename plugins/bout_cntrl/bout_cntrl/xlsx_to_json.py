#!/usr/bin/env python3
"""
pdf_to_json_v2.py

Converts the "OLYMPIC HOPES ... RING A" style schedule PDF (two-column
RED CORNER / BLUE CORNER layout, e.g. FRIDAY_ring_A.pdf) into the same
bouts JSON format used by pdf_to_json.py.

Source layout
-------------
Each bout is printed as a block of (up to) 3 text lines:

    <Red Name>          <Age> <Girls|Boys>     <Blue Name>          3 x <round-min> min.
    <bout number>
    <Red Country>        <weight>kg            <Blue Country>       <start time>

Category rules
--------------
    Age word is already spelled out: U15 / U17 / U19 (kept as-is).
    Gender word -> Men/Woman:
        Boys  -> Men
        Girls -> Woman

    So "U15 Girls" -> "U15 Woman", "U17 Boys" -> "U17 Men", etc.

Clock (seconds)
---------------
Parsed straight from the "3 x <round-min> min." text (the "3 x" round
count is ignored -- clock is the per-round time):
        1,5 min -> 90   (1:30)
        2 min   -> 120  (2:00)
        3 min   -> 180  (3:00)
This lines up with the U15/U17/U19 clock rule from the previous
converter, but is read directly from the PDF for robustness.

Country / flag
---------------
This PDF prints full country names (e.g. "Slovakia") rather than 3-letter
codes. COUNTRY_TABLE below maps each name to a 3-letter code (for the
"country" field, to stay consistent with the established JSON format)
and a lowercase flag code (for the "flag" field). Extend this table if a
new country shows up in future schedules.

Names
-----
Unlike the previous PDF (which printed "SURNAME Firstname" consistently),
this format prints names in mixed, inconsistent order/case (e.g. "Karin
Blašková" vs "Lakatos Zsolt" vs "Apollonio marika"), so there's no
reliable way to detect which token is the surname. Names are therefore
kept exactly as printed in the PDF (just whitespace-trimmed) rather than
reformatted.

Usage
-----
    python pdf_to_json_v2.py input.pdf output.json
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

GENDER_MAP = {
    "Boys": "Men",
    "Girls": "Woman",
}

# country name (as printed in the PDF, case-insensitive) -> (3-letter code, flag)
COUNTRY_TABLE = {
    "slovakia": ("SVK", "sk"),
    "czechia": ("CZE", "cz"),
    "italy": ("ITA", "it"),
    "england": ("ENG", "gb"),
    "poland": ("POL", "pl"),
    "lithuania": ("LTU", "lt"),
    "croatia": ("CRO", "hr"),
    "hungary": ("HUN", "hu"),
    "germany": ("GER", "de"),
    "moldova": ("MDA", "md"),
    "ukraine": ("UKR", "ua"),
    "france": ("FRA", "fr"),
    "spain": ("ESP", "es"),
    "portugal": ("POR", "pt"),
    "netherlands": ("NED", "nl"),
    "belgium": ("BEL", "be"),
    "switzerland": ("SUI", "ch"),
    "austria": ("AUT", "at"),
    "sweden": ("SWE", "se"),
    "norway": ("NOR", "no"),
    "denmark": ("DEN", "dk"),
    "finland": ("FIN", "fi"),
    "ireland": ("IRL", "ie"),
    "romania": ("ROU", "ro"),
    "bulgaria": ("BUL", "bg"),
    "serbia": ("SRB", "rs"),
    "turkey": ("TUR", "tr"),
    "usa": ("USA", "us"),
    "united states": ("USA", "us"),
    "great britain": ("GBR", "gb"),
    "scotland": ("SCO", "gb"),
    "wales": ("WAL", "gb"),
}

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Line 1 of a bout block: "<red name>   <age> <Girls|Boys>   <blue name>   3 x N[,N] min."
BOUT_HEADER_RE = re.compile(
    r"""^(?P<red>.+?)\s{2,}
        (?P<age>U1[579])\s+(?P<gender>Girls|Boys)\s{2,}
        (?P<blue>.+?)\s{2,}
        (?P<clock>3\s*x\s*[\d,]+\s*min\.?)\s*$
    """,
    re.VERBOSE,
)

# Line 3 of a bout block: "<red country>   <weight>kg   <blue country>   <hh:mm>"
COUNTRY_LINE_RE = re.compile(
    r"""^(?P<red_country>.+?)\s{2,}
        (?P<weight>\d+\+?)\s*kg\s{2,}
        (?P<blue_country>.+?)\s{2,}
        (?P<time>\d{1,2}:\d{2})\s*$
    """,
    re.VERBOSE,
)


def extract_text(pdf_path: str) -> str:
    """
    Extract text using poppler's `pdftotext -layout`.

    This format's columns (bout number / red side / category / blue side /
    clock, then country / weight / country / time on the next line) rely on
    precise horizontal alignment. pdftotext -layout reproduces that
    alignment far more reliably than pdfplumber's word-clustering, which
    was observed to occasionally merge or misplace the bout-number column.
    """
    with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
        subprocess.run(["pdftotext", "-layout", pdf_path, tmp.name], check=True)
        with open(tmp.name, "r", encoding="utf-8") as f:
            return f.read()


def flag_and_code(country_name: str):
    key = country_name.strip().lower()
    if key in COUNTRY_TABLE:
        return COUNTRY_TABLE[key]
    # Fallback: use the name itself as "code" and first two letters as flag.
    print(f"Warning: unknown country '{country_name}', add it to COUNTRY_TABLE", file=sys.stderr)
    return country_name.strip().upper()[:3], country_name.strip().lower()[:2]


def clock_seconds(clock_text: str) -> int:
    # e.g. "3 x 1,5 min." -> per-round minutes = 1,5 -> 1.5 -> 90 sec
    m = re.search(r"x\s*([\d,]+)\s*min", clock_text)
    if not m:
        raise ValueError(f"Could not parse clock text: '{clock_text}'")
    minutes = float(m.group(1).replace(",", "."))
    return round(minutes * 60)


def parse_bouts(text: str):
    lines = text.split("\n")
    bouts = []
    n = len(lines)

    for i in range(n):
        header_m = BOUT_HEADER_RE.match(lines[i])
        if not header_m:
            continue

        # Under `pdftotext -layout`, each bout block is a fixed 3-line
        # sequence: header line, then the bout-number line, then the
        # country/weight/time line.
        if i + 2 >= n:
            continue
        num_line = lines[i + 1].strip()
        if not num_line.isdigit():
            print(f"Warning: expected bout number after line {i!r}, got {num_line!r}", file=sys.stderr)
            continue
        bout_num = int(num_line)

        country_m = COUNTRY_LINE_RE.match(lines[i + 2])
        if not country_m:
            print(f"Warning: could not parse country line for bout {bout_num}: {lines[i + 2]!r}", file=sys.stderr)
            continue

        age = header_m.group("age")
        gender = GENDER_MAP[header_m.group("gender")]
        weight = country_m.group("weight")
        category = f"{age} {gender} -{weight}kg"

        red_country_name = country_m.group("red_country").strip()
        blue_country_name = country_m.group("blue_country").strip()
        red_code, red_flag = flag_and_code(red_country_name)
        blue_code, blue_flag = flag_and_code(blue_country_name)

        bouts.append({
            "bout": bout_num,
            "category": category,
            "red": {
                "name": header_m.group("red").strip(),
                "country": red_code,
                "flag": red_flag,
            },
            "blue": {
                "name": header_m.group("blue").strip(),
                "country": blue_code,
                "flag": blue_flag,
            },
            "clock": clock_seconds(header_m.group("clock")),
        })

    return bouts


def convert(pdf_path: str, json_path: str):
    text = extract_text(pdf_path)
    bouts = parse_bouts(text)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"bouts": bouts}, f, indent=4, ensure_ascii=False)

    print(f"Wrote {len(bouts)} bouts to {json_path}")


def main():
    parser = argparse.ArgumentParser(description="Convert a RING-A schedule PDF to bouts JSON.")
    parser.add_argument("pdf", help="Path to the input PDF file")
    parser.add_argument("json", help="Path to write the output JSON file")
    args = parser.parse_args()
    convert(args.pdf, args.json)


if __name__ == "__main__":
    main()