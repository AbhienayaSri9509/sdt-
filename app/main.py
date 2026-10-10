"""
Employee Attendance & Analytics API
FastAPI + MongoDB implementation meeting OpenAPI 3.0.3 contract and business rules R1-R10.
"""

import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB configuration - environment variables take precedence
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# IST timezone: UTC + 05:30
IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)])
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def parse_epoch_ms(val: Any) -> datetime:
    """
    Validate and convert integer epoch milliseconds to UTC datetime.
    Truncates to whole seconds per R1.
    Rejects seconds-looking values (< 100,000,000,000), floats, strings, bools, and out-of-range numbers with 422.
    """
    if val is None or isinstance(val, bool) or not isinstance(val, int):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Timestamp must be an integer count of epoch milliseconds.",
        )
    if not (100_000_000_000 <= val <= 4_102_444_800_000):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Timestamp out of valid epoch milliseconds range.",
        )
    # Truncate to whole seconds per R1
    sec = val // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC/naive datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        # Check if punch_in IST time is strictly earlier than shift_end
        punch_time = punch_in_ist.time()
        if (punch_time.hour, punch_time.minute, punch_time.second) < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date_str: str) -> int:
    """
    R2 Late. Grace period is 10 minutes.
    A punch-in is late only if it is strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start - not since the grace period.
    Otherwise 0.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(att_date, time(start_h, start_m), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec <= 0:
        return 0
    # Strictly more than 10 minutes (600 seconds)
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R3 Overtime.
    overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    Handles overnight shifts where shift_end falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date
    shift_end_dt = datetime.combine(shift_end_date, time(end_h, end_m), tzinfo=IST)

    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec < 0:
        return 0
    diff_min = int(diff_sec // 60)
    if diff_min >= 30:
        return diff_min
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    val = Decimal(str(diff_sec)) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: float) -> bool:
    """
    R5 Half day.
    After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    return work_hours < 4.50


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a history entry document for the HTTP response."""
    changes_out = {}
    for field_name, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field_name in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
        changes_out[field_name] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = dt_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_out,
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convert MongoDB attendance log document to AttendanceRecord response schema."""
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convert MongoDB employee document to Employee response schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid date format, expected YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Punch-in status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status value")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces uniqueness via unique index on emp_code.
    shift_start must differ from shift_end.
    """
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shift_start and shift_end must differ",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in.
    Enforces concurrency via unique index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = parse_epoch_ms(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = compute_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes the employee's most recent record whose punch_in is at or before punched_at.
    Handles overnight shifts and simultaneous punch-outs atomically.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = parse_epoch_ms(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the employee's most recent attendance record whose punch_in is at or before punch_out_utc
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record or record.get("punch_in") is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in record found for punch-out",
        )

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or more than 24 hours after it -> 422
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be after punch_in and within 24 hours of it",
        )

    # Compute derived fields
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_min = compute_overtime_minutes(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    half = is_half_day(work_hrs)

    # Atomic update with condition punch_out == None
    updated_doc = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half,
            }
        },
        return_document=True,
    )

    if updated_doc is None:
        # Another simultaneous punch-out already closed this record
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    return serialize_attendance_doc(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Streams via database cursor pagination.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_val}", tags=["attendance"])
def regularize_attendance(emp_code: str, date_val: str, body: RegularizeRequest):
    """
    Regularize (manually correct) one day's record.
    Addressable by (emp_code, date).
    Optimistic concurrency control via history versioning prevents lost updates.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_val})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Business rule: setting status ABSENT or LEAVE clears both punch times;
    # supplying punch_in or punch_out with it is 422.
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Punch times cannot be supplied when regularizing to ABSENT or LEAVE",
            )

    new_status = body.status if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
    else:
        # A presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            target_punch_in = parse_epoch_ms(body.punch_in)
        else:
            target_punch_in = rec.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )
        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date per R1
        in_att_date = compute_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if in_att_date != date_val:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in attendance date ({in_att_date}) does not match record date ({date_val})",
            )

        if body.punch_out is not None:
            target_punch_out = parse_epoch_ms(body.punch_out)
        else:
            target_punch_out = rec.get("punch_out")

        if target_punch_out is not None:
            if target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
            # punch_out must be after punch_in and within 24 hours
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours of it",
                )

    # Compute target derived values
    if new_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_val)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime_minutes(
                target_punch_out, emp["shift_start"], emp["shift_end"], date_val
            )
            target_half_day = is_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Check for actual changes
    field_changes: Dict[str, Dict[str, Any]] = {}

    def normalize_dt(d: Optional[datetime]) -> Optional[datetime]:
        if d is None:
            return None
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    old_punch_in = normalize_dt(rec.get("punch_in"))
    old_punch_out = normalize_dt(rec.get("punch_out"))

    if rec.get("status") != new_status:
        field_changes["status"] = {"from": rec.get("status"), "to": new_status}
    if old_punch_in != target_punch_in:
        field_changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if old_punch_out != target_punch_out:
        field_changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if rec.get("work_hours") != target_work_hours:
        field_changes["work_hours"] = {"from": rec.get("work_hours"), "to": target_work_hours}
    if rec.get("late_minutes", 0) != target_late_minutes:
        field_changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": target_late_minutes}
    if rec.get("overtime_minutes", 0) != target_overtime_minutes:
        field_changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": target_overtime_minutes}
    if rec.get("half_day", False) != target_half_day:
        field_changes["half_day"] = {"from": rec.get("half_day", False), "to": target_half_day}

    if not field_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request modifies no fields",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": field_changes,
    }

    # Optimistic locking: check current history array size to avoid losing concurrent history entries
    expected_history_len = len(rec.get("history", []))

    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date_val,
            f"history.{expected_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict during regularization",
        )

    return serialize_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    # Calculate working days for mid-month joiners per R7
    joined_date = date.fromisoformat(emp["joined_on"])
    working_days = 0
    for day in range(1, num_days + 1):
        cur_date = date(year, m, day)
        if cur_date >= joined_date and cur_date.weekday() < 5:
            working_days += 1

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        data = agg_res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late = int(data.get("total_late_minutes", 0))
        total_ot = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB cross-collection aggregation.
    Headcount per R9 includes all employees joined on or before period end.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "all_emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$all_emps"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        headcount = r["headcount"]
        if headcount == 0:
            continue
        items.append(
            {
                "department": r["department"],
                "headcount": headcount,
                "present_days": float(r["present_days"]),
                "avg_work_hours": round_half_up(r["avg_work_hours"], 2),
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month using $rank window function.
    Returns every employee whose rank <= limit.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": "$total_late_minutes",
                    "late_count": "$late_count",
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{dept_name}/trend", tags=["analytics"])
def department_trend(
    dept_name: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department with $densify gap-filling.
    Exactly one row per calendar day from from to to.
    """
    try:
        dt_from = date.fromisoformat(date_from)
        dt_to = date.fromisoformat(date_to)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dates must be in YYYY-MM-DD format",
        )

    if dt_to < dt_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to date cannot be earlier than from date",
        )

    num_days = (dt_to - dt_from).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    # Check department exists
    dept_emps = list(
        db.employees.find({"department": dept_name}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    )
    if not dept_emps:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    bounds_start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    bounds_end = (datetime.fromisoformat(date_to) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"department": dept_name}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", date_from]},
                                    {"$lte": ["$date", date_to]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "emp_code": 1,
                "joined_on": 1,
                "department": 1,
                "date": "$logs.date",
                "date_obj": {
                    "$cond": [
                        {"$ne": ["$logs.date", None]},
                        {"$dateFromString": {"dateString": "$logs.date"}},
                        bounds_start,
                    ]
                },
                "status": "$logs.status",
                "half_day": "$logs.half_day",
                "late_minutes": "$logs.late_minutes",
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [bounds_start, bounds_end],
                },
            }
        },
        {
            "$addFields": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}}
            }
        },
        {
            "$group": {
                "_id": "$date_str",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$status", None]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$status", None]},
                                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    raw_days = list(db.employees.aggregate(pipeline))

    items = []
    rate_history: List[Optional[float]] = []

    for d_item in raw_days:
        day_str = d_item["_id"]
        cur_d = date.fromisoformat(day_str)
        is_working_day = cur_d.weekday() < 5
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= day_str)
        present_count = float(d_item["present_count"])
        late_count = int(d_item["late_count"])

        if is_working_day and headcount > 0:
            raw_rate = present_count / headcount
            att_rate = round_half_up(raw_rate, 4)
        else:
            att_rate = None

        rate_history.append(att_rate)
        window = rate_history[-7:]
        non_null_window = [r for r in window if r is not None]
        if non_null_window:
            moving_avg = round_half_up(sum(non_null_window) / len(non_null_window), 4)
        else:
            moving_avg = None

        items.append(
            {
                "date": day_str,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": att_rate,
                "moving_avg_7d": moving_avg,
            }
        )

    return {"department": dept_name, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, alias="date_from"),
    date_to: Optional[str] = Query(default=None, alias="date_to"),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity on the named endpoint's main query or aggregation pipeline.
    Valid endpoint names:
    attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid endpoint name. Must be one of {valid_endpoints}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        command_doc = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        exp = db.command("explain", command_doc, verbosity="executionStats")
        clean_exp = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {
                "$group": {
                    "_id": None,
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_exp = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "all_emps": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "valid_work_hours": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                    ]
                                },
                                "$logs.work_hours",
                                "$$REMOVE",
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$all_emps"},
                    "present_days": "$present_days",
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                            {"$avg": "$valid_work_hours"},
                            None,
                        ]
                    },
                    "late_count": "$late_count",
                    "total_late_minutes": "$total_late_minutes",
                    "leave_count": "$leave_count",
                    "on_duty_count": "$on_duty_count",
                }
            },
            {"$sort": {"department": ASCENDING}},
        ]
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_exp = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend(
            [
                {
                    "$project": {
                        "_id": 0,
                        "emp_code": "$_id",
                        "name": "$emp.name",
                        "department": "$emp.department",
                        "total_late_minutes": "$total_late_minutes",
                        "late_count": "$late_count",
                    }
                },
                {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": DESCENDING},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
            ]
        )
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_exp = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    elif endpoint == "department_trend":
        dept = department
        f_date = from_date or date_from
        t_date = to_date or date_to
        if not dept or not f_date or not t_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to parameters are required for department_trend",
            )
        try:
            dt_from = date.fromisoformat(f_date)
            dt_to = date.fromisoformat(t_date)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if dt_to < dt_from or (dt_to - dt_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        bounds_start = datetime.fromisoformat(f_date).replace(tzinfo=timezone.utc)
        bounds_end = (datetime.fromisoformat(t_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

        pipeline = [
            {"$match": {"department": dept}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", f_date]},
                                        {"$lte": ["$date", t_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "emp_code": 1,
                    "joined_on": 1,
                    "department": 1,
                    "date": "$logs.date",
                    "date_obj": {
                        "$cond": [
                            {"$ne": ["$logs.date", None]},
                            {"$dateFromString": {"dateString": "$logs.date"}},
                            bounds_start,
                        ]
                    },
                    "status": "$logs.status",
                    "half_day": "$logs.half_day",
                    "late_minutes": "$logs.late_minutes",
                }
            },
            {
                "$densify": {
                    "field": "date_obj",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [bounds_start, bounds_end],
                    },
                }
            },
            {
                "$addFields": {
                    "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}}
                }
            },
            {
                "$group": {
                    "_id": "$date_str",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$status", None]},
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$status", None]},
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": ASCENDING}},
        ]
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        clean_exp = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_exp}
