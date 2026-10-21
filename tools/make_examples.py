"""Generate the two fictional example files in data/examples with deliberate faults.

Seeded and deterministic: the same seed always produces the same bytes.
All names, units, part numbers, and descriptions are invented; nothing here
comes from a real export.

Each example ships in two forms:

- `<name>.csv`           the raw export with faults; `validate.py` rejects it.
- `<name>_reviewed.csv`  the same 200/150 rows after a (pretend) human review
                         replaced every faulty row with its corrected value;
                         `validate.py` accepts it, so `map.py` and
                         `reconcile.py` can run on its `clean.csv`.

Documented reject counts (what `validate.py` reports on the raw files):

tenants_appfolio_200.csv   (rules/tenants.yaml)   12 rejections
    R001 x2  (blank unit; blank name)
    D001 x2  (impossible date 2021-02-30; wrong format 15/01/2020)
    D002 x1  (future date 2099-01-01)
    E001 x2  (Active; pending)
    U001 x4  (two duplicate unit+name pairs, every member rejected)
    F003 x1  (a record with five fields)

parts_legacy_150.csv       (rules/parts.yaml)     10 rejections
    R001 x3  (blank description x2; blank part_number)
    D001 x2  (impossible date 2020-04-31; wrong format 2021-7-4)
    D002 x1  (future date 2099-06-30)
    E001 x2  (Active; retired)
    U001 x2  (one duplicate part_number+revision pair, both members rejected)

Usage (from the repository root):

    python tools/make_examples.py              # refuses to overwrite existing files
    python tools/make_examples.py --overwrite  # regenerate in place
"""

import argparse
import csv
import io
import random
import sys
from pathlib import Path


SEED = 20261020
EXAMPLES = Path(__file__).resolve().parents[1] / "data" / "examples"

FIRST_NAMES = [
    "Avery", "Blair", "Casey", "Drew", "Ellis", "Finley", "Harper", "Jordan", "Kendall", "Logan",
    "Morgan", "Parker", "Quinn", "Reese", "Rowan", "Sawyer", "Skyler", "Taylor", "Emerson", "Hayden",
]
LAST_NAMES = [
    "Example", "Sample", "Specimen", "Mockton", "Placeholder", "Testwell", "Fictionson", "Dummyfield",
    "Pretendale", "Nonreal", "Stubbs", "Fixture",
]
TENANT_STATUSES = ["active", "active", "active", "notice", "vacant"]
BUILDINGS = ["A", "B", "C"]

PART_NOUNS = ["Hex bolt", "Washer", "Bracket", "Gasket", "Bearing", "Spacer", "Pin", "Clamp", "Cover plate", "Shaft"]
PART_SPECS = ["M6x20", "M8x30", "M10x40", "DN25", "6205", "12mm", "1/4 in", "Type 2", "60mm", "100mm"]
PART_FINISH = ["zinc", "stainless", "black oxide", "anodized", "nylon", "brass", "nitrile", "steel"]
UNITS = ["EA", "EA", "EA", "KG", "M", "SET"]
PART_STATUSES = ["active", "active", "active", "active", "obsolete", "hold"]
REVISIONS = ["A", "B", "C", "01", "02"]


