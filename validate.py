"""Validate one fictional CSV against a reviewed rule set; a rejected file has no clean handoff."""

import argparse
import csv
import hashlib
import io
import re
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import NamedTuple


DEFAULT_RULES = Path(__file__).resolve().parent / "rules" / "tenants.yaml"
RULE_KEYS = {"header", "required", "dates", "enums", "unique", "encoding"}
ENCODINGS = {"utf-8-no-bom"}
DATE_MAXES = {"today"}
NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


class Rejection(NamedTuple):
    row: int
    rule: str
    why: str


class DateRule(NamedTuple):
    field: str
    max: str


class EnumRule(NamedTuple):
    field: str
    allowed: list[str]


class Rules(NamedTuple):
    header: list[str]
    required: list[str]
    dates: list[DateRule]
    enums: list[EnumRule]
    unique: list[list[str]]
    encoding: str


class CsvFormatError(ValueError):
    pass


class RulesError(ValueError):
    """The rules file is missing, malformed, or names something it must not; never guess."""


def _records(text: str) -> Iterator[list[str]]:
    """Read strict double-quote CSV, retaining newlines inside quoted fields.

    A quote inside an unquoted field is malformed, even though Python's
    csv.reader(strict=True) permits it. Never consume past a quoting error.
    """
    fields: list[str] = []
    value: list[str] = []
    state = "start"
    record_started = False
    index = 0
    while index < len(text):
        char = text[index]
        if state == "quoted":
            if char == '"':
                state = "closed"
            else:
                value.append(char)
        elif char in "\r\n":
            yield fields + ["".join(value)] if record_started else []
            fields, value, state, record_started = [], [], "start", False
            if char == "\r" and text[index:index + 2] == "\r\n":
                index += 1
            index += 1
            continue
        elif char == ",":
            fields.append("".join(value))
            value, state = [], "start"
        elif state == "closed":
            if char != '"':
                raise CsvFormatError("Malformed quoting: text follows a closing quote.")
            value.append('"')
            state = "quoted"
        elif char == '"':
            if state != "start":
                raise CsvFormatError("Malformed quoting: quote inside an unquoted field.")
            state = "quoted"
        else:
            value.append(char)
            state = "unquoted"
        record_started = True
        index += 1
    if state == "quoted":
        raise CsvFormatError("Malformed quoting: quoted field is not closed.")
    if record_started:
        yield fields + ["".join(value)]


