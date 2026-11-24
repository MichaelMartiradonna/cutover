"""Load CSV files into a SQLite database so the checks in checks.sql can run.

Standard library only. Every column is stored as TEXT, exactly as written in the
file; nothing is trimmed, typed or repaired, so the SQL sees the same faults the
validator sees. Two columns are added to every table:

  record_no     the CSV record number, counting the header as record 1
                (the same numbering validate.py uses in rejects.csv)
  extra_fields  how many fields the record has beyond the header (0 if none);
                missing fields are stored as NULL

Usage (PowerShell, from the repository folder):
  python sql/load.py --db sql/cutover.db tenants_raw=data/examples/tenants_appfolio_200.csv tenants_reviewed=data/examples/tenants_appfolio_200_reviewed.csv

Each argument is TABLE=CSV_PATH. An existing table of the same name is replaced.
"""

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load(conn: sqlite3.Connection, table: str, path: Path) -> int:
    if not NAME.match(table):
        raise ValueError(f"table name {table!r} must be letters, digits and underscores")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        for column in header:
            if not NAME.match(column) or column in ("record_no", "extra_fields"):
                raise ValueError(f"{path}: header column {column!r} is not a plain SQL name")
        columns = ", ".join(f'"{c}" TEXT' for c in header)
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" (record_no INTEGER PRIMARY KEY, {columns}, extra_fields INTEGER NOT NULL)')
        marks = ", ".join("?" for _ in range(len(header) + 2))
        rows = 0
        for record in reader:
            record_no = reader.line_num  # equals the record number while no field spans lines
            values = record[: len(header)] + [None] * (len(header) - len(record))
            conn.execute(f'INSERT INTO "{table}" VALUES ({marks})',
                         [record_no, *values, max(0, len(record) - len(header))])
            rows += 1
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("sql/cutover.db"))
    parser.add_argument("tables", nargs="+", metavar="TABLE=CSV_PATH")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        for item in args.tables:
            table, _, source = item.partition("=")
            if not source:
                parser.error(f"{item!r} is not TABLE=CSV_PATH")
            count = load(conn, table, Path(source))
            print(f"{table}: {count} rows from {source}")
        conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
