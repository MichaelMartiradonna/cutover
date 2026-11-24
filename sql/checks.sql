-- Cutover validation and reconciliation checks in SQLite.
-- Cross-checks the Python pipeline (validate.py / reconcile.py) against the
-- raw and reviewed CSV snapshots loaded by sql/load.py.
--
-- Tables (all source fields stored as unmodified TEXT):
--   tenants_raw       data/examples/tenants_appfolio_200.csv           (200 rows, 12 deliberate faults)
--   tenants_reviewed  data/examples/tenants_appfolio_200_reviewed.csv  (200 rows, corrected)
-- Columns: record_no, unit, name, move_in_date, status, extra_fields

-- 1. Row counts: confirm raw and reviewed files have the same number of rows
-- Catches silently dropped or extra rows before comparing individual fields.
SELECT
    (SELECT COUNT(*) FROM tenants_raw)      AS raw_rows,
    (SELECT COUNT(*) FROM tenants_reviewed) AS reviewed_rows,
    (SELECT COUNT(*) FROM tenants_reviewed)
      - (SELECT COUNT(*) FROM tenants_raw)  AS row_diff;

-- 2. Required fields (R001): find records missing a unit or tenant name
-- Trims whitespace so blank-space entries are caught alongside empty strings and NULLs.
SELECT
    record_no,
    unit,
    name,
    CASE
        WHEN (unit IS NULL OR trim(unit) = '') AND (name IS NULL OR trim(name) = '') THEN 'unit, name'
        WHEN unit IS NULL OR trim(unit) = '' THEN 'unit'
        ELSE 'name'
    END AS missing_field
FROM tenants_raw
WHERE extra_fields = 0  -- skip structurally broken rows, matching validate.py
  AND (
      unit IS NULL OR trim(unit) = ''
      OR name IS NULL OR trim(name) = ''
  )
ORDER BY record_no;

-- 3. Allowed status values (E001): find records outside the approved status list
-- Keeps the approved values in a subquery and checks NULL explicitly so unexpected or miscased values stand out.
SELECT
    record_no,
    unit,
    name,
    status
FROM tenants_raw
WHERE extra_fields = 0
  AND (
      status IS NULL
      OR status NOT IN (
          SELECT 'active'
          UNION ALL SELECT 'notice'
          UNION ALL SELECT 'vacant'
      )
  )
ORDER BY record_no;

-- 4. Move-in dates (D001, D002): find invalid calendar dates or future dates
-- Checks strict YYYY-MM-DD format, confirms SQLite parses it back to the exact same date, and rejects future dates.
SELECT
    record_no,
    unit,
    name,
    move_in_date,
    CASE
        WHEN move_in_date IS NULL
          OR move_in_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
          OR date(move_in_date) IS NOT move_in_date
            THEN 'D001: invalid YYYY-MM-DD calendar date'
        ELSE 'D002: date is after local run date'
    END AS rule_reason
FROM tenants_raw
WHERE extra_fields = 0
  AND (
      move_in_date IS NULL
      OR move_in_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
      OR date(move_in_date) IS NOT move_in_date  -- catches impossible dates like Feb 30
      OR move_in_date > date('now', 'localtime')
  )
ORDER BY record_no;

-- 5. Duplicate tenants summary (U001, GROUP BY): one row per repeated (unit, name) pair
-- Normalizes case and surrounding spaces so near-identical duplicates are grouped together.
SELECT
    lower(trim(unit))              AS normalized_unit,
    lower(trim(name))              AS normalized_name,
    COUNT(*)                       AS occurrences,
    GROUP_CONCAT(record_no, ', ')  AS record_nos
FROM (
    SELECT record_no, unit, name
    FROM tenants_raw
    WHERE extra_fields = 0
      AND unit IS NOT NULL AND trim(unit) <> ''
      AND name IS NOT NULL AND trim(name) <> ''
    ORDER BY record_no
)
GROUP BY lower(trim(unit)), lower(trim(name))
HAVING COUNT(*) > 1
ORDER BY MIN(record_no);

-- 6. Duplicate tenants by record (U001, window functions): list every row in a duplicate group
-- Tags each row's order and group size without collapsing rows, making it easy to see the original vs. repeat.
WITH ranked AS (
    SELECT
        record_no,
        unit,
        name,
        ROW_NUMBER() OVER (
            PARTITION BY lower(trim(unit)), lower(trim(name))
            ORDER BY record_no
        ) AS dup_rank,
        COUNT(*) OVER (
            PARTITION BY lower(trim(unit)), lower(trim(name))
        ) AS dup_count
    FROM tenants_raw
    WHERE extra_fields = 0
      AND unit IS NOT NULL AND trim(unit) <> ''
      AND name IS NOT NULL AND trim(name) <> ''
)
SELECT
    record_no,
    unit,
    name,
    dup_rank,
    dup_count
FROM ranked
WHERE dup_count > 1
ORDER BY record_no;

-- 7. File format (F003): find rows with extra comma-separated columns
-- Flags rows whose column count does not match the four-column header.
SELECT
    record_no,
    unit,
    name,
    status,
    extra_fields
FROM tenants_raw
WHERE extra_fields > 0
ORDER BY record_no;

