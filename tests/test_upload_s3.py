import hashlib
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import upload_s3

try:  # Optional extras: moto mocks S3 so no AWS account is involved.
    import boto3
    import moto
except ImportError:
    boto3 = moto = None
mock_aws = getattr(moto, "mock_aws", None) or getattr(moto, "mock_s3", None)


ROOT = Path(__file__).resolve().parents[1]
BUCKET = "cutover-fictional-bucket"
KEY = "cutover/run-001/target.csv"
TARGET = (
    b"item_id,rev,item_name,lifecycle_state,created_date,source_system\n"
    b"PN-10005,01,Gasket 1/4 in zinc,active,2013-12-22,legacy-plm\n"
)
FAKE_ENV = {"AWS_DEFAULT_REGION": "us-east-1", "AWS_CONFIG_FILE": "/nonexistent",
            "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent"}


class _RunMixin:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.run_dir = Path(self.temporary.name) / "run"
        self.run_dir.mkdir()
        self.target = self.run_dir / "target.csv"
        self.target.write_bytes(TARGET)
        self.sidecar = self.run_dir / "target.csv.sha256"
        self.sidecar.write_bytes(hashlib.sha256(TARGET).hexdigest().encode("ascii") + b"  target.csv\n")

    def tearDown(self):
        self.temporary.cleanup()

    def _invoke(self, factory, destination=f"s3://{BUCKET}/{KEY}"):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            where = upload_s3.parse_s3_uri(destination)
            code = upload_s3.run(self.target, where, self.run_dir, client_factory=factory)
        report_path = self.run_dir / "upload_report.md"
        report = report_path.read_text(encoding="utf-8") if report_path.exists() else None
        return code, stderr.getvalue(), report

    def _check_pass(self, code, stderr, report, client):
        self.assertEqual((code, stderr), (0, ""))
        digest = hashlib.sha256(TARGET).hexdigest()
        self.assertIn(f"- Bucket: `{BUCKET}`", report)
        self.assertIn(f"- Key: `{KEY}`", report)
        self.assertIn(f"- Size: {len(TARGET)} bytes local, {len(TARGET)} bytes remote", report)
        self.assertIn(f"- Sidecar SHA-256: `{digest}`", report)
        self.assertIn(f"- Local SHA-256: `{digest}`", report)
        self.assertIn(f"- Remote SHA-256: `{digest}`", report)
        self.assertTrue(report.endswith("\nVerdict: PASS\n"))
        self.assertEqual(client.get_object(Bucket=BUCKET, Key=KEY)["Body"].read(), TARGET)
        self.assertEqual(self.target.read_bytes(), TARGET)

        # A second run refuses to overwrite both the local report and the remote object.
        (self.run_dir / "upload_report.md").rename(self.run_dir / "first_report.md")
        code, stderr, report = self._invoke(lambda: client)
        self.assertEqual(code, 2)
        self.assertIn("Refusing to overwrite existing object", stderr)
        self.assertIsNone(report)
        (self.run_dir / "first_report.md").rename(self.run_dir / "upload_report.md")
        code, stderr, report = self._invoke(lambda: client)
        self.assertEqual(code, 2)
        self.assertIn("Refusing to overwrite existing output", stderr)

    def _check_mismatch(self, client, tamper):
        # 1. The local file no longer matches its sidecar: nothing is uploaded.
        self.target.write_bytes(TARGET.replace(b"zinc", b"ZINC"))
        code, stderr, report = self._invoke(lambda: client)
        self.assertEqual(code, 1)
        self.assertIn("nothing was uploaded", stderr)
        self.assertTrue(report.endswith("\nVerdict: FAIL\n") or "Verdict: FAIL" in report)
        self.assertIn("nothing was uploaded", report)
        self.assertIn("- Remote SHA-256: `n/a`", report)
        self.assertFalse(upload_s3._exists(client, upload_s3.S3Location(BUCKET, KEY)))

        # 2. The upload succeeds but what comes back differs: FAIL, report names both hashes.
        self.target.write_bytes(TARGET)
        (self.run_dir / "upload_report.md").unlink()
        with tamper():
            code, stderr, report = self._invoke(lambda: client)
        self.assertEqual(code, 1)
        self.assertIn("remote SHA-256 does not match", stderr)
        self.assertIn(f"- Local SHA-256: `{hashlib.sha256(TARGET).hexdigest()}`", report)
        self.assertRegex(report, r"- Remote SHA-256: `[0-9a-f]{64}`")
        self.assertNotIn(f"- Remote SHA-256: `{hashlib.sha256(TARGET).hexdigest()}`", report)
        self.assertIn("Do not use the remote object", report)
        self.assertIn("\nVerdict: FAIL\n", report)

    def _check_missing_sidecar(self, client):
        self.sidecar.unlink()
        code, stderr, report = self._invoke(lambda: client)
        self.assertEqual(code, 2)
        self.assertIn("Missing checksum sidecar", stderr)
        self.assertIsNone(report)
        self.assertFalse(upload_s3._exists(client, upload_s3.S3Location(BUCKET, KEY)))

        self.sidecar.write_bytes(b"not a checksum\n")
        code, stderr, report = self._invoke(lambda: client)
        self.assertEqual(code, 2)
        self.assertIn("malformed", stderr)
        self.assertIsNone(report)

        for bad in ("http://bucket/key", "s3://", "s3://bucket", "s3://bucket/", "s3://bucket/folder/"):
            with self.subTest(uri=bad):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = upload_s3.main([str(self.target), bad, "--out", str(self.run_dir)])
                self.assertEqual(code, 2)
                self.assertIn("Upload error", stderr.getvalue())


