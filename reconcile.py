"""Compare a clean source CSV with a mapped target CSV and write reconciliation.md; FAIL exits 1.

Both inputs must match their `.sha256` sidecars first (exit 2 otherwise).
Checks: data row count, the set of key values (one column or a composite
`--key a,b`), and for every paired column the non-null count and row-by-row
value equality joined on the key. Constant columns must hold the declared
value in every target row. Columns are paired through the mapping when
`--mapping` is given, otherwise by shared column name. "non-null" means the
value is not the empty string; nothing is trimmed and values are compared
byte for byte.
"""

import argparse
import hashlib
import sys
from pathlib import Path
from typing import NamedTuple

import validate
from map import Mapping, MapRejected, load_mapping, read_sidecar


class Check(NamedTuple):
    name: str
    expected: object
    actual: object

    @property
    def passed(self) -> bool:
        return self.expected == self.actual


class ReconcileError(ValueError):
    """Invocation or input problem; nothing can be compared. Exit 2."""


class Table(NamedTuple):
    path: Path
    digest: str
    header: list[str]
    rows: list[list[str]]


def read_table(path: Path) -> Table:
    try:
        expected_digest = read_sidecar(path)
    except MapRejected as error:
        raise ReconcileError(str(error)) from None
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReconcileError(f"Cannot read {path}: {error}") from error
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_digest:
        raise ReconcileError(f"{path.name} does not match its checksum sidecar {path.name}.sha256; "
                             "the file changed after it was written.")
    if content.startswith(b"\xef\xbb\xbf"):
        raise ReconcileError(f"{path} starts with a UTF-8 BOM.")
    try:
        records = list(validate._records(content.decode("utf-8")))
    except UnicodeDecodeError:
        raise ReconcileError(f"{path} is not valid UTF-8.") from None
    except validate.CsvFormatError as error:
        raise ReconcileError(f"{path}: {error}") from None
    if not records or not records[0] or len(set(records[0])) != len(records[0]):
        raise ReconcileError(f"{path} has no usable header.")
    header, rows = records[0], records[1:]
    for number, row in enumerate(rows, start=2):
        if len(row) != len(header):
            raise ReconcileError(f"{path}: CSV record {number} has {len(row)} fields; expected {len(header)}.")
    return Table(path, digest, header, rows)


