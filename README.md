# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or a local .env
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0+ (uses `$setWindowFields`, `$densify`). Indexes are created at startup.

## Notes
- Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.
- Each endpoint and its explain share one plan function, so explain shows the real query.
- Known gaps / honest status: Tested against MongoDB Atlas (7.x) with a small hand-made dataset (ties, no-log employees, weekend records, mid-month joiner, overnight shift, parallel punch-in/out). Not load-tested with 100k records.
