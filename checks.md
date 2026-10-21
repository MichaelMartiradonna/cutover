# Cutover checks

Each stage is one command, fail-closed, writing into the run directory. Rule IDs are fixed in the code; the rules file only says which columns they apply to.

## Stage 1: validate (`validate.py SOURCE.csv --rules rules/<name>.yaml --out RUN`)

### Rules file contract
- Exactly these keys: `header`, `required`, `dates`, `enums`, `unique`, `encoding`. Unknown or missing keys exit 2 at load, before the output directory exists.
- `header`: the exact, ordered column list; no repeats. Every column named by another rule must be in it, or load exits 2.
- `dates` entries are `{field, max: today}`; `enums` entries are `{field, allowed: [..]}` with at least one allowed value; `unique` is a list of column lists; `encoding` is `utf-8-no-bom`. Any other value exits 2. Never guess.
- With no `--rules`, `rules/tenants.yaml` is used: header `unit,name,move_in_date,status`, required unit and name, date `move_in_date`, enum `status` in `active|notice|vacant`, unique `(unit, name)`.

### CSV contract
- UTF-8 without BOM; comma-delimited; standard double-quote quoting.
- Header must be exactly the rules file's `header`, in that order.
- `F001`: reject invalid encoding, BOM, missing/wrong header, or malformed quoting; never guess columns. Stop when record boundaries are unreliable.
- `F002`: reject a header-only file. `F003`: reject any record whose field count differs from the header's; skip its field checks.
- Number CSV records from 1 (header); quoted multiline records count once. File errors use row 0.

### Row rules
- `R001`: each `required` column must be nonblank. Empty or whitespace-only values fail; log each missing field.
- `D001`: each `dates` column must be exactly ASCII `YYYY-MM-DD` and parse as a valid calendar date. Blank values, surrounding whitespace, timestamps, and impossible dates fail.
- `D002`: a parsed date must be on or before the machine's local calendar date captured once at run start. Today passes; future dates fail. Skip this check if parsing fails.
- `E001`: each `enums` column must exactly equal one of its `allowed` values. Blank values, other case, surrounding whitespace, and other values fail.
- `U001`: for each `unique` column list, when every member is nonblank, compare the tuple of `value.strip().casefold()`. Reject every member of a duplicate group and identify the other record numbers.
- Duplicate comparison normalization is detection only. Preserve accepted values and order; there is no name-formatting check.

### Run and outputs
- Report every safely evaluable violation as one record in `rejects.csv`, header exactly `row,rule,why`; `why` explains the field failure or conflicting rows.
- Any validation failure exits 1 and produces only the reject log: no clean CSV, no checksum, no partial handoff.
- A passing run exits 0 and writes `clean.csv`, a header-only `rejects.csv`, and `clean.csv.sha256`.
- `C001`: hash the final clean file bytes with SHA-256; checksum text is 64 lowercase hex characters, two spaces, `clean.csv`, and one LF.
- Use a new local output directory for each run; never overwrite prior output. Source bytes stay unchanged.
- `I001`: read/write failure exits 2; log row 0 if possible, otherwise report a clear console error. Cleanup after a write failure is best effort and can itself fail, leaving a clean file or checksum behind. Exit 2 means the run failed; do not use any leftover files from that run.
- Reject, do not repair: no trimming output, recasing, filling fields, date reinterpretation, enum mapping, deduplication, or force-pass option. Human review resolves input before rerunning.

## Stage 2: map (`map.py RUN/clean.csv --mapping mappings/<name>.yaml --out RUN`)

