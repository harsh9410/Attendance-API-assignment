"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded, but real environment variables win)

Layout of this single file:
  1. config + indexes
  2. time / rule helpers (R1-R5, R8)
  3. request models
  4. attendance + employee endpoints
  5. "plans": one function per analytics endpoint that builds its query / pipeline.
     The real endpoint AND /admin/explain both call the same plan function, so the
     explain output is always for exactly the query the endpoint runs.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False: real environment variables take priority
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Config + indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE = ["ABSENT", "LEAVE"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# Index names are explicit so queries can hint them (guarantees IXSCAN in explain).
IDX_EMP_DATE = "emp_date_unique"
IDX_DATE_EMP = "date_desc_emp"
IDX_DEPT_JOINED = "dept_joined"
IDX_JOINED = "joined_on"

INDEXES = {
    "employees": [
        IndexModel([("emp_code", 1)], name="emp_code_unique", unique=True),
        IndexModel([("department", 1), ("emp_code", 1)], name="dept_emp"),
        IndexModel([("joined_on", 1)], name=IDX_JOINED),
        IndexModel([("department", 1), ("joined_on", 1)], name=IDX_DEPT_JOINED),
    ],
    "attendance_logs": [
        # one record per (employee, day): this is what makes punch-in race-safe
        IndexModel([("emp_code", 1), ("date", 1)], name=IDX_EMP_DATE, unique=True),
        # list sort (date desc, emp_code asc) and every date-range scan
        IndexModel([("date", -1), ("emp_code", 1)], name=IDX_DATE_EMP),
        IndexModel([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp"),
        # find the employee's latest punch-in at/before a time (punch-out)
        IndexModel([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in"),
    ],
}
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent. One failure must not stop the app from starting."""
    global _indexes_ready
    ok = True
    for coll, models in INDEXES.items():
        for model in models:
            try:
                db[coll].create_indexes([model])
            except Exception as exc:  # e.g. Mongo down, or duplicate data blocking a unique index
                ok = False
                log.warning("index %s.%s not created: %s", coll, model.document.get("name"), exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Helpers: time and business rules
# --------------------------------------------------------------------------- #
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
# strict=True: floats and strings are rejected (422), not coerced
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_ms(dt: Any) -> Optional[int]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_date(value: str, name: str = "date") -> date_cls:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_range(month: str) -> tuple[date_cls, date_cls]:
    if not re.match(MONTH_PATTERN, month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        raise HTTPException(422, "month is out of range")
    return date_cls(year, mon, 1), date_cls(year, mon, calendar.monthrange(year, mon)[1])


def half_up(value: Any, places: int) -> float:
    """Round half-up (R8). Python's round() is banker's rounding on binary floats."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def shift_of(emp: dict) -> tuple[str, str]:
    return emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning
    punch-in (before shift_end) belongs to the previous day's shift."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = _hm(shift_end)
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def _ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date_cls.fromisoformat(date_str) + timedelta(days=plus_days)
    h, m = _hm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; value is floor-minutes since shift start."""
    seconds = int((punch_in - _ist_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor-minutes after shift end, only if >= 30. Overnight shifts end on the next day."""
    end = _ist_at(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str, emp: dict) -> dict:
    """All derived fields in one place (used by punch-in, punch-out and PATCH)."""
    zero = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in ABSENCE or punch_in is None:
        return zero
    start, end = shift_of(emp)
    late = compute_late_minutes(punch_in, date_str, start)
    if punch_out is None:
        return {**zero, "late_minutes": late}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, start, end),
        "half_day": hours < 4.5,  # R5: on the rounded value
    }


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API JSON). _id never leaves the service.
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entries: Optional[list]) -> list:
    out = []
    for h in entries or []:
        changes = {}
        for key, val in (h.get("changes") or {}).items():
            if key in ("punch_in", "punch_out"):
                changes[key] = {"from": to_ms(val.get("from")), "to": to_ms(val.get("to"))}
            else:
                changes[key] = {"from": val.get("from"), "to": val.get("to")}
        out.append({"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return out


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),  # legacy records may omit it
        "history": history_out(doc.get("history")),
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HM_PATTERN)
    shift_end: str = Field("18:30", pattern=HM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # derived fields are not declared, so a client that sends them is simply ignored
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Employees + attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:  # Mongo was down at startup: retry now
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created = now_utc()
    doc = body.model_dump()
    doc["created_at"] = created
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = []
    if skip < 2**31:
        cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    start, end = shift_of(emp)
    day = attendance_date(ts, start, end)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record whose punch_in is at or before ts (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    fields = {"punch_out": ts, **derive(rec["status"], pin, ts, rec["date"], emp)}
    # compare-and-set: only succeeds while the record is still open and unchanged
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": pin},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def plan_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    lo = parse_date(date_from, "date_from") if date_from else None
    hi = parse_date(date_to, "date_to") if date_to else None
    if lo and hi and lo > hi:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = date_from
        if hi:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {"filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    q = plan["filter"]
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    items = []
    if plan["skip"] < 2**31:
        cur = (
            db.attendance_logs.find(q)
            .sort(list(plan["sort"].items()))
            .skip(plan["skip"])
            .limit(plan["limit"])
        )
        items = [record_out(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date).isoformat()
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    status = body.status or rec["status"]
    new_in, new_out = rec.get("punch_in"), rec.get("punch_out")
    if status in ABSENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None
    else:
        if body.punch_in is not None:
            new_in = to_dt(body.punch_in)
        if body.punch_out is not None:
            new_out = to_dt(body.punch_out)
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, *shift_of(emp)) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if (body.punch_in is not None or body.punch_out is not None) and new_out is not None:
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derive(status, new_in, new_out, day, emp)}
    if all(old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "the request changes nothing")
    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]},
    }

    # optimistic concurrency: every PATCH grows history by one, so a changed history length
    # (or changed status/punch times) means somebody else edited the record -> 409 for the loser.
    cond = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    hist = rec.get("history")
    if isinstance(hist, list):
        cond["history"] = {"$size": len(hist)}
        update = {"$set": new, "$push": {"history": entry}}
    else:  # legacy record with no history field
        cond["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    updated = db.attendance_logs.find_one_and_update(cond, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 5. Analytics plans (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
class Plan:
    """What an endpoint runs: a find() or an aggregation, on one collection."""

    def __init__(self, collection: str, pipeline: Optional[list] = None, hint: Optional[str] = None, find: Optional[dict] = None):
        self.collection, self.pipeline, self.hint, self.find = collection, pipeline, hint, find

    def run(self) -> list:
        return list(db[self.collection].aggregate(self.pipeline, hint=self.hint))

    def explain_command(self) -> dict:
        if self.find is not None:
            inner = {"find": self.collection, "filter": self.find["filter"], "sort": self.find["sort"],
                     "skip": self.find["skip"], "limit": self.find["limit"]}
        else:
            inner = {"aggregate": self.collection, "pipeline": self.pipeline, "cursor": {}}
            if self.hint:
                inner["hint"] = self.hint
        return {"explain": inner, "verbosity": "executionStats"}


# --- small expression builders (p = field prefix: "" for a log, "logs." after $unwind) --- #
def _f(p: str, name: str) -> str:
    return f"${p}{name}"


def _late(p: str) -> dict:
    return {"$ifNull": [_f(p, "late_minutes"), 0]}


def _is_present(p: str) -> dict:
    return {"$in": [_f(p, "status"), PRESENCE]}


def _is_weekday(p: str) -> dict:
    # $dayOfWeek: 1=Sunday ... 7=Saturday, so Mon-Fri = 2..6. The date string is parsed as a plain
    # calendar date, no time zone involved.
    day = {"$dateFromString": {"dateString": _f(p, "date"), "format": "%Y-%m-%d", "onError": None, "onNull": None}}
    return {"$in": [{"$dayOfWeek": day}, [2, 3, 4, 5, 6]]}


def _present_value(p: str, weekday_only: bool) -> dict:
    """1 for a present record, 0.5 for a present half day, else 0 (R5, R6, R7)."""
    cond = _is_present(p)
    if weekday_only:
        cond = {"$and": [cond, _is_weekday(p)]}
    half = {"$cond": [{"$eq": [{"$ifNull": [_f(p, "half_day"), False]}, True]}, 0.5, 1]}
    return {"$cond": [cond, half, 0]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def count_working_days(start: date_cls, end: date_cls) -> int:
    """Mon-Fri days in [start, end] (R7)."""
    if end < start:
        return 0
    full, rest = divmod((end - start).days + 1, 7)
    return full * 5 + sum(1 for i in range(rest) if (start + timedelta(days=i)).weekday() < 5)


# --- employee monthly ------------------------------------------------------- #
def plan_monthly(emp_code: str, month: str) -> tuple[Plan, date_cls, date_cls]:
    first, last = month_range(month)
    late_pos = {"$gt": [_late(""), 0]}
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_value("", True)},
            "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": _count_if(late_pos),
            "total_late_minutes": {"$sum": {"$cond": [late_pos, _late(""), 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return Plan("attendance_logs", pipeline, hint=IDX_EMP_DATE), first, last


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    plan, first, last = plan_monthly(emp_code, month)
    emp = get_employee(emp_code)
    joined = emp.get("joined_on")
    start = first
    if isinstance(joined, str) and DATE_RE.match(joined):
        try:
            start = max(first, date_cls.fromisoformat(joined))
        except ValueError:
            pass
    working = count_working_days(start, last)
    rows = plan.run()
    r = rows[0] if rows else {}
    present = r.get("present_days", 0) or 0
    pct = None
    if working > 0:
        pct = float((Decimal(str(present)) / Decimal(working) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": half_up(present, 2),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# --- department summary ----------------------------------------------------- #
def plan_summary(month: str, department: Optional[str]) -> Plan:
    first, last = month_range(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    p = "logs."
    has_hours = {"$and": [_is_present(p), {"$isNumber": "$logs.work_hours"}]}
    late_pos = {"$gt": [_late(p), 0]}
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1, "date": 1}},
            ],
            "as": "logs",
        }},
        # keep employees with zero logs (preserveNullAndEmptyArrays); "li" is the array index
        {"$unwind": {"path": "$logs", "includeArrayIndex": "li", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            # one row per log, so count the employee only on its first row (li is 0 or null)
            "headcount": _count_if({"$lte": [{"$ifNull": ["$li", 0]}, 0]}),
            "present_days": {"$sum": _present_value(p, True)},
            # hours in hundredths so the sum is exact; the mean is rounded half-up in Python
            "hours_x100": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]}, 0]}},
            "hours_n": _count_if(has_hours),
            "late_count": _count_if(late_pos),
            "total_late_minutes": {"$sum": {"$cond": [late_pos, _late(p), 0]}},
            "leave_count": _count_if({"$eq": ["$logs.status", "LEAVE"]}),
            "on_duty_count": _count_if({"$eq": ["$logs.status", "ON_DUTY"]}),
        }},
        {"$sort": {"_id": 1}},
    ]
    return Plan("employees", pipeline, hint=IDX_JOINED if department is None else IDX_DEPT_JOINED)


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    rows = plan_summary(month, department).run()
    items = []
    for r in rows:
        n = int(r["hours_n"])
        avg = None
        if n:
            avg = float((Decimal(int(r["hours_x100"])) / Decimal(n) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# --- late leaderboard ------------------------------------------------------- #
def plan_leaderboard(month: str, limit: int, department: Optional[str]) -> Plan:
    first, last = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, not the row count
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return Plan("attendance_logs", pipeline, hint=IDX_DATE_EMP)


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = plan_leaderboard(month, limit, department).run()
    items = [{**r, "rank": int(r["rank"]), "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
             for r in rows]
    return {"month": month, "items": items}


# --- department trend ------------------------------------------------------- #
D_10000, D_HALF = Decimal128("10000"), Decimal128("0.5")


def _half_up_4(expr: Any) -> dict:
    """Round-half-up to 4 decimals inside the pipeline using exact Decimal128 maths
    ($round in MongoDB is banker's rounding)."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, D_10000]}, D_HALF]}}, D_10000]}


def plan_trend(department: str, frm: str, to: str) -> Plan:
    lo, hi = parse_date(frm, "from"), parse_date(to, "to")
    if hi < lo:
        raise HTTPException(422, "to must not be before from")
    if (hi - lo).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = sorted(e.get("joined_on") or "" for e in emps)
    start = datetime(lo.year, lo.month, lo.day, tzinfo=UTC)
    stop = datetime(hi.year, hi.month, hi.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    raw_rate = {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _present_value("", False)},
                    "late_count": _count_if({"$gt": [_late(""), 0]})}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # gap filling: one document for every calendar day in [from, to]
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, stop]}}},
        {"$set": {"ds": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}}, "dow": {"$dayOfWeek": "$d"}}},
        {"$set": {
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
            # R9: employees of the department who had joined on or before that day
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "cond": {"$lte": ["$$this", "$ds"]}}}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, _half_up_4(raw_rate), None]}}},
        # 7-day moving average over the current row and up to 6 earlier rows of THIS range
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"mavg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": "$ds", "is_working_day": 1, "headcount": 1,
            "present_count": {"$toDouble": "$present_count"}, "late_count": 1,
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$toDouble": _half_up_4("$mavg")},
        }},
    ]
    return Plan("attendance_logs", pipeline, hint=IDX_DATE_EMP)


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    rows = plan_trend(department, from_, to).run()
    for r in rows:
        pc = r["present_count"]
        r["present_count"] = int(pc) if pc == int(pc) else pc
    return {"department": department, "items": rows}


# --- explain ---------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(**values: Any) -> None:
    missing = [k for k, v in values.items() if v in (None, "")]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EXPLAIN_ENDPOINTS,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        find = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
        plan = Plan("attendance_logs", find=find)
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        plan = plan_monthly(emp_code, month)[0]
    elif endpoint == "department_summary":
        _need(month=month)
        plan = plan_summary(month, department)
    elif endpoint == "late_leaderboard":
        _need(month=month)
        plan = plan_leaderboard(month, limit, department)
    else:
        _need(department=department, **{"from": from_, "to": to})
        plan = plan_trend(department, from_, to)
    raw = db.command(plan.explain_command())
    # explain output holds BSON types (Timestamp, Binary, Int64 ...): convert to plain JSON
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return JSONResponse({"endpoint": endpoint, "collection": plan.collection, "explain": explain})
