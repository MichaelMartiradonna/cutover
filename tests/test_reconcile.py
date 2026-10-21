import hashlib
import io
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import reconcile


ROOT = Path(__file__).resolve().parents[1]

SOURCE = (
    b"unit,name,move_in_date,status\n"
    b"101,Avery Example,2020-01-15,active\n"
    b"102,Blair Sample,2021-03-01,notice\n"
    b"103,Casey Specimen,,vacant\n"
)
# Same rows, renamed and reordered, plus a constant.
TARGET = (
    b"Tenant Name,Unit Number,Lease Status,Move In,Source System\n"
    b"Avery Example,101,active,2020-01-15,appfolio-export\n"
    b"Blair Sample,102,notice,2021-03-01,appfolio-export\n"
    b"Casey Specimen,103,vacant,,appfolio-export\n"
)
MAPPING = """\
source_header: [unit, name, move_in_date, status]
target_header: [Tenant Name, Unit Number, Lease Status, Move In, Source System]
columns:
  - {from: name, to: Tenant Name}
  - {from: unit, to: Unit Number}
  - {from: status, to: Lease Status}
  - {from: move_in_date, to: Move In}
constants:
  - {to: Source System, value: "appfolio-export"}
"""
MAPPED_CHECKS = 14   # 4 fixed + 4 renames x (non-null, values equal) + 1 constant x (non-null, values equal)


