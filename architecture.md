# Cutover architecture

## Shape
- Four local Python scripts, one per stage, run in order into one run directory: `validate.py` -> `map.py` -> `reconcile.py` -> `upload_s3.py` (optional). A human signs off with `RUNBOOK.md`.
- Standard library only for validate, map, and reconcile, plus PyYAML to read `rules/*.yaml` and `mappings/*.yaml`. `boto3` is an optional extra, imported lazily by the upload stage. Nothing else, no services, no database, no scheduling.
- Configuration is strict YAML with fixed keys. Anything unexpected exits 2 at load before any output exists. The code owns the rule IDs (`R001`, `D001`, `D002`, `E001`, `U001`, `F001`-`F003`, `C001`, `I001`); the rules file only says which columns they apply to.
- One UTF-8, comma-delimited CSV of fictional data per run. Example commands: `python validate.py data/examples/parts_legacy_150_reviewed.csv --rules rules/parts.yaml --out output/run-001`, then `python map.py output/run-001/clean.csv --mapping mappings/parts_legacy_to_cloud.yaml --out output/run-001`, then `python reconcile.py output/run-001/clean.csv output/run-001/target.csv --key part_number,revision --mapping mappings/parts_legacy_to_cloud.yaml --out output/run-001`.

## Data flow and trust
- `validate.py` parses the whole input with a strict CSV reader (stops at unreliable record boundaries), applies every rule from `checks.md`, and only then writes. A pass writes `clean.csv` as a byte-for-byte copy of the input and `clean.csv.sha256` over the written bytes.
- `map.py` trusts nothing by name: it recomputes the input's SHA-256 and compares it to the sidecar before mapping, so only the validated snapshot can be mapped. It reorders and renames columns, drops only listed columns, adds only declared string constants, and writes `target.csv` with the same deterministic writer as the reject log, then `target.csv.sha256`.
- `reconcile.py` first verifies both files against their `.sha256` sidecars (a changed file is refused with exit 2 and no report), then pairs columns through the mapping (or by shared name) and compares row count, key sets, per-column non-null counts, and row-by-row value equality joined on the key, including that every constant column holds its declared value. It records both files' SHA-256 in the report so the sign-off block can quote them.
- `upload_s3.py` checks the local file against its sidecar, uploads, downloads to a temporary file, and compares size and SHA-256. The remote object is trusted only after the round trip matches.
- Comparison keys (stripped, casefolded duplicate keys; key tuples in reconcile) are for detection only. No stage writes a transformed value anywhere.

## Outputs (all inside the run directory)
- `rejects.csv`: `row,rule,why`; header only when all checks pass.
- `clean.csv`, `clean.csv.sha256`: the validated snapshot and its checksum (64 lowercase hex, two spaces, file name, LF).
- `target.csv`, `target.csv.sha256`, `mapping_report.csv` (`target_column,source_column,kind,rows,note`; `kind` is `rename`, `constant`, `drop`, or on refusal `error`).
- `reconciliation.md`: check table and `Verdict:` line. `upload_report.md`: hashes, sizes, and `Verdict:` line.

## Fail closed
- Exit 0 means every check in that stage passed and all of its outputs are complete. Exit 1 means the stage rejected or refused: validate writes only `rejects.csv`; map writes only an `error` report; reconcile and upload write a FAIL report. Exit 2 means the run itself failed (invocation, configuration, I/O): use nothing from it.
- Never emit a valid-row subset from a rejected input, and never let a later stage consume an unvalidated or altered file (map and reconcile verify sidecars before reading; upload verifies before uploading).
- After a write failure, the stage attempts to remove its own partial data files (`clean.csv`/checksum, `target.csv`/checksum). Cleanup is best effort and can itself fail, leaving files behind; both the original failure and any cleanup failure go to stderr, and an `I001` row is logged when possible.
- `validate.py` requires a fresh output directory; the later stages create the directory only when they have something to write and refuse to run if any of their own output files already exists. The upload stage also refuses an existing S3 object. Prior output is never overwritten; the source CSV is never modified.
- There is no force flag, no quiet success summary, and no automatic retry. A human reviews the log, edits a separate copy of the input, and reruns into a new run directory.

## File layout
- `validate.py`, `map.py`, `reconcile.py`, `upload_s3.py`: the four stages. `viewer.py`: read-only, single-page viewer of a run directory on `127.0.0.1`; shows clean rows, reject rows, checksums, and, when present, target rows, the mapping report, `reconciliation.md`, and `upload_report.md`. It never runs a stage or changes files.
- `rules/tenants.yaml` (the original five checks as configuration; the default), `rules/parts.yaml`.
- `mappings/tenants_appfolio_to_import.yaml`, `mappings/parts_legacy_to_cloud.yaml`.
- `data/fake_tenants.csv`, `data/accepted.csv`: the original small fixtures. `data/examples/*.csv`: generated 200-row tenant and 150-row parts examples, raw (with faults) and `_reviewed`. `tools/make_examples.py`: the seeded generator; its docstring documents every fault.
- `tests/`: `test_validate.py` (the three original tests, unchanged), `test_rules_config.py`, `test_make_examples.py`, `test_map.py`, `test_reconcile.py`, `test_upload_s3.py` (moto-mocked S3 when installed, plus an in-memory stub that always runs).
- `checks.md`: the reviewed contract per stage. `RUNBOOK.md`: pre-checks, dry run, go/no-go, sign-off, rollback. `README.md`: the five-minute walkthrough and the reference. `requirements.txt`: `pyyaml`, with `boto3` and `moto[s3]` noted as optional extras.
- `internal/`: retained internal planning documents. `output/<run>/`: generated artifacts only, ignored by git.
