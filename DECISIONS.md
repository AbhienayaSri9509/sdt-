# DECISIONS.md

1. **Indexes.**
   We created four indexes:
   - `employees: { emp_code: 1 }` (unique): Guarantees unique employee codes without race conditions and accelerates point lookups.
   - `employees: { department: 1, joined_on: 1 }`: Covers department filtering, headcount checks, and lookups during department analytics.
   - `attendance_logs: { emp_code: 1, date: 1 }` (unique): Guarantees at most one log per employee per day and speeds up regularizations and lookups.
   - `attendance_logs: { date: -1, emp_code: 1 }`: Enables index-supported sorting and range scans for listing attendance without in-memory sorting.
   - `attendance_logs: { date: 1, late_minutes: -1 }`: Optimizes late-leaderboard queries by scanning only dates and positive late minutes via `IXSCAN`.
   
   *Considered and rejected:* An index on `{ punch_out: 1 }`. Punch-out queries locate records by `{ emp_code, punch_in }`, so indexing `punch_out` alone offers negligible selectivity and increases write overhead.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously: both compute the attendance date and attempt `db.attendance_logs.insert_one(doc)`. Because of the unique compound index on `{ emp_code: 1, date: 1 }`, MongoDB's WiredTiger engine serializes the index insertion. Exactly one request successfully acquires the unique index key slot and returns HTTP 201; the concurrent request trips a duplicate key constraint, raises `DuplicateKeyError`, and is caught by our handler to return HTTP 409 Conflict. No check-then-insert window exists.

3. **Ties.**
   When two employees have identical `total_late_minutes` at the cutoff of `limit`: our pipeline uses `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4) and filters by `rank <= limit`. Both tied employees share the same rank, and both are returned in the response (which may contain more rows than `limit`), ordered by `total_late_minutes` descending and `emp_code` ascending per contract requirements.

4. **Headcount.**
   The department summary pipeline initiates from the `employees` collection matching `{ joined_on: { $lte: period_end } }` and uses `$lookup` to join `attendance_logs` followed by `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero attendance logs are preserved as null entries, and their `emp_code` is accumulated into an `all_emps` set whose `$size` provides the authoritative headcount.

5. **One thing you would change.**
   If data scaled 100x (~10M+ attendance logs), we would pre-aggregate and store daily department rollups and employee monthly summaries upon punch-out/regularization (or via incremental change streams) instead of computing ad-hoc multi-month cross-collection aggregations on request.
