# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

1. **Indexes:** I created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to prevent duplicate records during race conditions, plus compound indexes like `[(date, -1), (emp_code, 1)]` for index-backed pagination. I considered and rejected a single index on just `date`, because pagination queries always need to sort by both `date` and `emp_code` together.
2. **Punch-in race:** Both requests skip the buggy `find_one` read check and immediately execute `insert_one`. MongoDB’s unique `(emp_code, date)` index processes them atomically. The first request succeeds and gets a 201 Created. The second request triggers a `DuplicateKeyError`, which my `except` block catches to return a 409 Conflict.
3. **Ties:** My pipeline uses `$setWindowFields` with `$rank` to apply standard competition ranking (e.g., 1, 2, 2, 4). If multiple employees tie exactly at the cutoff `limit`, they share that valid rank and all are returned, meaning the response can safely contain more items than the `limit`.
4. **Headcount:** The pipeline starts by querying the `employees` collection first (filtering by `joined_on`), and then uses `$lookup` to fetch their logs. This guarantees every employee is counted in the headcount, even if their log array comes back empty.
5. **Scaling 100x:** I would replace my offset `.skip()` pagination with cursor-based (keyset) pagination. Skipping deep into millions of records becomes incredibly slow, whereas cursor pagination remains fast.