def _date(rng: random.Random, first_year: int, last_year: int) -> str:
    year = rng.randint(first_year, last_year)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)  # Always a real calendar date; faults are injected separately.
    return f"{year:04d}-{month:02d}-{day:02d}"


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def tenants(rng: random.Random) -> tuple[bytes, bytes]:
    header = ["unit", "name", "move_in_date", "status"]
    rows: list[list[str]] = []
    used_names: set[str] = set()
    for number in range(200):
        building = BUILDINGS[number // 70]
        unit = f"{building}-{101 + number % 70 + 100 * (number // 70 % 3)}"
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used_names:
                used_names.add(name)
                break
        rows.append([unit, name, _date(rng, 2015, 2026), rng.choice(TENANT_STATUSES)])
    for row in rows:
        if row[2] > "2026-06-28":
            row[2] = "2026-06-28"

    reviewed = [row.copy() for row in rows]
    faulty = [row.copy() for row in rows]
    # Record numbers below are CSV records (header is record 1), so row index + 2.
    faulty[11][0] = ""                          # record 13: R001 blank unit
    faulty[24][1] = "   "                       # record 26: R001 whitespace-only name
    faulty[37][2] = "2021-02-30"                # record 39: D001 impossible date
    faulty[52][2] = "15/01/2020"                # record 54: D001 wrong format
    faulty[66][2] = "2099-01-01"                # record 68: D002 future date
    faulty[81][3] = "Active"                    # record 83: E001 wrong case
    faulty[95][3] = "pending"                   # record 97: E001 unknown status
    faulty[120] = faulty[119].copy()            # records 121+122: U001 exact duplicate pair
    faulty[151] = [f" {faulty[150][0]} ", faulty[150][1].upper(), faulty[150][2], faulty[150][3]]
    #                                              records 152+153: U001 duplicate despite spacing/case
    faulty[177] = faulty[177] + ["extra"]       # record 179: F003 five fields
    return _csv_bytes(header, faulty), _csv_bytes(header, reviewed)


def parts(rng: random.Random) -> tuple[bytes, bytes]:
    header = ["part_number", "revision", "description", "unit", "status", "created_on"]
    rows: list[list[str]] = []
    for number in range(150):
        part_number = f"PN-{10000 + number * 7 + rng.randint(0, 5)}"
        description = f"{rng.choice(PART_NOUNS)} {rng.choice(PART_SPECS)} {rng.choice(PART_FINISH)}"
        rows.append([part_number, rng.choice(REVISIONS), description, rng.choice(UNITS),
                     rng.choice(PART_STATUSES), _date(rng, 2012, 2026)])
    for row in rows:
        if row[5] > "2026-06-28":
            row[5] = "2026-06-28"

    reviewed = [row.copy() for row in rows]
    faulty = [row.copy() for row in rows]
    faulty[8][2] = ""                           # record 10: R001 blank description
    faulty[33][2] = " "                         # record 35: R001 whitespace-only description
    faulty[47][0] = ""                          # record 49: R001 blank part_number
    faulty[61][5] = "2020-04-31"                # record 63: D001 impossible date
    faulty[76][5] = "2021-7-4"                  # record 78: D001 wrong format
    faulty[90][5] = "2099-06-30"                # record 92: D002 future date
    faulty[104][4] = "Active"                   # record 106: E001 wrong case
    faulty[118][4] = "retired"                  # record 120: E001 unknown status
    faulty[133][0], faulty[133][1] = faulty[132][0], faulty[132][1]
    #                                              records 134+135: U001 duplicate part_number+revision
    return _csv_bytes(header, faulty), _csv_bytes(header, reviewed)


def generate(seed: int = SEED) -> dict[str, bytes]:
    """Return every example file's bytes, keyed by file name."""
    rng = random.Random(seed)
    tenants_faulty, tenants_reviewed = tenants(rng)
    parts_faulty, parts_reviewed = parts(rng)
    return {
        "tenants_appfolio_200.csv": tenants_faulty,
        "tenants_appfolio_200_reviewed.csv": tenants_reviewed,
        "parts_legacy_150.csv": parts_faulty,
        "parts_legacy_150_reviewed.csv": parts_reviewed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the fictional example CSVs.")
    parser.add_argument("--out", type=Path, default=EXAMPLES, help=f"Directory to write into (default: {EXAMPLES}).")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing files; otherwise refuse.")
    args = parser.parse_args(argv)
    files = generate()
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        existing = [name for name in files if (args.out / name).exists()]
        if existing and not args.overwrite:
            print(f"Refusing to overwrite existing file(s) in {args.out}: {', '.join(existing)} (use --overwrite).",
                  file=sys.stderr)
            return 2
        for name, content in files.items():
            (args.out / name).write_bytes(content)
    except OSError as error:
        print(f"Cannot write examples: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
