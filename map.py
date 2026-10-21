"""Rename and reorder the columns of a validated clean CSV into a target layout; never change a value.

Input must be a validated clean file with its checksum sidecar next to it
(`clean.csv` + `clean.csv.sha256`). The mapping YAML may only rename columns,
reorder them, drop columns it lists under `drop:`, and add declared constant
columns. There are no transforms and no expressions.
"""

import argparse
import csv
import hashlib
import io
import re
import sys
from pathlib import Path
from typing import NamedTuple

import validate
from validate import RulesError, _mapping, _string_list


MAPPING_KEYS = {"source_header", "target_header", "columns", "constants", "drop"}
REPORT_HEADER = ["target_column", "source_column", "kind", "rows", "note"]
SIDECAR = re.compile(rb"^([0-9a-f]{64})  (.+)\n\Z")


class Mapping(NamedTuple):
    source_header: list[str]
    target_header: list[str]
    columns: list[tuple[str, str]]       # (from, to)
    constants: list[tuple[str, str]]     # (to, value)
    drop: list[str]


class MapRejected(ValueError):
    """The input or the mapping cannot be used as-is; the report lists why. Exit 1."""


def parse_mapping(document: object) -> Mapping:
    """Check the mapping's shape strictly. Anything unexpected is a configuration error."""
    if not isinstance(document, dict):
        raise RulesError(f"Mapping file must be a mapping with keys {', '.join(sorted(MAPPING_KEYS))}.")
    top = document
    unknown = set(top) - MAPPING_KEYS
    missing = {"source_header", "target_header", "columns"} - set(top)
    if unknown:
        raise RulesError(f"Mapping file has unknown key(s): {', '.join(sorted(map(str, unknown)))}.")
    if missing:
        raise RulesError(f"Mapping file is missing key(s): {', '.join(sorted(missing))}.")
    source_header = _string_list(top["source_header"], "source_header", allow_empty=False)
    target_header = _string_list(top["target_header"], "target_header", allow_empty=False)
    if not isinstance(top["columns"], list):
        raise RulesError("columns must be a list of {from, to} entries.")
    columns: list[tuple[str, str]] = []
    for entry in top["columns"]:
        item = _mapping(entry, "columns entry", {"from", "to"})
        if not isinstance(item["from"], str) or not isinstance(item["to"], str):
            raise RulesError("Each columns entry needs string from and to values.")
        if item["from"] not in source_header:
            raise RulesError(f"columns names source column {item['from']!r}, which is not in source_header.")
        if item["to"] not in target_header:
            raise RulesError(f"columns names target column {item['to']!r}, which is not in target_header.")
        columns.append((item["from"], item["to"]))
    constants: list[tuple[str, str]] = []
    if "constants" in top:
        if not isinstance(top["constants"], list):
            raise RulesError("constants must be a list of {to, value} entries.")
        for entry in top["constants"]:
            item = _mapping(entry, "constants entry", {"to", "value"})
            if not isinstance(item["to"], str) or not isinstance(item["value"], str):
                raise RulesError("Each constants entry needs a string to and a quoted string value.")
            if item["to"] not in target_header:
                raise RulesError(f"constants names target column {item['to']!r}, which is not in target_header.")
            constants.append((item["to"], item["value"]))
    drop = _string_list(top.get("drop", []), "drop")
    for column in drop:
        if column not in source_header:
            raise RulesError(f"drop names source column {column!r}, which is not in source_header.")
    mapped_sources = [source for source, _ in columns]
    for column in drop:
        if column in mapped_sources:
            raise RulesError(f"Source column {column!r} is both mapped and dropped.")
    targets = [target for _, target in columns] + [target for target, _ in constants]
    if len(set(targets)) != len(targets):
        raise RulesError("A target column is assigned more than once.")
    return Mapping(source_header, target_header, columns, constants, drop)


def load_mapping(path: Path) -> Mapping:
    return parse_mapping(validate.load_yaml(path, "mapping file"))


def mapping_problems(mapping: Mapping) -> list[tuple[str, str, str]]:
    """Return (target_column, source_column, note) for every incomplete part of the mapping."""
    problems = []
    mapped_sources = {source for source, _ in mapping.columns}
    for column in mapping.source_header:
        if column not in mapped_sources and column not in mapping.drop:
            problems.append(("", column, f"Source column {column!r} is not mapped and not listed under drop."))
    assigned = {target for _, target in mapping.columns} | {target for target, _ in mapping.constants}
    for column in mapping.target_header:
        if column not in assigned:
            problems.append((column, "", f"Target column {column!r} has no source column and no constant."))
    return problems


def read_sidecar(path: Path) -> str:
    """Return the digest stored in `<path>.sha256`; raise MapRejected if absent or malformed."""
    sidecar = path.with_name(path.name + ".sha256")
    try:
        content = sidecar.read_bytes()
    except FileNotFoundError:
        raise MapRejected(f"Missing checksum sidecar {sidecar.name}; the input is not a validated clean file.") from None
    match = SIDECAR.match(content)
    if not match or match.group(2).decode("utf-8", "replace") != path.name:
        raise MapRejected(f"Checksum sidecar {sidecar.name} is malformed or names a different file.")
    return match.group(1).decode("ascii")