-- 8. Unified reject list: combine all checks into one log matching validate.py's rejects.csv
-- Uses UNION ALL so every rule violation is kept intact, even if one record breaks multiple rules.
WITH ranked_dups AS (
    SELECT
        record_no,
        unit,
        name,
        ROW_NUMBER() OVER (
            PARTITION BY lower(trim(unit)), lower(trim(name))
            ORDER BY record_no
        ) AS dup_rank,
        COUNT(*) OVER (
            PARTITION BY lower(trim(unit)), lower(trim(name))
        ) AS dup_count
    FROM tenants_raw
    WHERE extra_fields = 0
      AND unit IS NOT NULL AND trim(unit) <> ''
      AND name IS NOT NULL AND trim(name) <> ''
)
SELECT record_no, rule, why
FROM (
    -- R001: missing unit
    SELECT
        record_no,
        'R001' AS rule,
        'unit is required; it is blank.' AS why
    FROM tenants_raw
    WHERE extra_fields = 0
      AND (unit IS NULL OR trim(unit) = '')

    UNION ALL

    -- R001: missing name
    SELECT
        record_no,
        'R001' AS rule,
        'name is required; it is blank.' AS why
    FROM tenants_raw
    WHERE extra_fields = 0
      AND (name IS NULL OR trim(name) = '')

    UNION ALL

    -- E001: status outside approved list
    SELECT
        record_no,
        'E001' AS rule,
        'status must be active, notice, or vacant (found: ' || COALESCE(status, 'NULL') || ').' AS why
    FROM tenants_raw
    WHERE extra_fields = 0
      AND (
          status IS NULL
          OR status NOT IN (
              SELECT 'active'
              UNION ALL SELECT 'notice'
              UNION ALL SELECT 'vacant'
          )
      )

    UNION ALL

    -- D001: invalid date format or impossible calendar date
    SELECT
        record_no,
        'D001' AS rule,
        'move_in_date is not a valid YYYY-MM-DD date (found: ' || COALESCE(move_in_date, 'NULL') || ').' AS why
    FROM tenants_raw
    WHERE extra_fields = 0
      AND (
          move_in_date IS NULL
          OR move_in_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
          OR date(move_in_date) IS NOT move_in_date
      )

    UNION ALL

    -- D002: valid date, but in the future
    SELECT
        record_no,
        'D002' AS rule,
        'move_in_date is after local run date ' || date('now', 'localtime') || ' (found: ' || move_in_date || ').' AS why
    FROM tenants_raw
    WHERE extra_fields = 0
      AND move_in_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
      AND date(move_in_date) IS move_in_date
      AND move_in_date > date('now', 'localtime')

    UNION ALL

    -- U001: duplicate (unit, name) pairs after trimming and case-folding
    SELECT
        record_no,
        'U001' AS rule,
        'Duplicate unit and name (occurrence ' || dup_rank || ' of ' || dup_count || ').' AS why
    FROM ranked_dups
    WHERE dup_count > 1

    UNION ALL

    -- F003: wrong number of CSV columns
    SELECT
        record_no,
        'F003' AS rule,
        'Expected four fields; found ' || (4 + extra_fields) || '.' AS why
    FROM tenants_raw
    WHERE extra_fields > 0
)
ORDER BY record_no, rule;

-- 9. Review diff (JOIN): show every record changed between the raw and reviewed files
-- Uses IS NOT instead of <> so changes involving NULL values are compared accurately.
SELECT
    r.record_no,
    r.unit         AS raw_unit,
    v.unit         AS rev_unit,
    r.name         AS raw_name,
    v.name         AS rev_name,
    r.move_in_date AS raw_move_in_date,
    v.move_in_date AS rev_move_in_date,
    r.status       AS raw_status,
    v.status       AS rev_status,
    r.extra_fields AS raw_extra_fields,
    v.extra_fields AS rev_extra_fields
FROM tenants_raw AS r
INNER JOIN tenants_reviewed AS v
    ON r.record_no = v.record_no
WHERE r.unit         IS NOT v.unit
   OR r.name         IS NOT v.name
   OR r.move_in_date IS NOT v.move_in_date
   OR r.status       IS NOT v.status
   OR r.extra_fields IS NOT v.extra_fields
ORDER BY r.record_no;

-- 10. Change summary (GROUP BY): count how many records changed in each column
-- Highlights which fields required the most cleanup before import.
WITH column_diffs AS (
    SELECT r.record_no, 'unit' AS column_name
    FROM tenants_raw AS r
    INNER JOIN tenants_reviewed AS v ON r.record_no = v.record_no
    WHERE r.unit IS NOT v.unit

    UNION ALL

    SELECT r.record_no, 'name' AS column_name
    FROM tenants_raw AS r
    INNER JOIN tenants_reviewed AS v ON r.record_no = v.record_no
    WHERE r.name IS NOT v.name

    UNION ALL

    SELECT r.record_no, 'move_in_date' AS column_name
    FROM tenants_raw AS r
    INNER JOIN tenants_reviewed AS v ON r.record_no = v.record_no
    WHERE r.move_in_date IS NOT v.move_in_date

    UNION ALL

    SELECT r.record_no, 'status' AS column_name
    FROM tenants_raw AS r
    INNER JOIN tenants_reviewed AS v ON r.record_no = v.record_no
    WHERE r.status IS NOT v.status

    UNION ALL

    SELECT r.record_no, 'extra_fields' AS column_name
    FROM tenants_raw AS r
    INNER JOIN tenants_reviewed AS v ON r.record_no = v.record_no
    WHERE r.extra_fields IS NOT v.extra_fields
)
SELECT
    column_name,
    COUNT(*) AS changed_records
FROM column_diffs
GROUP BY column_name
ORDER BY changed_records DESC, column_name;
