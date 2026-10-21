import csv
import hashlib
import importlib
import io
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import validate

cutover_map = importlib.import_module("map")


ROOT = Path(__file__).resolve().parents[1]

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

CLEAN = (
    b"unit,name,move_in_date,status\r\n"
    b" 101 ,\"  aVeRy \"\"Example\"\",\r\nAvery Example  \",2020-01-15,active\r\n"
    b"103,Avery Example,2020-01-15,notice\r\n"
)


class MapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.clean = self.run_dir / "clean.csv"
        self.mapping_path = self.root / "mapping.yaml"
        self.mapping_path.write_text(MAPPING, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_clean(self, content=CLEAN, hashed=True):
        self.clean.write_bytes(content)
        if hashed:
            digest = hashlib.sha256(content).hexdigest()
            (self.run_dir / "clean.csv.sha256").write_bytes(f"{digest}  clean.csv\n".encode("ascii"))

    def _invoke(self, mapping=None, out=None):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cutover_map.main([str(self.clean), "--mapping", str(mapping or self.mapping_path),
                                     "--out", str(out or self.run_dir)])
        return code, stderr.getvalue()

    def _report(self):
        with (self.run_dir / "mapping_report.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, ["target_column", "source_column", "kind", "rows", "note"])
            return list(reader)

    def _assert_rejected(self, code, stderr):
        self.assertEqual(code, 1)
        self.assertIn("Mapping rejected", stderr)
        self.assertEqual({path.name for path in self.run_dir.iterdir()} - {"clean.csv", "clean.csv.sha256"},
                         {"mapping_report.csv"})
        report = self._report()
        self.assertTrue(report)
        self.assertEqual({row["kind"] for row in report}, {"error"})
        return report

    def test_rename_and_reorder(self):
        self._write_clean()
        code, stderr = self._invoke()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual({path.name for path in self.run_dir.iterdir()},
                         {"clean.csv", "clean.csv.sha256", "target.csv", "target.csv.sha256", "mapping_report.csv"})
        target = (self.run_dir / "target.csv").read_bytes()
        expected = (
            b"Tenant Name,Unit Number,Lease Status,Move In,Source System\n"
            b"\"  aVeRy \"\"Example\"\",\r\nAvery Example  \", 101 ,active,2020-01-15,appfolio-export\n"
            b"Avery Example,103,notice,2020-01-15,appfolio-export\n"
        )
        # Values are copied exactly: surrounding spaces, case, embedded quotes, and newlines survive.
        self.assertEqual(target, expected)
        source_rows = list(csv.reader(io.StringIO(CLEAN.decode("utf-8"), newline="")))
        target_rows = list(csv.reader(io.StringIO(target.decode("utf-8"), newline="")))
        for source_row, target_row in zip(source_rows[1:], target_rows[1:]):
            self.assertEqual(target_row, [source_row[1], source_row[0], source_row[3], source_row[2], "appfolio-export"])
        self.assertEqual(self.clean.read_bytes(), CLEAN)
        self.assertEqual(self._report(), [
            {"target_column": "Tenant Name", "source_column": "name", "kind": "rename", "rows": "2", "note": ""},
            {"target_column": "Unit Number", "source_column": "unit", "kind": "rename", "rows": "2", "note": ""},
            {"target_column": "Lease Status", "source_column": "status", "kind": "rename", "rows": "2", "note": ""},
            {"target_column": "Move In", "source_column": "move_in_date", "kind": "rename", "rows": "2", "note": ""},
            {"target_column": "Source System", "source_column": "", "kind": "constant", "rows": "2", "note": ""},
        ])

        # A second run into the same directory refuses to overwrite and changes nothing.
        before = {path.name: path.read_bytes() for path in self.run_dir.iterdir()}
        code, stderr = self._invoke()
        self.assertEqual(code, 2)
        self.assertIn("Refusing to overwrite", stderr)
        self.assertEqual({path.name: path.read_bytes() for path in self.run_dir.iterdir()}, before)

    def test_refuses_unhashed_input(self):
        self._write_clean(hashed=False)
        report = self._assert_rejected(*self._invoke())
        self.assertIn("Missing checksum sidecar", report[0]["note"])

        (self.run_dir / "mapping_report.csv").unlink()
        (self.run_dir / "clean.csv.sha256").write_bytes(b"0" * 64 + b"  clean.csv\n")
        report = self._assert_rejected(*self._invoke())
        self.assertIn("does not match its checksum sidecar", report[0]["note"])

        (self.run_dir / "mapping_report.csv").unlink()
        (self.run_dir / "clean.csv.sha256").write_bytes(hashlib.sha256(CLEAN).hexdigest().encode() + b"  other.csv\n")
        report = self._assert_rejected(*self._invoke())
        self.assertIn("malformed or names a different file", report[0]["note"])

    def test_refuses_unmapped_source_column(self):
        self._write_clean()
        self.mapping_path.write_text(MAPPING.replace("  - {from: status, to: Lease Status}\n", "")
                                     .replace("Lease Status, ", ""), encoding="utf-8")
        report = self._assert_rejected(*self._invoke())
        self.assertEqual([(row["target_column"], row["source_column"]) for row in report], [("", "status")])
        self.assertIn("not mapped and not listed under drop", report[0]["note"])

        # Listing it under drop: is the explicit way to leave a column behind.
        (self.run_dir / "mapping_report.csv").unlink()
        self.mapping_path.write_text(self.mapping_path.read_text(encoding="utf-8") + "drop: [status]\n", encoding="utf-8")
        code, stderr = self._invoke()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual((self.run_dir / "target.csv").read_bytes().splitlines()[0],
                         b"Tenant Name,Unit Number,Move In,Source System")
        self.assertIn({"target_column": "", "source_column": "status", "kind": "drop", "rows": "2", "note": ""}, self._report())

    def test_refuses_target_without_source(self):
        self._write_clean()
        self.mapping_path.write_text(MAPPING.replace("Move In, ", "Move In, Balance, "), encoding="utf-8")
        report = self._assert_rejected(*self._invoke())
        self.assertEqual([(row["target_column"], row["source_column"]) for row in report], [("Balance", "")])
        self.assertIn("has no source column and no constant", report[0]["note"])

    def test_constant_column(self):
        self._write_clean()
        code, stderr = self._invoke()
        self.assertEqual((code, stderr), (0, ""))
        with (self.run_dir / "target.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["Source System"] for row in rows], ["appfolio-export", "appfolio-export"])

        # A constant must be a quoted string, must land in the target header, and may not double-assign a column.
        for broken, fragment in (
            (MAPPING.replace('value: "appfolio-export"', "value: 2024"), "quoted string value"),
            (MAPPING.replace("to: Source System, value", "to: Origin, value"), "not in target_header"),
            (MAPPING.replace("to: Source System, value", "to: Move In, value"), "assigned more than once"),
            (MAPPING + "transform: [trim]\n", "unknown key(s): transform"),
            (MAPPING.replace("{from: name, to: Tenant Name}", "{from: tenant, to: Tenant Name}"), "not in source_header"),
        ):
            with self.subTest(fragment=fragment):
                self.mapping_path.write_text(broken, encoding="utf-8")
                out = self.root / ("fresh-" + re.sub(r"\W", "", fragment))
                code, stderr = self._invoke(out=out)
                self.assertEqual(code, 2)
                self.assertIn("Mapping error", stderr)
                self.assertIn(fragment, stderr)
                self.assertFalse(out.exists())

    def test_output_hash_matches(self):
        self._write_clean()
        code, stderr = self._invoke()
        self.assertEqual((code, stderr), (0, ""))
        target = (self.run_dir / "target.csv").read_bytes()
        checksum = (self.run_dir / "target.csv.sha256").read_bytes()
        self.assertIsNotNone(re.fullmatch(rb"[0-9a-f]{64}  target\.csv\n", checksum))
        self.assertEqual(checksum, hashlib.sha256(target).hexdigest().encode("ascii") + b"  target.csv\n")

        # Byte-stable: the same input and mapping produce the same target bytes and hash.
        second = self.root / "second"
        code, stderr = self._invoke(out=second)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual((second / "target.csv").read_bytes(), target)
        self.assertEqual((second / "target.csv.sha256").read_bytes(), checksum)

        # The written target feeds the next stage: map accepts its own output as a hashed input.
        mapping2 = self.root / "identity.yaml"
        mapping2.write_text(
            "source_header: [Tenant Name, Unit Number, Lease Status, Move In, Source System]\n"
            "target_header: [Unit Number, Tenant Name]\n"
            "columns:\n  - {from: Unit Number, to: Unit Number}\n  - {from: Tenant Name, to: Tenant Name}\n"
            "drop: [Lease Status, Move In, Source System]\n", encoding="utf-8")
        third = self.root / "third"
        stderr_io = io.StringIO()
        with redirect_stderr(stderr_io):
            code = cutover_map.main([str(second / "target.csv"), "--mapping", str(mapping2), "--out", str(third)])
        self.assertEqual((code, stderr_io.getvalue()), (0, ""))
        self.assertEqual((third / "target.csv").read_bytes().splitlines()[0], b"Unit Number,Tenant Name")

    def test_shipped_mappings_load(self):
        for name in ("tenants_appfolio_to_import.yaml", "parts_legacy_to_cloud.yaml"):
            with self.subTest(name=name):
                mapping = cutover_map.load_mapping(ROOT / "mappings" / name)
                self.assertEqual(cutover_map.mapping_problems(mapping), [])
        tenants = validate.load_rules(ROOT / "rules" / "tenants.yaml")
        self.assertEqual(cutover_map.load_mapping(ROOT / "mappings" / "tenants_appfolio_to_import.yaml").source_header,
                         tenants.header)
        parts = validate.load_rules(ROOT / "rules" / "parts.yaml")
        self.assertEqual(cutover_map.load_mapping(ROOT / "mappings" / "parts_legacy_to_cloud.yaml").source_header,
                         parts.header)


if __name__ == "__main__":
    unittest.main()