def _tamper_download():
    """Make the verification download return different bytes than were uploaded."""
    original = upload_s3._download

    def corrupted(client, where, destination):
        size = original(client, where, destination)
        destination.write_bytes(destination.read_bytes().replace(b"zinc", b"zinc "))
        return size + 1

    return patch.object(upload_s3, "_download", corrupted)


@unittest.skipUnless(mock_aws, "boto3 and moto[s3] are optional extras; install them to run the mocked S3 tests")
class UploadS3MotoTests(_RunMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, FAKE_ENV)
        self.env.start()
        os.environ.pop("AWS_PROFILE", None)  # patch.dict restores it on stop.
        self.mock = mock_aws()
        self.mock.start()
        self.client = boto3.client("s3", region_name="us-east-1")
        self.client.create_bucket(Bucket=BUCKET)

    def tearDown(self):
        self.mock.stop()
        self.env.stop()
        super().tearDown()

    def test_upload_then_verify_pass(self):
        # Default path: upload_s3 builds its own boto3 client from the (mocked) environment.
        code, stderr, report = self._invoke(None)
        self._check_pass(code, stderr, report, self.client)

    def test_hash_mismatch_fail(self):
        self._check_mismatch(self.client, _tamper_download)

    def test_missing_sidecar_exit_2(self):
        self._check_missing_sidecar(self.client)


class _StubError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _StubBody:
    def __init__(self, content):
        self.stream = io.BytesIO(content)

    def read(self, size=-1):
        return self.stream.read(size)


class StubS3Client:
    """The minimum of the boto3 S3 client surface that upload_s3 uses; keeps tests runnable offline."""

    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _StubError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _StubError("NoSuchKey")
        return {"Body": _StubBody(self.objects[(Bucket, Key)])}


class UploadS3StubTests(_RunMixin, unittest.TestCase):
    """The same three scenarios against an in-memory stub, so the logic is checked without moto."""

    def setUp(self):
        super().setUp()
        self.client = StubS3Client()

    def test_upload_then_verify_pass_stub(self):
        code, stderr, report = self._invoke(lambda: self.client)
        self._check_pass(code, stderr, report, self.client)

    def test_hash_mismatch_fail_stub(self):
        self._check_mismatch(self.client, _tamper_download)

    def test_missing_sidecar_exit_2_stub(self):
        self._check_missing_sidecar(self.client)

    def test_transport_error_exit_2(self):
        class Flaky(StubS3Client):
            def put_object(self, Bucket, Key, Body):
                raise _StubError("AccessDenied")

        code, stderr, report = self._invoke(Flaky)
        self.assertEqual(code, 2)
        self.assertIn("before anything was uploaded", stderr)
        self.assertIsNone(report)

    def test_boto3_missing_exit_2(self):
        with patch.dict(sys.modules, {"boto3": None}):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = upload_s3.main([str(self.target), f"s3://{BUCKET}/{KEY}", "--out", str(self.run_dir)])
        self.assertEqual(code, 2)
        self.assertIn("boto3 is not installed", stderr.getvalue())
        self.assertIn("pip install boto3", stderr.getvalue())
        self.assertFalse((self.run_dir / "upload_report.md").exists())


if __name__ == "__main__":
    unittest.main()
