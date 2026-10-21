import csv
import hashlib
import io
import re
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import validate


ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 18)


class ValidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = (ROOT / "data" / "fake_tenants.csv").read_bytes()
        cls.rows = list(csv.reader(io.StringIO(cls.fixture.decode("utf-8"), newline="")))
        cls.header = cls.rows[0]
        cls.valid_row = cls.rows[1]
        cls.accepted = (ROOT / "data" / "accepted.csv").read_bytes()

    def _csv(self, rows, ending="\n"):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator=ending)
        writer.writerows(rows)
        return buffer.getvalue().encode("utf-8")

    def _input(self, **changes):
        row = self.valid_row.copy()
        for field, value in changes.items():
            row[self.header.index(field)] = value
        return self._csv([self.header, row])

    def _invoke(self, source, output):
        stderr = io.StringIO()
        with patch.object(validate, "date", wraps=date) as local_date, redirect_stderr(stderr):
            local_date.today.return_value = TODAY
            code = validate.main([str(source), "--out", str(output)])
            local_date.today.assert_called_once_with()
        return code, stderr.getvalue()

    def _rejects(self, output):
        with (output / "rejects.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, ["row", "rule", "why"])
            records = list(reader)
        for record in records:
            self.assertEqual(set(record), {"row", "rule", "why"})
            self.assertTrue(record["why"].strip())
        return records

    def _assert_failure(self, output, expected):
        self.assertEqual({path.name for path in output.iterdir()}, {"rejects.csv"})
        records = self._rejects(output)
        self.assertCountEqual([(int(row["row"]), row["rule"]) for row in records], expected)
        return records

    def test_accepted_input(self):
        preserved_rows = [
            [" 101 ", '  aVeRy "Example",\r\nAvery Example  ', "2020-01-15", "active"],
            ["103", "Avery Example", "2020-01-15", "notice"],
        ]
        cases = [
            ("planned accepted input", self.accepted),
            ("notice", self._input(status="notice")),
            ("vacant", self._input(status="vacant")),
            ("today", self._input(move_in_date=TODAY.isoformat())),
            ("leap day", self._input(move_in_date="2020-02-29")),
            ("values, quotes, multiline, CRLF, order", self._csv([self.header, *preserved_rows], "\r\n")),
            ("no terminal newline", self.accepted.rstrip(b"\r\n")),
            ("embedded double quotes and comma", self._input(name='Avery "Example", Example')),
            ("standalone CR record endings", self._csv([self.header, self.valid_row], "\r")),
        ]
        for label, payload in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                source, output = root / "input.csv", root / "output" / "fresh"
                source.write_bytes(payload)
                code, stderr = self._invoke(source, output)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual({path.name for path in output.iterdir()},
                                 {"clean.csv", "rejects.csv", "clean.csv.sha256"})
                self.assertEqual(source.read_bytes(), payload)
                self.assertEqual((output / "clean.csv").read_bytes(), payload)
                with (output / "clean.csv").open(encoding="utf-8", newline="") as stream:
                    self.assertEqual(list(csv.reader(stream)),
                                     list(csv.reader(io.StringIO(payload.decode("utf-8"), newline=""))))
                self.assertEqual(self._rejects(output), [])
                self.assertEqual((output / "rejects.csv").read_bytes(), b"row,rule,why\n")

    def test_rejected_input(self):
        expected_mixed = [(3, "R001"), (4, "R001"), (5, "D001"), (6, "D002"),
                          (7, "E001"), (8, "U001"), (9, "U001")]
        with self.subTest(label="planned seven rejections"), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source, output = root / "input.csv", root / "fresh"
            source.write_bytes(self.fixture)
            code, stderr = self._invoke(source, output)
            self.assertEqual((code, stderr), (1, ""))
            records = self._assert_failure(output, expected_mixed)
            by_row = {int(record["row"]): record for record in records}
            for row, field in ((3, "unit"), (4, "name"), (5, "move_in_date"),
                               (6, "move_in_date"), (7, "status")):
                self.assertIn(field, by_row[row]["why"])
            self.assertRegex(by_row[8]["why"], r"\b9\b")
            self.assertRegex(by_row[9]["why"], r"\b8\b")
            self.assertEqual(source.read_bytes(), self.fixture)

        cases = [
            ("empty", b"", [(0, "F001")]),
            ("header only", self._csv([self.header]), [(0, "F002")]),
            ("UTF-8 BOM", b"\xef\xbb\xbf" + self.accepted, [(0, "F001")]),
            ("invalid UTF-8", self.accepted + b"\xff", [(0, "F001")]),
            ("missing header", self._csv([self.valid_row]), [(0, "F001")]),
            ("wrong header order", self._csv([list(reversed(self.header)), self.valid_row]), [(0, "F001")]),
            ("wrong delimiter", self.accepted.replace(b",", b";"), [(0, "F001")]),
            ("blank record", self.accepted + b"\n", [(3, "F003")]),
            ("three fields, skip field rules", self._csv([self.header, ["", "", "bad"]]), [(2, "F003")]),
            ("five fields", self._csv([self.header, [*self.valid_row, "extra"]]), [(2, "F003")]),
            ("both required fields", self._input(unit=" \t", name="\u2003"), [(2, "R001"), (2, "R001")]),
            ("quoted empty name", self.accepted.replace(b"Avery Example", b'""'), [(2, "R001")]),
            ("unclosed quote", self.accepted.replace(b"Avery Example", b'"Avery Example'), [(0, "F001")]),
            ("quote in unquoted field", self.accepted.replace(b"Avery Example", b'Avery "Example'), [(0, "F001")]),
            ("text after closing quote", self.accepted.replace(b"Avery Example", b'"Avery"Example'), [(0, "F001")]),
            ("space before opening quote", self.accepted.replace(b"Avery Example", b' "Avery Example"'), [(0, "F001")]),
            ("space after closing quote", self.accepted.replace(b"Avery Example", b'"Avery Example" '), [(0, "F001")]),
            ("malformed header quoting", b'unit,na"me,move_in_date,status\n', [(0, "F001")]),
        ]
        for invalid_date in ("", " 2020-01-15", "2020-01-15 ", "2020-01-15\n", "2020-1-15",
                             "2020-01-15T00:00:00", "２０２０-０１-１５", "2020-01-1٥",
                             "0000-01-15", "2020-02-30", "2021-02-29"):
            cases.append((f"invalid date {invalid_date!r}", self._input(move_in_date=invalid_date), [(2, "D001")]))
        cases.append(("tomorrow", self._input(move_in_date="2026-10-19"), [(2, "D002")]))
        for invalid_status in ("", " ", "Active", "active ", " active", "pending"):
            cases.append((f"invalid status {invalid_status!r}", self._input(status=invalid_status), [(2, "E001")]))

        duplicate_rows = [self.valid_row, [" 101 ", " aVeRy Example ", "2020-01-15", "notice"],
                          ["101", "AVERY EXAMPLE", "2020-01-15", "pending"]]
        cases.append(("every duplicate, independent status error", self._csv([self.header, *duplicate_rows]),
                      [(2, "U001"), (3, "U001"), (4, "U001"), (4, "E001")]))
        blank_names = [self.valid_row.copy(), self.valid_row.copy()]
        for row in blank_names:
            row[1] = " "
        cases.append(("blank names excluded from duplicate detection", self._csv([self.header, *blank_names]),
                      [(2, "R001"), (3, "R001")]))
        cases.append(("continue after wrong field count", self._csv([self.header, ["bad"],
                      ["103", "Avery Example", "invalid", "pending"]]),
                      [(2, "F003"), (3, "D001"), (3, "E001")]))
        multiline_row = self.valid_row.copy()
        multiline_row[1] = "Avery\nExample"
        cases.append(("multiline counts as one record", self._csv([self.header, multiline_row,
                      ["", "Avery Example", "2020-01-15", "active"]]), [(3, "R001")]))
        cases.append(("evaluate safe prefix then stop", self._input(unit="") +
                      b'101,"unclosed,2020-01-15,active\n' + self._csv([self.valid_row]),
                      [(0, "F001"), (2, "R001")]))
        for label, payload, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                source, output = root / "input.csv", root / "fresh"
                source.write_bytes(payload)
                code, stderr = self._invoke(source, output)
                self.assertEqual((code, stderr), (1, ""))
                records = self._assert_failure(output, expected)
                self.assertEqual(source.read_bytes(), payload)
                if label == "every duplicate, independent status error":
                    for record in records:
                        if record["rule"] == "U001":
                            row = int(record["row"])
                            for other in {2, 3, 4} - {row}:
                                self.assertRegex(record["why"], rf"\b{other}\b")
                            self.assertNotRegex(record["why"].split(":", 1)[1], rf"\b{row}\b")

        for label in ("missing input", "directory input"):
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                source, output = root / "input.csv", root / "fresh"
                if label == "directory input":
                    source.mkdir()
                code, stderr = self._invoke(source, output)
                self.assertEqual(code, 2)
                self.assertIn("I001", stderr)
                self._assert_failure(output, [(0, "I001")])

        for populated in (False, True):
            with self.subTest(label="existing output", populated=populated), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                source, output = root / "input.csv", root / "existing"
                source.write_bytes(self.accepted)
                output.mkdir()
                if populated:
                    for name in ("clean.csv", "clean.csv.sha256", "rejects.csv", "keep.txt"):
                        (output / name).write_bytes(b"Prior output must remain unchanged.\r\n")
                before = {path.name: path.read_bytes() for path in output.iterdir()}
                code, stderr = self._invoke(source, output)
                self.assertEqual(code, 2)
                self.assertIn("fresh output directory", stderr)
                self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)
                self.assertEqual(source.read_bytes(), self.accepted)

        # Inject filesystem faults because file permissions vary across machines.
        faults = [
            ("input.csv", "rb", False),
            ("rejects.csv", "xb", False),
            ("rejects.csv", "xb", True),
            ("clean.csv", "xb", False),
            ("clean.csv", "xb", True),
            ("clean.csv", "rb", False),
            ("clean.csv.sha256", "xb", False),
            ("clean.csv.sha256", "xb", True),
        ]
        original_open = Path.open
        for filename, failing_mode, partial in faults:
            with self.subTest(io_file=filename, mode=failing_mode, partial=partial), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                source, output = root / "input.csv", root / "fresh"
                source.write_bytes(self.accepted)

                def faulted_open(path, mode="r", *args, **kwargs):
                    if path.name == filename and mode == failing_mode:
                        if not partial:
                            raise PermissionError("Simulated local read/write failure.")
                        stream = original_open(path, mode, *args, **kwargs)
                        failing_stream = Mock(wraps=stream)

                        def partial_write(content):
                            stream.write(content[:5])
                            raise OSError("Simulated write failure after partial output.")

                        failing_stream.write.side_effect = partial_write
                        return closing(failing_stream)
                    return original_open(path, mode, *args, **kwargs)

                with patch.object(Path, "open", autospec=True, side_effect=faulted_open):
                    code, stderr = self._invoke(source, output)
                self.assertEqual(code, 2)
                self.assertIn("I001", stderr)
                if filename == "rejects.csv" and not partial:
                    self.assertEqual(list(output.iterdir()), [])
                    self.assertIn("Cannot write reject log", stderr)
                else:
                    self._assert_failure(output, [(0, "I001")])
                self.assertFalse((output / "clean.csv").exists())
                self.assertFalse((output / "clean.csv.sha256").exists())
                self.assertEqual(source.read_bytes(), self.accepted)

        with self.subTest(label="I001 checksum write and cleanup failure"), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source, output = root / "input.csv", root / "fresh"
            source.write_bytes(self.accepted)
            original_unlink = Path.unlink

            def checksum_failure(path, mode="r", *args, **kwargs):
                if path == output / "clean.csv.sha256" and mode == "xb":
                    raise OSError("Simulated checksum write failure.")
                return original_open(path, mode, *args, **kwargs)

            def cleanup_failure(path, *args, **kwargs):
                if path == output / "clean.csv":
                    raise PermissionError("Simulated cleanup failure.")
                return original_unlink(path, *args, **kwargs)

            # I001 remains a failed run even when the filesystem prevents cleanup.
            with patch.object(Path, "open", autospec=True, side_effect=checksum_failure), \
                    patch.object(Path, "unlink", autospec=True, side_effect=cleanup_failure):
                code, stderr = self._invoke(source, output)
            self.assertEqual(code, 2)
            self.assertIn("I001", stderr)
            self.assertIn("Simulated checksum write failure", stderr)
            self.assertIn("Cannot remove this run's partial output", stderr)
            self.assertIn("Simulated cleanup failure", stderr)
            self.assertEqual({path.name for path in output.iterdir()}, {"clean.csv", "rejects.csv"})
            self.assertEqual((output / "clean.csv").read_bytes(), self.accepted)
            records = self._rejects(output)
            self.assertEqual([(record["row"], record["rule"]) for record in records], [("0", "I001")])
            self.assertIn("Simulated checksum write failure", records[0]["why"])
            self.assertEqual(source.read_bytes(), self.accepted)

        with self.subTest(label="unwritable output directory"), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source, output = root / "input.csv", root / "fresh"
            source.write_bytes(self.accepted)
            with patch.object(Path, "mkdir", side_effect=PermissionError("Simulated directory creation failure.")):
                code, stderr = self._invoke(source, output)
            self.assertEqual(code, 2)
            self.assertIn("fresh output directory", stderr)
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), self.accepted)

    def test_checksum(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source, output = root / "input.csv", root / "fresh"
            source.write_bytes(self.accepted)
            code, stderr = self._invoke(source, output)
            self.assertEqual((code, stderr), (0, ""))
            final_clean_bytes = (output / "clean.csv").read_bytes()
            checksum = (output / "clean.csv.sha256").read_bytes()
            digest = hashlib.sha256(final_clean_bytes).hexdigest().encode("ascii")
            self.assertIsNotNone(re.fullmatch(rb"[0-9a-f]{64}  clean\.csv\n", checksum))
            self.assertEqual(checksum, digest + b"  clean.csv\n")
            self.assertEqual(final_clean_bytes, self.accepted)
            self.assertEqual(self._rejects(output), [])
            self.assertEqual(source.read_bytes(), self.accepted)

            second_output = root / "second-fresh"
            code, stderr = self._invoke(source, second_output)
            self.assertEqual((code, stderr), (0, ""))
            second_clean_bytes = (second_output / "clean.csv").read_bytes()
            second_checksum = (second_output / "clean.csv.sha256").read_bytes()
            self.assertEqual(second_clean_bytes, self.accepted)
            self.assertEqual(second_checksum, checksum)
            second_digest = hashlib.sha256(second_clean_bytes).hexdigest().encode("ascii")
            self.assertEqual(second_checksum, second_digest + b"  clean.csv\n")
            self.assertEqual(self._rejects(second_output), [])
            self.assertEqual(source.read_bytes(), self.accepted)


if __name__ == "__main__":
    unittest.main()
