# REVIEW.md

Defects found in the starter `app/main.py`. Line references are to the starter file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Replaces hour/minute on the **UTC** punch-in, but shift times are IST (R1). Late minutes are off by 5h30. | Punch-in 09:28 IST (03:58 UTC) on a 09:30 shift: the code compares against 09:30 UTC and gets a huge negative/positive number. | Shift start is built as an IST datetime on the attendance date (`_ist_at`). |
| 2 | `compute_late_minutes` | Floors to whole minutes **before** comparing with 10 (`minutes > 10`), so seconds are lost. 09:40:01 gives 10 min, `10 > 10` is false, so it returns 0. R2 says late when strictly more than 10:00. | Punch-in 09:40:01 on a 09:30 shift: expected 10, got 0. | Compare seconds (`> 600`), then floor to minutes. |
| 3 | `compute_late_minutes` | Uses the punch-in's own calendar day as the shift day. Overnight shifts (22:00-06:00) punched in after midnight belong to the previous day. | Overnight employee punches in at 02:00: start is computed on the wrong day. | Uses the attendance date from R1 (`attendance_date`). |
| 4 | `compute_work_hours` | `round()` is banker's rounding on binary floats, R4 wants half-up. | 18 seconds = 0.005 h should be 0.01, float round gives 0.0 or 0.01 by luck. | `Decimal` with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Ignores the 30 minute minimum (R3): 5 min after shift end is reported as 5. | Punch-out 18:35 on 18:30 shift: expected 0, got 5. | Return minutes only if `>= 30`. |
| 6 | `compute_overtime` | Shift end is always on the record date. For overnight shifts it falls on the next day, so overtime is hugely wrong. Also uses `fromisoformat(date).replace(...)` and never gets the shift start. | Overnight 22:00-06:00, punch-out 06:40: shows about -15h (0) or garbage. | Shift end gets `+1 day` when `shift_end <= shift_start`. |
| 7 | `create_employee` | Check-then-insert (`find_one` then `insert_one`) is a race: concurrent creates both pass the check. No unique index exists. | Fire 20 parallel POSTs with the same `emp_code`: several 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` and return 409. |
| 8 | `create_employee` / `EmployeeIn` | No validation: bad `emp_code` format, bad email, `shift_start == shift_end`, impossible `joined_on` (2026-02-30), over-long strings all get stored. | POST `joined_on: "banana"` returns 201. | Pydantic patterns/lengths and validators, 422 on failure. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive **local** time, and the response returns a datetime instead of epoch ms. | Different server time zone gives different stored instants. | `now_utc()` aware UTC; response converted to epoch ms. |
| 10 | `list_employees` | `skip = page * page_size`, so page 1 skips the first page (should be `(page-1)*page_size`). | `GET /employees?page=1` misses the first 20 employees. | Fixed skip. |
| 11 | `list_employees` | `total = count_documents({})` ignores the `department` filter (R10). | `?department=Sales` returns 2 items but `total` is the whole collection. | `count_documents(q)` with the same filter. |
| 12 | `list_employees` | No sort, so pages are not stable. `page=0`, negative or `page_size=100000` are accepted. | `page=0`, `page_size=1000000`. | Sort by `emp_code`, `Query(ge=1, le=100)` validation, index `(department, emp_code)`. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises `TypeError`, so 500 instead of 404. | POST with an unknown `emp_code`. | `get_employee()` raises 404. |
| 14 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses server local time, naive. Seconds-looking values, floats and strings are not rejected; `if body.punched_at` treats `0` as "missing". Sub-second part is kept (R1 says truncate). | `punched_at=1783312500` (seconds) is accepted and lands in 1970. | Strict `int` field with range check (422), `to_dt` truncates to whole seconds, `is not None` check. |
| 15 | `punch_in` | `date` is the server's local calendar date, not the IST attendance date (R1, overnight rule). | Server in UTC: punch-in 00:30 IST gets yesterday's date. | `attendance_date()`. |
| 16 | `punch_in` | Check-then-insert race: two simultaneous punch-ins both pass `find_one`. | Parallel identical punch-ins give two 201s or duplicates. | Unique index `(emp_code, date)`; `DuplicateKeyError` gives 409. |
| 17 | `punch_in` / `PunchInIn` | `status` is a free string: `ABSENT`, `LEAVE` or `"banana"` are accepted. | `status: "ABSENT"` with a punch time. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 18 | `punch_in` response | Leaks internal `_id` as `id`, returns datetimes instead of epoch ms. | Response has `"id": "66f..."`. | `record_out()` serializer. |
| 19 | `list_attendance` | Loads the whole collection (`list(find(q))`), sorts and slices in Python. Slow and memory-heavy at 100k records. `total = len(docs)`. | 100k records: seconds and big memory. | Sort/skip/limit in MongoDB with index `(date desc, emp_code)`, `count_documents`. |
| 20 | `list_attendance` | Sort is by date only (R: then `emp_code`). `page=0` gives a negative slice and wrong results. `date_from > date_to` not rejected, dates and `status` not validated. | `page=0`; `date_from=2026-07-10&date_to=2026-07-01` returns 200. | Sort `(date -1, emp_code 1)`, validated query params, 422. |
| 21 | `list_attendance` | Returns `_id` as `id` and raw datetimes; legacy records without `half_day`/`history` come back with fields missing. | Legacy record (sample 9) has no `half_day`. | `record_out()` fills defaults, epoch ms, no ids. |
| 22 | `/health` | Always returns ok without touching MongoDB. | Stop MongoDB: still 200. | `ping`, 503 on failure. |
| 23 | module level | `MongoClient` is not `tz_aware`, so reads return naive datetimes that get mixed with aware ones. No indexes are created. | `TypeError: can't subtract offset-naive and offset-aware datetimes`. | `tz_aware=True`; indexes created at startup, idempotently. |

## Looked at and decided it is fine
- `load_dotenv()`: by default it does **not** override real environment variables, so the "real env wins" requirement is met.
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct (India has no DST).
- Defaults `09:30` / `18:30` for shifts match the contract.
- `doc.pop("_id")` after `insert_one`: needed because PyMongo adds `_id` to the dict; fine once the response is built by a serializer.
- `/health` route path and response shape are right; only the missing ping was wrong.
