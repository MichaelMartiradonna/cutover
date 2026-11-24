# Cutover's checks in SQL

The Python pipeline validates and reconciles with code. This folder runs the same checks as SQL queries against SQLite, as a cross-check: if the SQL and `validate.py` disagree about a record, one of them is wrong.

Standard library only (Python's built-in `sqlite3`; nothing to install).

## Run it

From the repository folder (PowerShell):

```powershell
python sql/load.py --db sql/cutover.db tenants_raw=data/examples/tenants_appfolio_200.csv tenants_reviewed=data/examples/tenants_appfolio_200_reviewed.csv
python sql/run.py sql/checks.sql --db sql/cutover.db --md sql/results.md
```

`load.py` stores every field as text exactly as written (nothing trimmed or repaired) and adds `record_no` (the CSV record number `validate.py` reports, header = 1) and `extra_fields`. `run.py` prints each query's result under its title comment and writes the full Markdown report to `sql/results.md` (git-ignored).

## What the queries do

| # | Check | What it verifies | SQL feature |
|---|---|---|---|
| 1 | Row counts | Compares row counts in `tenants_raw` and `tenants_reviewed` side by side (`row_diff`) | Scalar subqueries |
| 2 | Required fields (`R001`) | Flags rows where `unit` or `name` is `NULL`, empty, or whitespace-only | `WHERE`, `IS NULL`, `trim()`, `CASE` |
| 3 | Allowed values (`E001`) | Flags rows whose `status` is `NULL` or outside the case-sensitive allowed list | Subquery (`NOT IN (SELECT ... UNION ALL)`) |
| 4 | Dates (`D001`, `D002`) | Flags impossible or non-`YYYY-MM-DD` dates and dates after the local run date | `GLOB`, `date()` round-trip with `IS NOT`, `CASE` |
| 5 | Duplicates — grouped (`U001`) | Groups normalized `(unit, name)` pairs appearing more than once and lists their record numbers | `GROUP BY`, `HAVING COUNT(*) > 1`, `GROUP_CONCAT()` |
| 6 | Duplicates — per record (`U001`) | Assigns each duplicate record its rank and group size without collapsing rows | CTE, window functions (`ROW_NUMBER() OVER`, `COUNT(*) OVER`) |
| 7 | File format (`F003`) | Flags rows containing extra comma-separated fields beyond the four-column header | `WHERE extra_fields > 0` |
| 8 | Unified reject list | Combines all validation checks into one `(record_no, rule, why)` log sorted by `record_no, rule` | CTE, `UNION ALL`, `ORDER BY` |
| 9 | Review diff | Joins raw to reviewed on `record_no` to show every modified record's old and new values | `INNER JOIN`, `IS NOT` (handles `NULL`s safely) |
| 10 | Change summary | Counts how many records changed in each column between raw and reviewed | CTE, `UNION ALL`, `GROUP BY` |

## Results

Run against the synthetic 200-row AppFolio-style tenant exports (`tenants_appfolio_200.csv` vs. `tenants_appfolio_200_reviewed.csv`):

- **Parity with `validate.py`:** Query 8 flags **12 records**, matching `rejects.csv` record for record on `(record_no, rule)`: 2 `R001` (records 13, 26), 2 `D001` (39, 54), 1 `D002` (68), 2 `E001` (83, 97), 4 `U001` (121, 122, 152, 153), and 1 `F003` (179).
- **What the review changed:** Both tables hold **200 rows** (`row_diff = 0`). Query 9 shows **10 records** changed between raw and reviewed: the 8 single-record faults were corrected in place (including dropping the extra field on record 179), while the 2 original duplicate rows (`121`, `152`) stayed untouched and their duplicate counterparts (`122`, `153`) were replaced with valid tenant records.

### Unified reject list (Query 8 — 12 rows, matches `rejects.csv`)

| record_no | rule | why |
|---|---|---|
| 13 | R001 | unit is required; it is blank. |
| 26 | R001 | name is required; it is blank. |
| 39 | D001 | move_in_date is not a valid YYYY-MM-DD date (found: 2021-02-30). |
| 54 | D001 | move_in_date is not a valid YYYY-MM-DD date (found: 15/01/2020). |
| 68 | D002 | move_in_date is after local run date 2026-11-24 (found: 2099-01-01). |
| 83 | E001 | status must be active, notice, or vacant (found: Active). |
| 97 | E001 | status must be active, notice, or vacant (found: pending). |
| 121 | U001 | Duplicate unit and name (occurrence 1 of 2). |
| 122 | U001 | Duplicate unit and name (occurrence 2 of 2). |
| 152 | U001 | Duplicate unit and name (occurrence 1 of 2). |
| 153 | U001 | Duplicate unit and name (occurrence 2 of 2). |
| 179 | F003 | Expected four fields; found 5. |

### Changed records per column (Query 10 — 16 field changes across 10 records)

| column_name | changed_records |
|---|---|
| move_in_date | 5 |
| status | 4 |
| name | 3 |
| unit | 3 |
| extra_fields | 1 |
