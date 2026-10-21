import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

import validate
from tools import make_examples


ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 18)


class MakeExamplesTests(unittest.TestCase):
    def test_generator_is_deterministic(self):
        first = make_examples.generate()
        second = make_examples.generate()
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"tenants_appfolio_200.csv", "tenants_appfolio_200_reviewed.csv",
                                      "parts_legacy_150.csv", "parts_legacy_150_reviewed.csv"})
        for name, content in first.items():
            with self.subTest(name=name):
                # The committed files are exactly what the generator produces.
                self.assertEqual((ROOT / "data" / "examples" / name).read_bytes(), content)
                rows = list(csv.reader(io.StringIO(content.decode("utf-8"), newline="")))
                self.assertEqual(len(rows) - 1, 200 if name.startswith("tenants") else 150)
        self.assertNotEqual(make_examples.generate(seed=1), first)

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            out = Path(temporary) / "examples"
            self.assertEqual(make_examples.main(["--out", str(out)]), 0)
            self.assertEqual({path.name: path.read_bytes() for path in out.iterdir()}, first)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(make_examples.main(["--out", str(out)]), 2)
            self.assertIn("Refusing to overwrite", stderr.getvalue())

    def test_documented_reject_counts(self):
        expected = {
            "tenants_appfolio_200.csv": ("tenants.yaml", {"R001": 2, "D001": 2, "D002": 1, "E001": 2, "U001": 4, "F003": 1}),
            "parts_legacy_150.csv": ("parts.yaml", {"R001": 3, "D001": 2, "D002": 1, "E001": 2, "U001": 2}),
        }
        for name, (rules, counts) in expected.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                output = Path(temporary) / "run"
                stderr = io.StringIO()
                with patch.object(validate, "date", wraps=date) as local_date, redirect_stderr(stderr):
                    local_date.today.return_value = TODAY
                    code = validate.main([str(ROOT / "data" / "examples" / name), "--rules",
                                          str(ROOT / "rules" / rules), "--out", str(output)])
                self.assertEqual((code, stderr.getvalue()), (1, ""))
                with (output / "rejects.csv").open(encoding="utf-8", newline="") as stream:
                    rules_seen = [row["rule"] for row in csv.DictReader(stream)]
                self.assertEqual({rule: rules_seen.count(rule) for rule in set(rules_seen)}, counts)
                self.assertEqual(len(rules_seen), sum(counts.values()))

                reviewed = Path(temporary) / "reviewed"
                with patch.object(validate, "date", wraps=date) as local_date, redirect_stderr(stderr):
                    local_date.today.return_value = TODAY
                    code = validate.main([str(ROOT / "data" / "examples" / name.replace(".csv", "_reviewed.csv")),
                                          "--rules", str(ROOT / "rules" / rules), "--out", str(reviewed)])
                self.assertEqual((code, stderr.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
