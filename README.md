# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment. Requires Python 3.11+ and MongoDB 6.0+.

## Run locally

Set `MONGO_URI` and `MONGO_DB` in your shell (or in a local, uncommitted `.env` file), then run:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

For local MongoDB, a typical URI is `mongodb://localhost:27017` and the database name can be `attendance_db`. The service creates its indexes idempotently at startup. `GET /health` checks MongoDB readiness. To test against the supplied sample documents, run `python sample_seed.py` from the original candidate kit after setting the same environment variables; keep the seed script and data files out of the public submission.

## Included functionality

- Employee create/list with validation, filtered pagination, and database-enforced unique business codes.
- Attendance punch-in, punch-out, paginated listing, and audit-trailed regularization.
- Employee-month, department-summary, late-leaderboard, and daily-department-trend analytics using MongoDB aggregation pipelines.
- `GET /admin/explain/{endpoint}` for execution-stats explain plans on the supported query paths.

The API accepts and returns instants as epoch milliseconds; attendance dates and shift clocks follow the assignment's IST rules. Do not commit credentials, a `.env` file, the assignment's problem statement, or sample data dumps.
