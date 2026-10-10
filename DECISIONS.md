# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index protects identity. A unique `(emp_code, date)` index prevents duplicate attendance days and supports punch lookups. Date-leading attendance indexes back listings and monthly analytics; department/join-date indexes support headcount and trend queries. I did not add a unique email index because the contract guarantees only `emp_code` uniqueness.

2. **Punch-in race.** Both requests may reach insert, but the unique `(emp_code, date)` index lets only one document succeed. That request returns `201`; MongoDB rejects the other and the API maps the duplicate-key error to `409`. A prior existence check alone is not safe.

3. **Ties.** Competition ranking uses total late minutes only. If the cutoff rank is tied, every employee with `rank <= limit` is returned, and tied rows are then ordered by `emp_code`.

4. **Headcount.** The summary starts from employees who joined by month-end and looks up logs while preserving employees with no logs. Headcount is counted once per employee, not once per log.

5. **100x more data.** I would maintain incremental daily employee/department aggregates so trend and summary queries do not expand every employee across every day. I would validate plans and workload against production-like data before adding caching or partitioning.
