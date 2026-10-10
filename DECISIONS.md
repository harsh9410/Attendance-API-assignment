# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`, `(department, emp_code)`, `(joined_on)` and `(department, joined_on)`. `attendance_logs`: unique `(emp_code, date)` for the double punch-in guarantee and the monthly query, `(date desc, emp_code)` for list sorting and date ranges, `(status, date, emp_code)` for the status filter, and `(emp_code, punch_in desc)` to find the record to punch out. I did not index `late_minutes`: every query narrows by date first, and the extra index would only slow down writes.

2. **Punch-in race.** Both requests work out the same date and call `insert_one`. The unique `(emp_code, date)` index lets only one of them succeed. The other gets a duplicate key error and I return 409. I never check first and insert later.

3. **Ties.** The leaderboard uses `$rank`, so tied people share a rank and the next rank is skipped (1, 2, 2, 4). I apply `limit` to the rank, not to the row count, so everyone tied at the cutoff is returned, even if that gives more than `limit` rows.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up their logs. `$unwind` keeps employees with no logs, and each employee is counted once.

5. **100x data.** I would stop calculating from raw logs each time. I would keep a daily summary collection updated on every write, and store `department` on each log so the `$lookup` is not needed.