def build_target(source_bytes: bytes, mapping: Mapping) -> tuple[bytes, int]:
    """Return the target CSV bytes and the number of data rows; values are copied unchanged."""
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise MapRejected("Input is not valid UTF-8.") from None
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        raise MapRejected("Input starts with a UTF-8 BOM.")
    try:
        records = list(validate._records(text))
    except validate.CsvFormatError as error:
        raise MapRejected(str(error)) from None
    if not records or records[0] != mapping.source_header:
        raise MapRejected(f"Input header must be exactly {','.join(mapping.source_header)}.")
    position = {column: index for index, column in enumerate(mapping.source_header)}
    source_for = {target: source for source, target in mapping.columns}
    constant_for = dict(mapping.constants)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(mapping.target_header)
    width = len(mapping.source_header)
    for row, fields in enumerate(records[1:], start=2):
        if len(fields) != width:
            raise MapRejected(f"CSV record {row} has {len(fields)} fields; expected {width}.")
        writer.writerow([fields[position[source_for[target]]] if target in source_for else constant_for[target]
                         for target in mapping.target_header])
    return buffer.getvalue().encode("utf-8"), len(records) - 1


def _report_bytes(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REPORT_HEADER)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _write_new(path: Path, content: bytes, owned: set[Path]) -> None:
    with path.open("xb") as stream:
        owned.add(path)
        stream.write(content)


def run(source: Path, mapping: Mapping, output: Path) -> int:
    target_path = output / "target.csv"
    checksum_path = output / "target.csv.sha256"
    report_path = output / "mapping_report.csv"
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"I001: Cannot create output directory {output}: {error}", file=sys.stderr)
        return 2
    existing = [path.name for path in (target_path, checksum_path, report_path) if path.exists()]
    if existing:
        print(f"I001: Refusing to overwrite existing output in {output}: {', '.join(existing)}", file=sys.stderr)
        return 2

    owned: set[Path] = set()
    try:
        try:
            problems = mapping_problems(mapping)
            if problems:
                raise MapRejected("The mapping is incomplete.", problems)
            expected_digest = read_sidecar(source)
            source_bytes = source.read_bytes()
            actual_digest = hashlib.sha256(source_bytes).hexdigest()
            if actual_digest != expected_digest:
                raise MapRejected(f"{source.name} does not match its checksum sidecar; the input is not the validated clean file.")
            target_bytes, rows = build_target(source_bytes, mapping)
        except MapRejected as rejected:
            message = rejected.args[0]
            detail = rejected.args[1] if len(rejected.args) > 1 else [("", "", message)]
            _write_new(report_path, _report_bytes([[target, src, "error", 0, note] for target, src, note in detail]), owned)
            print(f"Mapping rejected: {message} See {report_path}.", file=sys.stderr)
            return 1

        _write_new(target_path, target_bytes, owned)
        digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
        _write_new(checksum_path, f"{digest}  target.csv\n".encode("ascii"), owned)
        source_for = {target: src for src, target in mapping.columns}
        report_rows: list[list[object]] = []
        for target in mapping.target_header:
            if target in source_for:
                report_rows.append([target, source_for[target], "rename", rows, ""])
            else:
                report_rows.append([target, "", "constant", rows, ""])
        for column in mapping.drop:
            report_rows.append(["", column, "drop", rows, ""])
        _write_new(report_path, _report_bytes(report_rows), owned)
        return 0
    except OSError as error:
        for path in (checksum_path, target_path):
            if path in owned:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    print(f"I001: Cannot remove this run's partial output {path}: {cleanup_error}", file=sys.stderr)
        message = f"Read/write failure: {error}"
        try:
            content = _report_bytes([["", "", "error", 0, f"I001 {message}"]])
            if report_path in owned:
                report_path.write_bytes(content)
            else:
                _write_new(report_path, content, owned)
        except OSError as log_error:
            print(f"I001: Cannot write mapping report {report_path}: {log_error}", file=sys.stderr)
        print(f"I001: {message}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clean_csv", type=Path, help="Validated clean CSV; its .sha256 sidecar must sit next to it.")
    parser.add_argument("--mapping", type=Path, required=True, help="Mapping YAML file.")
    parser.add_argument("--out", type=Path, required=True,
                        help="Run directory; target.csv, target.csv.sha256, and mapping_report.csv must not exist there yet.")
    args = parser.parse_args(argv)
    try:
        mapping = load_mapping(args.mapping)
    except RulesError as error:
        print(f"Mapping error in {args.mapping}: {error}", file=sys.stderr)
        return 2
    return run(args.clean_csv, mapping, args.out)


if __name__ == "__main__":
    sys.exit(main())
