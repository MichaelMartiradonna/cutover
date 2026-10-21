import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

import validate


ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 18)

LEGACY_RULES = """\
header: [unit, name, move_in_date, status]
required: [unit, name]
dates:
  - field: move_in_date
    max: today
enums:
  - field: status
    allowed: [active, notice, vacant]
unique:
  - [unit, name]
encoding: utf-8-no-bom
"""


class RulesConfigTests(unittest.TestCase):
    def _invoke(self, argv):
        stderr = io.StringIO()
        with patch.object(validate, "date", wraps=date) as local_date, redirect_stderr(stderr):
            local_date.today.return_value = TODAY
            code = validate.main(argv)
        return code, stderr.getvalue()

    def _rejects(self, output):
        with (output / "rejects.csv").open(encoding="utf-8", newline="") as stream:
            return [(int(row["row"]), row["rule"], row["why"]) for row in csv.DictReader(stream)]

    def test_loads_default_rules_matches_legacy_behavior(self):
        rules = validate.load_rules(validate.DEFAULT_RULES)
        self.assertEqual(rules.header, ["unit", "name", "move_in_date", "status"])
        self.assertEqual(rules.required, ["unit", "name"])
        self.assertEqual(rules.dates, [validate.DateRule("move_in_date", "today")])
        self.assertEqual(rules.enums, [validate.EnumRule("status", ["active", "notice", "vacant"])])
        self.assertEqual(rules.unique, [["unit", "name"]])
        self.assertEqual(rules.encoding, "utf-8-no-bom")
        self.assertEqual(validate.parse_rules(__import__("yaml").safe_load(LEGACY_RULES)), rules)

        expected = [
            (3, "R001", "unit is required; it is blank."),
            (4, "R001", "name is required; it is blank."),
            (5, "D001", "move_in_date must be an exact ASCII YYYY-MM-DD valid calendar date."),
            (6, "D002", "move_in_date is after the local run date 2026-10-18."),
            (7, "E001", "status must be exactly active, notice, or vacant."),
            (8, "U001", "Duplicate unit and name; also in CSV record(s): 9."),
            (9, "U001", "Duplicate unit and name; also in CSV record(s): 8."),
        ]
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            for label, argv_extra in (("implicit default", []), ("explicit --rules", ["--rules", str(ROOT / "rules" / "tenants.yaml")])):
                with self.subTest(label=label):
                    output = root / label.replace(" ", "-")
                    code, stderr = self._invoke([str(ROOT / "data" / "fake_tenants.csv"), "--out", str(output), *argv_extra])
                    self.assertEqual((code, stderr), (1, ""))
                    self.assertEqual(self._rejects(output), expected)
                    self.assertEqual({path.name for path in output.iterdir()}, {"rejects.csv"})

            # Legacy wording for file-level and field-count errors is preserved.
            source = root / "short.csv"
            source.write_bytes(b"unit,name,move_in_date,status\n101,Avery Example\n")
            code, stderr = self._invoke([str(source), "--out", str(root / "short")])
            self.assertEqual((code, stderr), (1, ""))
            self.assertEqual(self._rejects(root / "short"), [(2, "F003", "Expected four fields; found 2.")])
            source.write_bytes(b"unit,name\n101,Avery Example\n")
            code, stderr = self._invoke([str(source), "--out", str(root / "header")])
            self.assertEqual((code, stderr), (1, ""))
            self.assertEqual(self._rejects(root / "header"),
                             [(0, "F001", "Header must be exactly unit,name,move_in_date,status.")])

    def _assert_load_fails(self, text, fragment):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            rules_path = root / "rules.yaml"
            rules_path.write_text(text, encoding="utf-8")
            with self.assertRaises(validate.RulesError) as raised:
                validate.load_rules(rules_path)
            self.assertIn(fragment, str(raised.exception))

            # Through the command line the same problem exits 2 before any output is written.
            source = root / "input.csv"
            source.write_bytes((ROOT / "data" / "accepted.csv").read_bytes())
            output = root / "fresh"
            code, stderr = self._invoke([str(source), "--rules", str(rules_path), "--out", str(output)])
            self.assertEqual(code, 2)
            self.assertIn("Rules error", stderr)
            self.assertIn(fragment, stderr)
            self.assertFalse(output.exists())

    def test_unknown_key_exits_2(self):
        self._assert_load_fails(LEGACY_RULES + "transforms: [trim]\n", "unknown key(s): transforms")
        self._assert_load_fails(LEGACY_RULES.replace("required: [unit, name]\n", ""), "missing key(s): required")
        self._assert_load_fails(LEGACY_RULES.replace("max: today", "max: tomorrow"), "max: today")
        self._assert_load_fails(LEGACY_RULES.replace("utf-8-no-bom", "latin-1"), "encoding must be one of")
        self._assert_load_fails(LEGACY_RULES.replace("    max: today\n", "    max: today\n    fill: today\n"), "unknown key(s): fill")
        self._assert_load_fails("- just\n- a list\n", "must be a mapping")
        self._assert_load_fails("header: [unit, name, move_in_date, status\n", "Cannot parse")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            missing = Path(temporary) / "absent.yaml"
            code, stderr = self._invoke([str(ROOT / "data" / "accepted.csv"), "--rules", str(missing), "--out", str(Path(temporary) / "fresh")])
            self.assertEqual(code, 2)
            self.assertIn("Cannot read rules file", stderr)
            self.assertFalse((Path(temporary) / "fresh").exists())

    def test_rule_names_missing_column_exits_2(self):
        self._assert_load_fails(LEGACY_RULES.replace("required: [unit, name]", "required: [unit, tenant]"),
                                "required names column 'tenant', which is not in header")
        self._assert_load_fails(LEGACY_RULES.replace("field: move_in_date", "field: move_out_date"),
                                "dates names column 'move_out_date', which is not in header")
        self._assert_load_fails(LEGACY_RULES.replace("field: status", "field: state"),
                                "enums names column 'state', which is not in header")
        self._assert_load_fails(LEGACY_RULES.replace("- [unit, name]", "- [unit, email]"),
                                "unique names column 'email', which is not in header")
        self._assert_load_fails(LEGACY_RULES.replace("header: [unit, name, move_in_date, status]",
                                                     "header: [unit, name, move_in_date, status, unit]"),
                                "must not repeat a column")

    def test_all_existing_tests_still_pass(self):
        import test_validate

        suite = unittest.defaultTestLoader.loadTestsFromModule(test_validate)
        self.assertEqual(suite.countTestCases(), 3)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(), stream.getvalue())
        self.assertEqual(result.testsRun, 3)


if __name__ == "__main__":
    unittest.main()