def _string_list(value: object, where: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RulesError(f"{where} must be a list of non-empty strings.")
    if not allow_empty and not value:
        raise RulesError(f"{where} must not be empty.")
    if len(set(value)) != len(value):
        raise RulesError(f"{where} must not repeat a column.")
    return list(value)


def _mapping(value: object, where: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise RulesError(f"{where} must be a mapping with keys {', '.join(sorted(keys))}.")
    unknown = set(value) - keys
    missing = keys - set(value)
    if unknown:
        raise RulesError(f"{where} has unknown key(s): {', '.join(sorted(map(str, unknown)))}.")
    if missing:
        raise RulesError(f"{where} is missing key(s): {', '.join(sorted(missing))}.")
    return value


def load_yaml(path: Path, what: str) -> object:
    """Load one YAML document with PyYAML's safe loader; raise RulesError on any problem."""
    try:
        import yaml
    except ImportError as error:
        raise RulesError(f"PyYAML is required to read {what} {path}: {error}. Install it with: pip install pyyaml") from error
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RulesError(f"Cannot read {what} {path}: {error}") from error
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise RulesError(f"Cannot parse {what} {path}: {error}") from error


def parse_rules(document: object) -> Rules:
    top = _mapping(document, "Rules file", RULE_KEYS)
    header = _string_list(top["header"], "header", allow_empty=False)
    columns = set(header)

    def known(column: str, where: str) -> str:
        if column not in columns:
            raise RulesError(f"{where} names column {column!r}, which is not in header.")
        return column

    required = [known(column, "required") for column in _string_list(top["required"], "required")]

    dates: list[DateRule] = []
    if not isinstance(top["dates"], list):
        raise RulesError("dates must be a list of {field, max} entries.")
    for entry in top["dates"]:
        item = _mapping(entry, "dates entry", {"field", "max"})
        if not isinstance(item["field"], str) or item["max"] not in DATE_MAXES:
            raise RulesError("Each dates entry needs a string field and max: today.")
        dates.append(DateRule(known(item["field"], "dates"), item["max"]))

    enums: list[EnumRule] = []
    if not isinstance(top["enums"], list):
        raise RulesError("enums must be a list of {field, allowed} entries.")
    for entry in top["enums"]:
        item = _mapping(entry, "enums entry", {"field", "allowed"})
        if not isinstance(item["field"], str):
            raise RulesError("Each enums entry needs a string field.")
        enums.append(EnumRule(known(item["field"], "enums"), _string_list(item["allowed"], "enums allowed", allow_empty=False)))

    unique: list[list[str]] = []
    if not isinstance(top["unique"], list):
        raise RulesError("unique must be a list of column lists.")
    for entry in top["unique"]:
        unique.append([known(column, "unique") for column in _string_list(entry, "unique entry", allow_empty=False)])

    if top["encoding"] not in ENCODINGS:
        raise RulesError(f"encoding must be one of: {', '.join(sorted(ENCODINGS))}.")
    return Rules(header, required, dates, enums, unique, top["encoding"])


def load_rules(path: Path) -> Rules:
    return parse_rules(load_yaml(path, "rules file"))


def _count_word(count: int) -> str:
    return NUMBER_WORDS[count] if 0 <= count < len(NUMBER_WORDS) else str(count)


def _join(items: list[str], conjunction: str) -> str:
    if len(items) <= 2:
        return f" {conjunction} ".join(items)
    return ", ".join(items[:-1]) + f", {conjunction} " + items[-1]


def _validate(source_bytes: bytes, today: date, rules: Rules) -> list[Rejection]:
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        return [Rejection(0, "F001", "UTF-8 BOM is not allowed.")]
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return [Rejection(0, "F001", "Input is not valid UTF-8.")]

    records = _records(text)
    try:
        header = next(records, None)
    except CsvFormatError as error:
        return [Rejection(0, "F001", str(error))]
    if header != rules.header:
        return [Rejection(0, "F001", f"Header must be exactly {','.join(rules.header)}.")]

    width = len(rules.header)
    index = {column: position for position, column in enumerate(rules.header)}
    rejections: list[Rejection] = []
    duplicate_groups: dict[tuple[tuple[str, ...], ...], list[int]] = {}
    record_count = 0
    malformed = False
    try:
        for row, fields in enumerate(records, start=2):
            record_count += 1
            if len(fields) != width:
                rejections.append(Rejection(row, "F003", f"Expected {_count_word(width)} fields; found {len(fields)}."))
                continue
            for field in rules.required:
                if not fields[index[field]].strip():
                    rejections.append(Rejection(row, "R001", f"{field} is required; it is blank."))

            for rule in rules.dates:
                raw = fields[index[rule.field]]
                parsed_date = None
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                    try:
                        parsed_date = date.fromisoformat(raw)
                    except ValueError:
                        pass
                if parsed_date is None:
                    rejections.append(Rejection(
                        row, "D001", f"{rule.field} must be an exact ASCII YYYY-MM-DD valid calendar date."
                    ))
                elif parsed_date > today:
                    rejections.append(Rejection(
                        row, "D002", f"{rule.field} is after the local run date {today.isoformat()}."
                    ))
            for rule in rules.enums:
                if fields[index[rule.field]] not in rule.allowed:
                    rejections.append(Rejection(row, "E001", f"{rule.field} must be exactly {_join(rule.allowed, 'or')}."))
            for columns in rules.unique:
                values = [fields[index[column]] for column in columns]
                if all(value.strip() for value in values):
                    key = (tuple(columns), tuple(value.strip().casefold() for value in values))
                    duplicate_groups.setdefault(key, []).append(row)
    except CsvFormatError as error:
        malformed = True
        rejections.append(Rejection(0, "F001", str(error)))

    if not record_count and not malformed:
        rejections.append(Rejection(0, "F002", "Input contains only the header; a data record is required."))
    for (columns, _), group in duplicate_groups.items():
        if len(group) > 1:
            for row in group:
                others = ", ".join(str(other) for other in group if other != row)
                rejections.append(Rejection(row, "U001", f"Duplicate {_join(list(columns), 'and')}; also in CSV record(s): {others}."))
    return sorted(rejections, key=lambda rejection: (rejection.row, rejection.rule))


def _reject_bytes(rejections: list[Rejection]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["row", "rule", "why"])
    writer.writerows(rejections)
    return buffer.getvalue().encode("utf-8")


def _write_new(path: Path, content: bytes, owned: set[Path]) -> None:
    with path.open("xb") as stream:
        owned.add(path)
        stream.write(content)


def run(source: Path, output: Path, rules: Rules) -> int:
    today = date.today()  # Capture the machine's local date exactly once per run.
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"I001: Cannot create a fresh output directory {output}: {error}", file=sys.stderr)
        return 2

    owned: set[Path] = set()
    rejections: list[Rejection] = []
    rejects_path = output / "rejects.csv"
    clean_path = output / "clean.csv"
    checksum_path = output / "clean.csv.sha256"
    try:
        source_bytes = source.read_bytes()
        rejections = _validate(source_bytes, today, rules)
        _write_new(rejects_path, _reject_bytes(rejections), owned)
        if rejections:
            return 1

        # Copy the validated snapshot without changing values, order, or bytes.
        _write_new(clean_path, source_bytes, owned)
        digest = hashlib.sha256(clean_path.read_bytes()).hexdigest()
        _write_new(checksum_path, f"{digest}  clean.csv\n".encode("ascii"), owned)
        return 0
    except OSError as error:
        for path in (checksum_path, clean_path):
            if path in owned:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    print(f"I001: Cannot remove this run's partial output {path}: {cleanup_error}", file=sys.stderr)
        message = f"Read/write failure: {error}"
        rejections.insert(0, Rejection(0, "I001", message))
        try:
            if rejects_path in owned:
                # Only replace our own incomplete log, never any prior run's file.
                rejects_path.write_bytes(_reject_bytes(rejections))
            else:
                _write_new(rejects_path, _reject_bytes(rejections), owned)
        except OSError as log_error:
            print(f"I001: Cannot write reject log {rejects_path}: {log_error}", file=sys.stderr)
        print(f"I001: {message}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES,
                        help=f"Rules YAML file (default: {DEFAULT_RULES.name} in the rules/ folder next to this script).")
    parser.add_argument("--out", type=Path, required=True, help="New local output directory; must not exist.")
    args = parser.parse_args(argv)
    try:
        rules = load_rules(args.rules)
    except RulesError as error:
        # A rules problem is a configuration failure: no output directory, no files, exit 2.
        print(f"Rules error in {args.rules}: {error}", file=sys.stderr)
        return 2
    return run(args.input_csv, args.out, rules)


if __name__ == "__main__":
    sys.exit(main())
