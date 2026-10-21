"""Upload target.csv to S3, download it again, and verify its SHA-256 against the sidecar; mismatch exits 1.

Optional stage. `boto3` is imported only when this script runs; without it the
script exits 2 with an install hint. Credentials and region come from the
environment or an AWS profile (the normal boto3 lookup chain). Nothing in this
repository holds or reads a credential file.
"""

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from map import MapRejected, read_sidecar


CHUNK = 1024 * 1024


class S3Location(NamedTuple):
    bucket: str
    key: str


class UploadError(RuntimeError):
    """Invocation, dependency, or transport failure: the upload is not verified. Exit 2."""


def parse_s3_uri(uri: str) -> S3Location:
    if not uri.startswith("s3://"):
        raise UploadError(f"Destination must look like s3://BUCKET/KEY, got {uri!r}.")
    bucket, _, key = uri[len("s3://"):].partition("/")
    if not bucket or not key or key.endswith("/"):
        raise UploadError(f"Destination must name a bucket and an object key, got {uri!r}.")
    return S3Location(bucket, key)


def _client(profile: str | None, region: str | None):
    try:
        import boto3
    except ImportError as error:
        raise UploadError(f"boto3 is not installed ({error}). Install the optional extra with: pip install boto3") from error
    session = boto3.session.Session(profile_name=profile, region_name=region)
    return session.client("s3")


def _error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _exists(client, where: S3Location) -> bool:
    try:
        client.head_object(Bucket=where.bucket, Key=where.key)
    except Exception as error:  # botocore.exceptions.ClientError; boto3 may be absent at import time.
        if _error_code(error) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def _upload(client, where: S3Location, content: bytes) -> None:
    client.put_object(Bucket=where.bucket, Key=where.key, Body=content)


def _download(client, where: S3Location, destination: Path) -> int:
    """Stream the remote object into destination; return the number of bytes written."""
    body = client.get_object(Bucket=where.bucket, Key=where.key)["Body"]
    size = 0
    with destination.open("wb") as stream:
        while True:
            chunk = body.read(CHUNK)
            if not chunk:
                break
            stream.write(chunk)
            size += len(chunk)
    return size


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render(where: S3Location, source: Path, local_size: int, remote_size: int | None, sidecar: str,
           local: str, remote: str | None, verdict: str, note: str) -> str:
    lines = [
        "# Upload report",
        "",
        f"- Bucket: `{where.bucket}`",
        f"- Key: `{where.key}`",
        f"- Local file: `{source.as_posix()}`",
        f"- Size: {local_size} bytes local, {remote_size if remote_size is not None else 'n/a'} bytes remote",
        f"- Sidecar SHA-256: `{sidecar}`",
        f"- Local SHA-256: `{local}`",
        f"- Remote SHA-256: `{remote if remote is not None else 'n/a'}`",
        "",
        f"Verdict: {verdict}",
    ]
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    return "\n".join(lines)


def run(source: Path, where: S3Location, output: Path, profile: str | None = None, region: str | None = None,
        client_factory=None) -> int:
    report_path = output / "upload_report.md"
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"I001: Cannot create output directory {output}: {error}", file=sys.stderr)
        return 2
    if report_path.exists():
        print(f"I001: Refusing to overwrite existing output in {output}: {report_path.name}", file=sys.stderr)
        return 2
    try:
        sidecar_digest = read_sidecar(source)
        content = source.read_bytes()
    except MapRejected as error:
        print(f"Upload error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"I001: Cannot read {source}: {error}", file=sys.stderr)
        return 2
    local_digest = hashlib.sha256(content).hexdigest()

    def write_report(remote_size, remote_digest, verdict, note) -> bool:
        try:
            with report_path.open("xb") as stream:
                stream.write(render(where, source, len(content), remote_size, sidecar_digest,
                                    local_digest, remote_digest, verdict, note).encode("utf-8"))
            return True
        except OSError as error:
            print(f"I001: Cannot write {report_path}: {error}", file=sys.stderr)
            return False

    if local_digest != sidecar_digest:
        write_report(None, None, "FAIL", "The local file does not match its checksum sidecar; nothing was uploaded.")
        print(f"Upload FAIL: {source.name} does not match {source.name}.sha256; nothing was uploaded. See {report_path}.",
              file=sys.stderr)
        return 1

    uploaded = False
    try:
        client = client_factory() if client_factory else _client(profile, region)
        if _exists(client, where):
            raise UploadError(f"Refusing to overwrite existing object s3://{where.bucket}/{where.key}.")
        _upload(client, where, content)
        uploaded = True
        with tempfile.TemporaryDirectory() as temporary:
            downloaded = Path(temporary) / "downloaded.csv"
            remote_size = _download(client, where, downloaded)
            remote_digest = _sha256_file(downloaded)
    except UploadError as error:
        print(f"Upload error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # boto3/botocore transport, credential, or permission errors.
        state = "after the object was uploaded; it is unverified" if uploaded else "before anything was uploaded"
        print(f"Upload error ({type(error).__name__}) {state}: {error}", file=sys.stderr)
        return 2

    if remote_digest == sidecar_digest and remote_size == len(content):
        if not write_report(remote_size, remote_digest, "PASS", ""):
            return 2
        return 0
    write_report(remote_size, remote_digest, "FAIL",
                 "The downloaded object does not match the sidecar checksum. Do not use the remote object; "
                 "delete it, then rerun this stage.")
    print(f"Upload FAIL: remote SHA-256 does not match {source.name}.sha256. See {report_path}.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_csv", type=Path, help="Mapped target CSV; its .sha256 sidecar must sit next to it.")
    parser.add_argument("destination", help="s3://BUCKET/KEY (the object must not already exist).")
    parser.add_argument("--out", type=Path, required=True, help="Run directory; upload_report.md must not exist there yet.")
    parser.add_argument("--profile", help="AWS profile name; otherwise the environment is used.")
    parser.add_argument("--region", help="AWS region; otherwise the environment or profile decides.")
    args = parser.parse_args(argv)
    try:
        where = parse_s3_uri(args.destination)
    except UploadError as error:
        print(f"Upload error: {error}", file=sys.stderr)
        return 2
    return run(args.target_csv, where, args.out, args.profile, args.region)


if __name__ == "__main__":
    sys.exit(main())