def _sidecar(path: Path, content: bytes) -> None:
    path.with_name(path.name + ".sha256").write_bytes(
        hashlib.sha256(content).hexdigest().encode("ascii") + f"  {path.name}\n".encode("ascii"))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.source = self.root / "clean.csv"
        self.target = self.root / "target.csv"
        self.mapping = self.root / "mapping.yaml"
        self.mapping.write_text(MAPPING, encoding="utf-8")
        self._set(self.source, SOURCE)
        self._set(self.target, TARGET)
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def _set(self, path: Path, content: bytes):
        """Write a file the way a stage would: bytes plus a matching sidecar."""
        path.write_bytes(content)
        _sidecar(path, content)

    def _invoke(self, key="unit", mapping=True, out=None, source=None, target=None):
        self.counter += 1
        out = out or self.root / f"run-{self.counter}"
        argv = [str(source or self.source), str(target or self.target), "--key", key, "--out", str(out)]
        if mapping:
            argv += ["--mapping", str(self.mapping)]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = reconcile.main(argv)
        report = (out / "reconciliation.md").read_text(encoding="utf-8") if (out / "reconciliation.md").exists() else None
        return code, stderr.getvalue(), report, out

    def _rows(self, report):
        rows = {}
        for line in report.splitlines():
            match = re.fullmatch(r"\| (.+?) \| (.+?) \| (.+?) \| (PASS|FAIL) \|", line)
            if match and match.group(1) != "Check":
                rows[match.group(1)] = (match.group(2), match.group(3), match.group(4))
        return rows

    def _assert_refused(self, code, stderr, report, out, fragment):
        self.assertEqual(code, 2)
        self.assertIn("Reconcile error", stderr)
        self.assertIn(fragment, stderr)
        self.assertIsNone(report)
        self.assertFalse(out.exists(), "exit 2 must leave no run directory behind")

    def test_identical_pass(self):
        code, stderr, report, out = self._invoke(key="unit,name")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual({path.name for path in out.iterdir()}, {"reconciliation.md"})
        rows = self._rows(report)
        self.assertEqual(len(rows), MAPPED_CHECKS)
        self.assertEqual(rows["Row count"], ("3", "3", "PASS"))
        self.assertEqual(rows["Distinct keys"], ("3", "3", "PASS"))
        self.assertEqual(rows["Keys missing from target"], ("0", "0", "PASS"))
        self.assertEqual(rows["Keys not in source"], ("0", "0", "PASS"))
        self.assertEqual(rows["Non-null: move_in_date -> Move In"], ("2", "2", "PASS"))
        self.assertEqual(rows["Values equal: move_in_date -> Move In"], ("3", "3", "PASS"))
        self.assertEqual(rows["Non-null: (constant) -> Source System"], ("3", "3", "PASS"))
        self.assertEqual(rows["Values equal: (constant 'appfolio-export') -> Source System"], ("3", "3", "PASS"))
        self.assertEqual({result for _, _, result in rows.values()}, {"PASS"})
        self.assertIn(f"\nVerdict: PASS ({MAPPED_CHECKS} checks, 0 failed)\n", report)
        self.assertIn("Key: unit, name -> Unit Number, Tenant Name", report)
        self.assertEqual(self.source.read_bytes(), SOURCE)
        self.assertEqual(self.target.read_bytes(), TARGET)

        # Without a mapping, columns pair by shared name: comparing the source with itself passes.
        code, stderr, report, out = self._invoke(key="unit", mapping=False, target=self.source)
        self.assertEqual((code, stderr), (0, ""))
        rows = self._rows(report)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows["Values equal: unit -> unit"], ("3", "3", "PASS"))
        self.assertIn("Mapping: none", report)

        # Rerunning into the same directory refuses to overwrite.
        before = report
        code, stderr, report, out = self._invoke(key="unit", mapping=False, target=self.source, out=out)
        self.assertEqual(code, 2)
        self.assertIn("Refusing to overwrite", stderr)
        self.assertEqual(report, before)

    def test_row_count_mismatch_fail(self):
        self._set(self.target, TARGET + b"Drew Mockton,104,active,2019-05-05,appfolio-export\n")
        code, stderr, report, _ = self._invoke()
        self.assertEqual(code, 1)
        self.assertIn("Reconciliation FAIL", stderr)
        rows = self._rows(report)
        self.assertEqual(rows["Row count"], ("3", "4", "FAIL"))
        self.assertEqual(rows["Keys not in source"], ("0", "1", "FAIL"))
        # The constant checks are measured against the source row count, so the extra row fails them too.
        self.assertEqual(rows["Non-null: (constant) -> Source System"], ("3", "4", "FAIL"))
        self.assertEqual(rows["Values equal: (constant 'appfolio-export') -> Source System"], ("3", "4", "FAIL"))
        self.assertIn("## Keys not in source (1)", report)
        self.assertIn("('104',)", report)
        self.assertRegex(report, rf"\nVerdict: FAIL \({MAPPED_CHECKS} checks, [1-9]\d* failed\)\n")

        # A dropped row keeps every surviving value equal but the row count differs.
        self._set(self.target, TARGET.replace(b"Casey Specimen,103,vacant,,appfolio-export\n", b""))
        code, _, report, _ = self._invoke()
        self.assertEqual(code, 1)
        rows = self._rows(report)
        self.assertEqual(rows["Row count"], ("3", "2", "FAIL"))
        self.assertEqual(rows["Values equal: name -> Tenant Name"], ("2", "2", "PASS"))

    def test_missing_key_fail(self):
        self._set(self.target, TARGET.replace(b"Blair Sample,102,", b"Blair Sample,102X,"))
        code, stderr, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        rows = self._rows(report)
        self.assertEqual(rows["Row count"], ("3", "3", "PASS"))
        self.assertEqual(rows["Keys missing from target"], ("0", "1", "FAIL"))
        self.assertEqual(rows["Keys not in source"], ("0", "1", "FAIL"))
        self.assertEqual(rows["Values equal: name -> Tenant Name"], ("2", "2", "PASS"))
        self.assertIn("('102',)", report)
        self.assertIn("('102X',)", report)
        self.assertIn(f"\nVerdict: FAIL ({MAPPED_CHECKS} checks, 2 failed)\n", report)

        # Composite keys compare every part; a changed name is a missing key too.
        self._set(self.target, TARGET.replace(b"Blair Sample,102,", b"Blair  Sample,102,"))
        code, _, report, _ = self._invoke(key="unit,name")
        self.assertEqual(code, 1)
        self.assertEqual(self._rows(report)["Keys missing from target"], ("0", "1", "FAIL"))
        self.assertIn("('102', 'Blair Sample')", report)

        # A key column that does not exist is an invocation error, not a FAIL: exit 2 and no report.
        self._assert_refused(*self._invoke(key="email"), "Key column 'email'")

    def test_nonnull_count_mismatch_fail(self):
        # Same rows and keys, but one Move In value became empty: non-null and value equality fail together.
        self._set(self.target, TARGET.replace(b"Avery Example,101,active,2020-01-15,", b"Avery Example,101,active,,"))
        code, stderr, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        rows = self._rows(report)
        self.assertEqual(rows["Row count"], ("3", "3", "PASS"))
        self.assertEqual(rows["Keys missing from target"], ("0", "0", "PASS"))
        self.assertEqual(rows["Non-null: move_in_date -> Move In"], ("2", "1", "FAIL"))
        self.assertEqual(rows["Values equal: move_in_date -> Move In"], ("3", "2", "FAIL"))
        self.assertIn(f"\nVerdict: FAIL ({MAPPED_CHECKS} checks, 2 failed)\n", report)

        # Whitespace is a value, not null: nothing is trimmed before counting.
        self._set(self.target, TARGET.replace(b"Casey Specimen,103,vacant,,", b"Casey Specimen,103,vacant, ,"))
        code, _, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        self.assertEqual(self._rows(report)["Non-null: move_in_date -> Move In"], ("2", "3", "FAIL"))

    def test_reconcile_detects_changed_value(self):
        # Counts, keys, and non-null counts all still match; only one status value differs.
        self._set(self.target, TARGET.replace(b"Blair Sample,102,notice,", b"Blair Sample,102,vacant,"))
        code, stderr, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        rows = self._rows(report)
        self.assertEqual(rows["Non-null: status -> Lease Status"], ("3", "3", "PASS"))
        self.assertEqual(rows["Values equal: status -> Lease Status"], ("3", "2", "FAIL"))
        self.assertEqual([result for _, _, result in rows.values()].count("FAIL"), 1)
        self.assertIn("## Values equal: status -> Lease Status (1 key(s) differ)", report)
        self.assertIn("('102',)", report)
        self.assertIn(f"\nVerdict: FAIL ({MAPPED_CHECKS} checks, 1 failed)\n", report)

        # Case and whitespace differences are differences: values are compared byte for byte.
        for altered in (b"Blair Sample,102,Notice,", b"Blair Sample,102,notice ,"):
            with self.subTest(altered=altered):
                self._set(self.target, TARGET.replace(b"Blair Sample,102,notice,", altered))
                code, _, report, _ = self._invoke(key="unit")
                self.assertEqual(code, 1)
                self.assertEqual(self._rows(report)["Values equal: status -> Lease Status"], ("3", "2", "FAIL"))

        # Without a mapping, shared columns are compared by value too.
        altered_source = self.root / "altered.csv"
        self._set(altered_source, SOURCE.replace(b"Casey Specimen", b"Casey Specimen Jr"))
        code, _, report, _ = self._invoke(key="unit", mapping=False, target=altered_source)
        self.assertEqual(code, 1)
        self.assertEqual(self._rows(report)["Values equal: name -> name"], ("3", "2", "FAIL"))
        self.assertIn("('103',)", report)

        # A repeated key compares the multiset of its values, so a swap inside the group still passes
        # but a changed member fails.
        self._set(self.source, SOURCE + b"101,Avery Example,2022-02-02,notice\n")
        self._set(self.target, TARGET.replace(b"Avery Example,101,active,2020-01-15,appfolio-export\n",
                                              b"Avery Example,101,notice,2022-02-02,appfolio-export\n"
                                              b"Avery Example,101,active,2020-01-15,appfolio-export\n"))
        code, _, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 0)
        self._set(self.target, TARGET + b"Avery Example,101,notice,2022-02-03,appfolio-export\n")
        code, _, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        self.assertEqual(self._rows(report)["Values equal: move_in_date -> Move In"], ("3", "2", "FAIL"))

        # More than five differing keys: the report lists the first five and says so.
        many_source = b"unit,name,move_in_date,status\n" + b"".join(
            f"{n},Name {n},2020-01-15,active\n".encode() for n in range(1, 8))
        many_target = b"Tenant Name,Unit Number,Lease Status,Move In,Source System\n" + b"".join(
            f"Name {n},{n},notice,2020-01-15,appfolio-export\n".encode() for n in range(1, 8))
        self._set(self.source, many_source)
        self._set(self.target, many_target)
        code, _, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        self.assertIn("## Values equal: status -> Lease Status (7 key(s) differ, first 5 shown)", report)
        self.assertEqual(report.count("('"), 5)

    def test_constant_value_mismatch_fail(self):
        # Non-null, same row count, same keys, but one row does not hold the declared constant.
        self._set(self.target, TARGET.replace(b"102,notice,2021-03-01,appfolio-export", b"102,notice,2021-03-01,legacy"))
        code, _, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        rows = self._rows(report)
        self.assertEqual(rows["Non-null: (constant) -> Source System"], ("3", "3", "PASS"))
        self.assertEqual(rows["Values equal: (constant 'appfolio-export') -> Source System"], ("3", "2", "FAIL"))
        self.assertEqual([result for _, _, result in rows.values()].count("FAIL"), 1)
        self.assertIn("## Values equal: (constant 'appfolio-export') -> Source System (1 key(s) differ)", report)
        self.assertIn("('102',)", report)

        # A blank constant cell fails both constant checks.
        self._set(self.target, TARGET.replace(b",appfolio-export\n", b",\n", 1))
        code, _, report, _ = self._invoke(key="unit")
        self.assertEqual(code, 1)
        rows = self._rows(report)
        self.assertEqual(rows["Non-null: (constant) -> Source System"], ("3", "2", "FAIL"))
        self.assertEqual(rows["Values equal: (constant 'appfolio-export') -> Source System"], ("3", "2", "FAIL"))

    def test_reconcile_refuses_altered_target(self):
        # The sidecar still holds map.py's hash; the file changed afterwards. Counts and keys are untouched.
        self.target.write_bytes(TARGET.replace(b"102,notice,", b"102,vacant,"))
        self._assert_refused(*self._invoke(key="unit"), "target.csv does not match its checksum sidecar")

    def test_reconcile_refuses_altered_source(self):
        self.source.write_bytes(SOURCE.replace(b"101,Avery Example,2020-01-15,active", b"101,Avery Example,2020-01-15,notice"))
        self._assert_refused(*self._invoke(key="unit"), "clean.csv does not match its checksum sidecar")

    def test_reconcile_refuses_missing_sidecar(self):
        (self.root / "target.csv.sha256").unlink()
        self._assert_refused(*self._invoke(key="unit"), "Missing checksum sidecar target.csv.sha256")
        _sidecar(self.target, TARGET)
        (self.root / "clean.csv.sha256").unlink()
        self._assert_refused(*self._invoke(key="unit"), "Missing checksum sidecar clean.csv.sha256")
        # A malformed sidecar, or one naming a different file, is refused the same way.
        (self.root / "clean.csv.sha256").write_bytes(b"not a checksum\n")
        self._assert_refused(*self._invoke(key="unit"), "malformed or names a different file")
        (self.root / "clean.csv.sha256").write_bytes(hashlib.sha256(SOURCE).hexdigest().encode() + b"  other.csv\n")
        self._assert_refused(*self._invoke(key="unit"), "malformed or names a different file")

    def test_report_markdown_format(self):
        code, stderr, report, out = self._invoke(key="unit")
        self.assertEqual((code, stderr), (0, ""))
        lines = report.splitlines()
        self.assertEqual(lines[0], "# Reconciliation")
        self.assertEqual(lines[1], "")
        self.assertTrue(lines[2].startswith("- Source: `") and "(3 data rows, sha256 `" in lines[2])
        self.assertTrue(lines[3].startswith("- Target: `") and "(3 data rows, sha256 `" in lines[3])
        self.assertEqual(lines[4], "- Key: unit -> Unit Number")
        self.assertEqual(lines[5], f"- Mapping: `{self.mapping.as_posix()}`")
        self.assertEqual(lines[6], "")
        self.assertEqual(lines[7], "| Check | Expected | Actual | Result |")
        self.assertEqual(lines[8], "|---|---|---|---|")
        table = lines[9:9 + MAPPED_CHECKS]
        for line in table:
            self.assertRegex(line, r"^\| [^|]+ \| \d+ \| \d+ \| (PASS|FAIL) \|$")
        self.assertEqual(lines[9], "| Row count | 3 | 3 | PASS |")
        self.assertEqual(lines[9 + MAPPED_CHECKS], "")
        self.assertEqual(lines[10 + MAPPED_CHECKS], f"Verdict: PASS ({MAPPED_CHECKS} checks, 0 failed)")
        self.assertEqual(len(lines), 11 + MAPPED_CHECKS)
        self.assertTrue(report.endswith("\n"))
        self.assertRegex(report, r"sha256 `[0-9a-f]{64}`")
        self.assertIn(f"sha256 `{hashlib.sha256(SOURCE).hexdigest()}`", lines[2])
        self.assertIn(f"sha256 `{hashlib.sha256(TARGET).hexdigest()}`", lines[3])

        # Pipes in column names are escaped so the table stays a table.
        self._set(self.source, b"a|b,name\n1,x\n")
        self._set(self.target, b"a|b,name\n1,x\n")
        code, stderr, report, _ = self._invoke(key="a|b", mapping=False)
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("| Non-null: a\\|b -> a\\|b | 1 | 1 | PASS |", report)
        self.assertIn("| Values equal: a\\|b -> a\\|b | 1 | 1 | PASS |", report)

        # Malformed input or a mapping that does not fit the files is an invocation error: exit 2, no report.
        self._set(self.target, b'a|b,name\n1,"x\n')
        self._assert_refused(*self._invoke(key="a|b", mapping=False), "Malformed quoting")
        self._set(self.source, SOURCE)
        self._set(self.target, TARGET)
        self.mapping.write_text(MAPPING.replace("Unit Number", "Apt"), encoding="utf-8")
        self._assert_refused(*self._invoke(key="unit"), "does not match the mapping's target_header")


if __name__ == "__main__":
    unittest.main()
