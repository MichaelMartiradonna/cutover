"""Read-only local viewer for one existing Cutover run directory (validate, map, reconcile, upload)."""

import argparse
import csv
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def csv_table(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            records = list(csv.reader(stream, strict=True))
    except FileNotFoundError:
        return f'<p class="note">No {escape(path.name)} in this run.</p>'
    except (OSError, UnicodeError, csv.Error) as error:
        return f'<p class="note">Cannot read {escape(path.name)}: {escape(str(error))}</p>'
    if not records:
        return '<p class="note">File is empty.</p>'
    header, *rows = records
    headings = "".join(f'<th scope="col">{escape(value)}</th>' for value in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    table = f'<div class="table-wrap"><table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>'
    return table + ('<p class="note">No rows; header only.</p>' if not rows else "")


def text_block(path: Path, encoding: str = "utf-8") -> str:
    try:
        return "<pre>" + escape(path.read_text(encoding=encoding)) + "</pre>"
    except FileNotFoundError:
        return f'<p class="note">No {escape(path.name)} in this run.</p>'
    except (OSError, UnicodeError) as error:
        return f'<p class="note">Cannot read {escape(path.name)}: {escape(str(error))}</p>'


def render(output: Path) -> bytes:
    checksum = text_block(output / "clean.csv.sha256", "ascii")
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cutover | Local viewer</title>
<style>
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #20332d; background: #f5f6f2; }
body { margin: 0; padding: 40px 24px; }
main { max-width: 1100px; margin: auto; }
header { border-bottom: 2px solid #315c48; padding-bottom: 20px; }
h1 { font-size: 30px; margin: 0 0 12px; font-weight: 650; }
h2 { font-size: 20px; margin: 0 0 12px; }
p { line-height: 1.6; }
.eyebrow { text-transform: uppercase; letter-spacing: .1em; font-size: 12px; color: #496252; }
.path { font-family: monospace; overflow-wrap: anywhere; font-size: 13px; }
.note { color: #536159; font-size: 14px; }
section { padding: 24px 0; border-bottom: 1px solid #ccd5cd; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: #fff; text-align: left; }
th { background: #e5ece5; font-size: 13px; }
th, td { padding: 12px 14px; border-bottom: 1px solid #dce2dc; vertical-align: top; }
td { white-space: pre-wrap; overflow-wrap: anywhere; }
pre { padding: 16px; background: #e5ece5; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 600px) { body { padding: 24px 16px; } th, td { padding: 10px; } }
</style></head><body><main>
<header><p class="eyebrow">Read-only · Local only</p><h1>Cutover viewer</h1>
<p>One run's clean rows, reject rows, stored checksums, and (when present) its mapped target, reconciliation, and upload report.</p>
"""
    page += f'<p class="path">{escape(str(output))}</p></header>'
    # Leftover files can survive exit 2; displaying them must not imply approval.
    page += '<p class="note">Viewer only. Use a handoff only after every stage exited 0 and the checksums were verified. Files left by an exit 2 run must not be used.</p>'
    page += '<section aria-labelledby="clean"><h2 id="clean">Clean rows</h2>' + csv_table(output / "clean.csv") + "</section>"
    page += '<section aria-labelledby="rejects"><h2 id="rejects">Reject rows</h2>' + csv_table(output / "rejects.csv") + "</section>"
    page += '<section aria-labelledby="checksum"><h2 id="checksum">Checksum</h2><p class="note">Stored value; this viewer does not verify it.</p>' + checksum + "</section>"
    # Later pipeline stages write into the same run directory; show their files only when they exist.
    if (output / "target.csv").exists() or (output / "mapping_report.csv").exists():
        page += '<section aria-labelledby="target"><h2 id="target">Mapped target rows</h2>' + csv_table(output / "target.csv")
        page += '<h2 id="mapping">Mapping report</h2>' + csv_table(output / "mapping_report.csv")
        page += '<h2 id="target-checksum">Target checksum</h2><p class="note">Stored value; this viewer does not verify it.</p>'
        page += text_block(output / "target.csv.sha256", "ascii") + "</section>"
    if (output / "reconciliation.md").exists():
        page += '<section aria-labelledby="reconcile"><h2 id="reconcile">Reconciliation</h2><p class="note">reconciliation.md as written by reconcile.py; the Verdict line decides.</p>'
        page += text_block(output / "reconciliation.md") + "</section>"
    if (output / "upload_report.md").exists():
        page += '<section aria-labelledby="upload"><h2 id="upload">Upload report</h2><p class="note">upload_report.md as written by upload_s3.py; the Verdict line decides.</p>'
        page += text_block(output / "upload_report.md") + "</section>"
    page += '<p class="note">Refresh to reread this directory. Stop the viewer with Ctrl+C in the terminal.</p></main></body></html>'
    return page.encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path, help="Existing run directory")
    args = parser.parse_args()
    output = args.output_directory.resolve()
    if not output.is_dir():
        parser.error("Choose an existing output directory.")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Serve only the viewer, never a directory listing or arbitrary files.
            if self.path != "/":
                self.send_error(404)
                return
            content = render(output)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        print(f"Cutover local viewer: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
