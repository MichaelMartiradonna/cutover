# Cutover runbook

One page. Print it, fill in the sign-off block, keep it with the run directory. The rule throughout: a human decides, the scripts only refuse. Nothing moves without a checksum.

## 1. Pre-checks (before the dry run)

- [ ] **Backup.** A copy of the legacy export exists outside the run directory, with its SHA-256 recorded below. The source file is never edited in place.
- [ ] **Freeze the source.** The export is final: no more edits in the legacy system until the cutover is signed off or rolled back. Note the export timestamp.
- [ ] **Agree the rules.** The customer has reviewed `rules/<name>.yaml`: the exact header, which columns are required, which are dates, the allowed enum values, and the unique key. A rule nobody has agreed to is not a rule; do not loosen one to make the file pass.
- [ ] **Agree the mapping.** The customer has reviewed `mappings/<name>.yaml`: every source column is either mapped or listed under `drop:`, every target column has a source or a declared constant. No transforms exist, so if a value needs changing it is changed in the legacy system or in a reviewed copy of the export, by a person, before validation.
- [ ] **Target is empty.** The S3 key (or the destination import folder) does not already hold an object with this name; the upload stage refuses to overwrite, so decide the key now. Scope: this toolkit proves the file handoff up to the import boundary; what the receiving system does after it imports `target.csv` (the import job, its own validation, post-import reconciliation against its tables) is outside this runbook and follows the target system's own procedure, recorded separately.
- [ ] **Environment.** Python 3.10+, `pip install pyyaml`, and for the upload stage `pip install boto3` with credentials in the environment or an AWS profile. No credentials in the repository, ever.

## 2. Dry run

Run the whole chain on the export into a fresh run directory and read every output. Expect the first attempt to be rejected; that is the point.

```powershell
$run = "output/cutover-$(Get-Date -Format yyyyMMdd-HHmmss)"
python validate.py SOURCE.csv --rules rules/<name>.yaml --out "$run"; "validate: $LASTEXITCODE"
python map.py "$run/clean.csv" --mapping mappings/<name>.yaml --out "$run"; "map: $LASTEXITCODE"
python reconcile.py "$run/clean.csv" "$run/target.csv" --key <key column[,column]> --mapping mappings/<name>.yaml --out "$run"; "reconcile: $LASTEXITCODE"
```

- If validate exits 1: read `rejects.csv`, resolve each row with the customer in a **separate reviewed copy** of the export, and rerun everything into a **new** run directory. Do not hand-pick good rows.
- If map or reconcile exits 1: the mapping or the key is wrong, or the files differ. Fix the configuration with the customer, rerun from validate into a new directory.
- Any exit 2: the run itself failed (bad path, bad YAML, I/O). Fix the cause; use nothing from that directory.
- Then the optional upload, only after reconcile passed: `python upload_s3.py "$run/target.csv" s3://BUCKET/KEY --out "$run"`.
- Open `python viewer.py "$run"` and look at the rows with the customer. Looking is not approval; the exit codes and reports are.

## 3. Go / no-go

Go requires **all** of the following for the same run directory, in the same session, on the frozen export:

| Criterion | Evidence |
|---|---|
| validate exit 0 | `rejects.csv` is header-only; `clean.csv.sha256` present |
| map exit 0 | `mapping_report.csv` has no `error` rows; `target.csv.sha256` present |
| reconcile exit 0 | `reconciliation.md` ends with `Verdict: PASS` |
| upload exit 0 (if used) | `upload_report.md` ends with `Verdict: PASS`; remote hash equals `target.csv.sha256` |
| Rules and mapping unchanged since the customer reviewed them | file hashes recorded below match the reviewed versions |

Anything else is no-go. There is no partial go, no "go with known issues", and no force flag.

## 4. Sign-off

```text
Cutover sign-off

Customer / project:      ____________________________________
Source export file:      ____________________________________  exported at: ______________
Source SHA-256:          ____________________________________________________________
Rules file + SHA-256:    rules/____________.yaml    _______________________________________
Mapping file + SHA-256:  mappings/_________.yaml    _______________________________________
Run directory:           output/____________________________________
clean.csv.sha256:        ____________________________________________________________
target.csv.sha256:       ____________________________________________________________
Remote object (if any):  s3://_________________________________  remote SHA-256 matches: [ ] yes
reconciliation.md:       Verdict: PASS   [ ] confirmed
upload_report.md:        Verdict: PASS   [ ] confirmed   [ ] not used

Run by (name, date, time):        ____________________________________
Approved by (customer, date):     ____________________________________
```

Keep the run directory with this page. It is the audit trail: what was checked, what was refused, and what exactly was handed over.

## 5. Rollback

The target is a new file (and, if uploaded, a new S3 object). The source was never changed. Rolling back this toolkit's work therefore means discarding this run's target file and rerunning; reversing an import that the receiving system has already performed is the target system's procedure, not this runbook's. Steps:

1. Delete the target object or file at the destination (and nothing else there).
2. Unfreeze the legacy system if the cutover is postponed, and record why.
3. When the cause is fixed (a new export, a corrected rule or mapping agreed with the customer), rerun the whole chain into a **new** run directory and sign off again. Never edit files inside an old run directory to make them pass.