- Mapping file keys: `source_header`, `target_header`, `columns` (each `{from, to}`), optional `constants` (each `{to, value}`, value a quoted string), optional `drop`. Unknown keys, wrong types, a `from` not in `source_header`, a `to` not in `target_header`, a target assigned twice, or a column both mapped and dropped exit 2 at load.
- Input gate: the input's `.sha256` sidecar must exist, be well formed, name the input file, and match the input bytes. Otherwise exit 1 with an `error` row; the input is not the validated clean file.
- Completeness gate: every source column is mapped or listed under `drop`; every target column has exactly one source or one constant. Otherwise exit 1 with one `error` row per problem.
- The input header must equal `source_header`; every record must have the header's field count; malformed CSV is refused. Exit 1 with an `error` row.
- `target.csv`: columns in `target_header` order, values copied unchanged from the source record or the declared constant, UTF-8 without BOM, LF line endings, minimal double-quote quoting (the same writer as `rejects.csv`). Byte-stable for the same input and mapping.
- `target.csv.sha256`: `C001` format with the name `target.csv`.
- `mapping_report.csv`: header `target_column,source_column,kind,rows,note`; one `rename` or `constant` row per target column, one `drop` row per dropped column, `rows` = data-row count, `note` empty. On refusal: `error` rows with the reason in `note` and no `target.csv`.
- Refuses to run if `target.csv`, `target.csv.sha256`, or `mapping_report.csv` already exists (exit 2). `I001` handling as in stage 1: partial `target.csv`/checksum removed best effort, error row written, exit 2.

## Stage 3: reconcile (`reconcile.py RUN/clean.csv RUN/target.csv --key COL[,COL] [--mapping ...] --out RUN`)

- Input gate: both files must have a `.sha256` sidecar next to them that is well formed, names the file, and matches the file's bytes. A missing, malformed, or mismatching sidecar is exit 2 with no `reconciliation.md` and no run directory created: a file that changed after its stage wrote it is not compared at all, so a PASS can never be produced for tampered input.
- Columns are paired through the mapping (`columns` and `constants`) when `--mapping` is given; the file headers must then equal `source_header` and `target_header`. Without a mapping, columns pair by shared name; no shared column is an error.
- `--key` names one or more source columns; each must exist in the source and have a paired target column, or exit 2.
- Checks, each PASS or FAIL: data-row count; distinct key count; keys missing from the target (expected 0); keys not in the source (expected 0); per paired column, the non-null count (non-null means the value is not the empty string; no trimming) and `Values equal`: rows joined on the key, the source value and the target value must be byte-identical (expected = joined keys, actual = keys whose values agree; a repeated key compares the sorted multiset of its values); per constant, the non-null count against the source row count and `Values equal`: every target row must hold exactly the declared constant string, expected = source row count.
- `reconciliation.md`: source and target paths with row counts and SHA-256, key and mapping, a table of every check with expected, actual, PASS/FAIL, up to 20 missing or unexpected keys, the first 5 differing keys per failed `Values equal` check, and a final `Verdict: PASS|FAIL (n checks, m failed)` line.
- Any FAIL exits 1; the report is still written. Malformed files, a bad key, or a mapping that does not fit exit 2 with no report. Refuses to overwrite an existing `reconciliation.md`.

## Stage 4: upload (`upload_s3.py RUN/target.csv s3://BUCKET/KEY --out RUN`, optional)

- `boto3` is imported only when the stage runs; missing exits 2 with an install hint. Credentials and region come from the environment or an AWS profile only.
- `target.csv.sha256` must exist and be well formed, or exit 2. The local file must match it before anything is uploaded; otherwise exit 1 with a FAIL report and nothing uploaded.
- An existing object at the key is never overwritten (exit 2). Upload, then download to a temporary file, then compare size and SHA-256 with the sidecar.
- `upload_report.md`: bucket, key, local path, local and remote sizes, sidecar, local and remote SHA-256, and `Verdict: PASS|FAIL`. A mismatch exits 1 and the report says not to use the remote object. Transport, credential, or permission errors exit 2 and the console says whether the object had already been uploaded. Refuses to overwrite an existing `upload_report.md`.

## Across stages
- Exit codes everywhere: 0 pass, 1 rejected/refused/mismatch, 2 invocation, configuration, or I/O failure.
- No stage transforms, trims, recases, fills, dedupes, or reinterprets a value. The mapping has no expressions.
- Every stage refuses to overwrite its own output files; validate refuses an existing directory.
- The read-only viewer displays a run; it never verifies, approves, or changes anything.
