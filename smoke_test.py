"""Smoke test against a REAL MongoDB 6.0+ (not part of the submission - keep it outside the repo).

  cd hrone-assignment
  MONGO_URI=mongodb://localhost:27017 python ../smoke_test.py

It uses its own database `attendance_smoke` and DROPS it first. Expected numbers were worked out by hand
from the seed below (July 2026: 1 Jul is a Wednesday, 4-5 Jul is a weekend).
"""
import os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

os.environ["MONGO_DB"] = "attendance_smoke"
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from app import main as m

U = timezone.utc
def dt(d, h=4, mi=0): return datetime.fromisoformat(d).replace(hour=h, minute=mi, tzinfo=U)
def emp(code, dept, joined): return {"emp_code": code, "name": code, "email": f"{code}@x.com", "department": dept,
        "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined, "created_at": dt(joined)}
def log(code, d, status="PRESENT", wh=None, late=0, ot=0, half=False):
    pin = dt(d) if status in m.PRESENCE else None
    return {"emp_code": code, "date": d, "status": status, "punch_in": pin, "punch_out": dt(d, 12) if wh else None,
            "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}

m.client.drop_database("attendance_smoke")
m.db.employees.insert_many([emp("EMP9001","Eng","2026-07-01"), emp("EMP9002","Eng","2026-07-15"), emp("EMP9003","Eng","2026-07-01"),
    emp("EMP9004","Ops","2026-07-01"), emp("EMP9005","Ops","2026-07-01"), emp("EMP9006","Ops","2026-07-01")])
m.db.attendance_logs.insert_many([
    log("EMP9001","2026-07-06",wh=9.0), log("EMP9001","2026-07-07",wh=4.0,late=20,half=True),
    log("EMP9001","2026-07-08","LEAVE"), log("EMP9001","2026-07-11",wh=8.0,late=15,ot=40),   # 11 Jul = Saturday
    log("EMP9002","2026-07-15","WFH",wh=8.0), log("EMP9002","2026-07-16","ON_DUTY",wh=8.5),
    log("EMP9004","2026-07-06",wh=8.0,late=50), log("EMP9005","2026-07-06",wh=8.0,late=30),
    log("EMP9005","2026-07-07",wh=8.0,late=20), log("EMP9006","2026-07-06",wh=8.0,late=10),
    log("GHOST","2026-07-06",late=999)])                                                    # unknown employee: must be ignored

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n   got : {got}\n   want: {want}"))

with TestClient(m.app) as c:
    check("health", c.get("/health").status_code, 200)

    r = c.get("/analytics/employees/EMP9001/monthly?month=2026-07").json()
    check("monthly EMP9001", r, {"emp_code":"EMP9001","month":"2026-07","working_days":23,"present_days":1.5,"leave_days":1,
          "late_count":2,"total_late_minutes":35,"total_overtime_minutes":40,"attendance_pct":6.52})
    r = c.get("/analytics/employees/EMP9002/monthly?month=2026-07").json()
    check("monthly mid-month joiner", (r["working_days"], r["present_days"], r["attendance_pct"]), (13, 2.0, 15.38))
    check("monthly unknown employee", c.get("/analytics/employees/NOPE/monthly?month=2026-07").status_code, 404)

    s = {i["department"]: i for i in c.get("/analytics/departments/summary?month=2026-07").json()["items"]}
    check("summary Eng", s["Eng"], {"department":"Eng","headcount":3,"present_days":3.5,"avg_work_hours":7.5,"late_count":2,
          "total_late_minutes":35,"leave_count":1,"on_duty_count":1})
    check("summary Ops headcount/late", (s["Ops"]["headcount"], s["Ops"]["late_count"], s["Ops"]["total_late_minutes"]), (3, 4, 110))
    check("summary: dept not joined yet is omitted", c.get("/analytics/departments/summary?month=2026-05").json()["items"], [])
    check("summary headcount counts joined-by-month-end only", c.get("/analytics/departments/summary?month=2026-07&department=Eng").json()["items"][0]["headcount"], 3)

    rows = c.get("/analytics/leaderboard/late?month=2026-07&limit=1").json()["items"]
    check("leaderboard tie at cutoff", [(x["rank"], x["emp_code"]) for x in rows], [(1, "EMP9004"), (1, "EMP9005")])
    rows = c.get("/analytics/leaderboard/late?month=2026-07").json()["items"]
    check("leaderboard ranks 1,1,3,4", [(x["rank"], x["emp_code"], x["total_late_minutes"]) for x in rows],
          [(1,"EMP9004",50),(1,"EMP9005",50),(3,"EMP9001",35),(4,"EMP9006",10)])
    rows = c.get("/analytics/leaderboard/late?month=2026-07&department=Ops&limit=3").json()["items"]
    check("leaderboard inside department", [(x["rank"], x["emp_code"]) for x in rows], [(1,"EMP9004"),(1,"EMP9005"),(3,"EMP9006")])

    t = c.get("/analytics/departments/Eng/trend?from=2026-07-04&to=2026-07-10").json()["items"]
    check("trend one row per day", [x["date"] for x in t], [f"2026-07-{d:02d}" for d in range(4, 11)])
    check("trend rate", [x["attendance_rate"] for x in t], [None, None, 0.5, 0.25, 0.0, 0.0, 0.0])
    check("trend moving avg", [x["moving_avg_7d"] for x in t], [None, None, 0.5, 0.375, 0.25, 0.1875, 0.15])
    check("trend headcount", [x["headcount"] for x in t], [2] * 7)
    t = c.get("/analytics/departments/Ops/trend?from=2026-07-20&to=2026-07-22").json()["items"]
    check("trend with no records at all", [(x["attendance_rate"], x["present_count"]) for x in t], [(0.0, 0)] * 3)
    check("trend unknown dept", c.get("/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02").status_code, 404)
    check("trend > 92 days", c.get("/analytics/departments/Eng/trend?from=2026-01-01&to=2026-06-01").status_code, 422)

    # concurrency: 20 identical punch-ins -> exactly one 201
    body = {"emp_code": "EMP9003", "punched_at": 1783312500000}
    with ThreadPoolExecutor(20) as ex:
        codes = sorted(f.result().status_code for f in [ex.submit(c.post, "/attendance/punch-in", json=body) for _ in range(20)])
    check("20 parallel punch-ins", (codes.count(201), codes.count(409)), (1, 19))
    out = {"emp_code": "EMP9003", "punched_at": 1783312500000 + 9 * 3600 * 1000}
    with ThreadPoolExecutor(20) as ex:
        codes = sorted(f.result().status_code for f in [ex.submit(c.post, "/attendance/punch-out", json=out) for _ in range(20)])
    check("20 parallel punch-outs", (codes.count(200), codes.count(409)), (1, 19))

    # explain: IXSCAN present, COLLSCAN absent
    q = {"attendance_list": "", "employee_monthly": "?emp_code=EMP9001&month=2026-07", "department_summary": "?month=2026-07",
         "late_leaderboard": "?month=2026-07", "department_trend": "?department=Eng&from=2026-07-01&to=2026-07-31"}
    for ep, qs in q.items():
        r = c.get(f"/admin/explain/{ep}{qs}")
        txt = r.text
        check(f"explain {ep}", (r.status_code, "IXSCAN" in txt, "COLLSCAN" in txt), (200, True, False))

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
