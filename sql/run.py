"""Run every query in a .sql file against the database and print each result.

A query's title is the first `-- ` comment line above it. Comment-only blocks
with no SQL statement are skipped. With --md, results are also written as
Markdown tables.

Usage:
  python sql/run.py sql/checks.sql --db sql/cutover.db [--md sql/results.md]
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def blocks(text: str):
    """Yield (title, sql) for each complete statement in the file."""
    buffer, title, fresh = [], None, True
    for line in text.splitlines():
        stripped = line.strip()
        if not buffer:
            if not stripped:
                fresh = True  # a blank line ends a comment block; the next one names the next query
                continue
            if stripped.startswith("--"):
                if fresh:
                    title, fresh = stripped[2:].strip() or None, False
                continue
        buffer.append(line)
        statement = "\n".join(buffer)
        if sqlite3.complete_statement(statement):
            if statement.strip(" ;\n"):
                yield title or "(untitled)", statement.strip()
            buffer, title, fresh = [], None, True


def table(columns, rows, markdown: bool) -> str:
    cells = [[("NULL" if v is None else str(v)) for v in row] for row in rows]
    if markdown:
        lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
        lines += ["| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in cells]
        return "\n".join(lines)
    widths = [max(len(c), *(len(r[i]) for r in cells)) if cells else len(c) for i, c in enumerate(columns)]
    out = ["  ".join(c.ljust(w) for c, w in zip(columns, widths)), "  ".join("-" * w for w in widths)]
    out += ["  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in cells]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--db", type=Path, default=Path("sql/cutover.db"))
    parser.add_argument("--md", type=Path, help="also write results as Markdown to this file")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"{args.db} not found; run sql/load.py first.", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    report, title = [], None
    try:
        for title, sql in blocks(args.sql_file.read_text(encoding="utf-8")):
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall()
            print(f"\n== {title}  ({len(rows)} rows)\n{table(columns, rows, False)}")
            report.append(f"### {title}\n\n{len(rows)} rows.\n\n{table(columns, rows, True)}\n")
    except sqlite3.Error as error:
        print(f"\nSQL error in '{title}': {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if args.md:
        args.md.write_text("\n".join(report), encoding="utf-8")
        print(f"\nMarkdown written to {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