def column_pairs(source: Table, target: Table, mapping: Mapping | None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (renamed pairs (source col, target col), constants (target col, value))."""
    if mapping is None:
        shared = [column for column in source.header if column in target.header]
        if not shared:
            raise ReconcileError("The files share no column names; pass --mapping to pair columns.")
        return [(column, column) for column in shared], []
    if source.header != mapping.source_header:
        raise ReconcileError(f"{source.path} header does not match the mapping's source_header.")
    if target.header != mapping.target_header:
        raise ReconcileError(f"{target.path} header does not match the mapping's target_header.")
    return list(mapping.columns), list(mapping.constants)


def compare(source: Table, target: Table, key: list[str], mapping: Mapping | None) -> tuple[list[Check], dict]:
    pairs, constants = column_pairs(source, target, mapping)
    target_for = dict(pairs)
    for column in key:
        if column not in source.header:
            raise ReconcileError(f"Key column {column!r} is not in {source.path}.")
        if column not in target_for:
            raise ReconcileError(f"Key column {column!r} has no counterpart in {target.path}.")
    target_key = [target_for[column] for column in key]

    def keys(table: Table, columns: list[str]) -> set[tuple[str, ...]]:
        positions = [table.header.index(column) for column in columns]
        return {tuple(row[position] for position in positions) for row in table.rows}

    source_keys, target_keys = keys(source, key), keys(target, target_key)
    missing = sorted(source_keys - target_keys)
    extra = sorted(target_keys - source_keys)

    def non_null(table: Table, column: str) -> int:
        position = table.header.index(column)
        return sum(1 for row in table.rows if row[position] != "")

    def by_key(table: Table, columns: list[str]) -> dict[tuple[str, ...], list[list[str]]]:
        positions = [table.header.index(column) for column in columns]
        grouped: dict[tuple[str, ...], list[list[str]]] = {}
        for row in table.rows:
            grouped.setdefault(tuple(row[position] for position in positions), []).append(row)
        return grouped

    source_by_key, target_by_key = by_key(source, key), by_key(target, target_key)
    joined = sorted(source_keys & target_keys)

    checks = [
        Check("Row count", len(source.rows), len(target.rows)),
        Check("Distinct keys", len(source_keys), len(target_keys)),
        Check("Keys missing from target", 0, len(missing)),
        Check("Keys not in source", 0, len(extra)),
    ]
    differences: dict[str, list[tuple[str, ...]]] = {}
    for source_column, target_column in pairs:
        checks.append(Check(f"Non-null: {source_column} -> {target_column}",
                            non_null(source, source_column), non_null(target, target_column)))
        # Row-by-row, byte-for-byte equality joined on the key. A key that repeats compares the
        # sorted multiset of its values so that no row can hide behind another.
        source_position, target_position = source.header.index(source_column), target.header.index(target_column)
        unequal = [
            joined_key for joined_key in joined
            if sorted(row[source_position] for row in source_by_key[joined_key])
            != sorted(row[target_position] for row in target_by_key[joined_key])
        ]
        name = f"Values equal: {source_column} -> {target_column}"
        checks.append(Check(name, len(joined), len(joined) - len(unequal)))
        if unequal:
            differences[name] = unequal
    for target_column, value in constants:
        position = target.header.index(target_column)
        checks.append(Check(f"Non-null: (constant) -> {target_column}",
                            len(source.rows) if value != "" else 0, non_null(target, target_column)))
        # Every target row must hold exactly the declared constant; the expected count is the source's.
        key_positions = [target.header.index(column) for column in target_key]
        unequal = sorted({tuple(row[position_] for position_ in key_positions)
                          for row in target.rows if row[position] != value})
        name = f"Values equal: (constant {value!r}) -> {target_column}"
        checks.append(Check(name, len(source.rows), sum(1 for row in target.rows if row[position] == value)))
        if unequal:
            differences[name] = unequal
    details = {"target_key": target_key, "missing": missing, "extra": extra, "differences": differences}
    return checks, details


def _cell(text: object) -> str:
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render(source: Table, target: Table, key: list[str], mapping_path: Path | None,
           checks: list[Check], details: dict) -> str:
    failed = [check for check in checks if not check.passed]
    lines = [
        "# Reconciliation",
        "",
        f"- Source: `{_cell(source.path.as_posix())}` ({len(source.rows)} data rows, sha256 `{source.digest}`)",
        f"- Target: `{_cell(target.path.as_posix())}` ({len(target.rows)} data rows, sha256 `{target.digest}`)",
        f"- Key: {_cell(', '.join(key))} -> {_cell(', '.join(details['target_key']))}",
        f"- Mapping: {('`' + _cell(mapping_path.as_posix()) + '`') if mapping_path else 'none (columns paired by shared name)'}",
        "",
        "| Check | Expected | Actual | Result |",
        "|---|---|---|---|",
    ]
    for check in checks:
        lines.append(f"| {_cell(check.name)} | {_cell(check.expected)} | {_cell(check.actual)} | {'PASS' if check.passed else 'FAIL'} |")
    lines.append("")
    for label in ("missing", "extra"):
        values = details[label]
        if values:
            title = "Keys missing from target" if label == "missing" else "Keys not in source"
            shown = values[:20]
            lines.append(f"## {title} ({len(values)}{', first 20 shown' if len(values) > 20 else ''})")
            lines.append("")
            lines.append("```text")
            lines.extend(repr(value) for value in shown)
            lines.append("```")
            lines.append("")
    for name, keys_ in details["differences"].items():
        lines.append(f"## {_cell(name)} ({len(keys_)} key(s) differ{', first 5 shown' if len(keys_) > 5 else ''})")
        lines.append("")
        lines.append("```text")
        lines.extend(repr(value) for value in keys_[:5])
        lines.append("```")
        lines.append("")
    verdict = "PASS" if not failed else "FAIL"
    lines.append(f"Verdict: {verdict} ({len(checks)} checks, {len(failed)} failed)")
    lines.append("")
    return "\n".join(lines)


def run(source_path: Path, target_path: Path, key: list[str], mapping_path: Path | None, output: Path) -> int:
    report_path = output / "reconciliation.md"
    try:
        mapping = load_mapping(mapping_path) if mapping_path else None
    except validate.RulesError as error:
        print(f"Mapping error in {mapping_path}: {error}", file=sys.stderr)
        return 2
    if report_path.exists():
        print(f"I001: Refusing to overwrite existing output in {output}: {report_path.name}", file=sys.stderr)
        return 2
    try:
        source, target = read_table(source_path), read_table(target_path)
        checks, details = compare(source, target, key, mapping)
    except ReconcileError as error:
        print(f"Reconcile error: {error}", file=sys.stderr)
        return 2
    report = render(source, target, key, mapping_path, checks, details)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"I001: Cannot create output directory {output}: {error}", file=sys.stderr)
        return 2
    try:
        with report_path.open("xb") as stream:
            stream.write(report.encode("utf-8"))
    except OSError as error:
        print(f"I001: Cannot write {report_path}: {error}", file=sys.stderr)
        try:
            report_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            print(f"I001: Cannot remove this run's partial output {report_path}: {cleanup_error}", file=sys.stderr)
        return 2
    failed = [check for check in checks if not check.passed]
    if failed:
        print(f"Reconciliation FAIL: {len(failed)} of {len(checks)} checks failed. See {report_path}.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_csv", type=Path, help="Validated clean CSV (the stage-1 clean.csv).")
    parser.add_argument("target_csv", type=Path, help="Mapped target CSV (the stage-2 target.csv).")
    parser.add_argument("--key", required=True,
                        help="Source key column, or a composite as a,b. With --mapping the target column is looked up.")
    parser.add_argument("--mapping", type=Path, help="Mapping YAML used by map.py; pairs columns and constants.")
    parser.add_argument("--out", type=Path, required=True, help="Run directory; reconciliation.md must not exist there yet.")
    args = parser.parse_args(argv)
    key = [column for column in args.key.split(",")]
    if not key or any(column == "" for column in key):
        print("Reconcile error: --key must name one or more columns, separated by commas.", file=sys.stderr)
        return 2
    return run(args.source_csv, args.target_csv, key, args.mapping, args.out)


if __name__ == "__main__":
    sys.exit(main())
