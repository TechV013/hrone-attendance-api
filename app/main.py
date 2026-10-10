"""Employee Attendance & Analytics API for the HROne Software Engineer Trainee assignment.

Run from the repository root with:
    uvicorn app.main:app --port 8000

Configuration is read from MONGO_URI and MONGO_DB. When MONGO_URI is not set, PyMongo's
standard local default is used for developer convenience.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
from threading import Lock
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# Do not let a .env file override values injected into the process environment.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
HISTORY_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)
logger = logging.getLogger("hrone_attendance")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
_INDEX_LOCK = Lock()
_indexes_ready = False


# --------------------------------------------------------------------------- #
# Validation and time helpers
# --------------------------------------------------------------------------- #
def _parse_date(value: str) -> Date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    parsed = Date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def _month_bounds(month: str) -> tuple[str, str, datetime, datetime, int]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        year, mon = (int(part) for part in month.split("-"))
        last_day = calendar.monthrange(year, mon)[1]
        start_date = Date(year, mon, 1)
        end_date = Date(year, mon, last_day)
    except (ValueError, OverflowError):
        raise HTTPException(422, "invalid month")
    start_s, end_s = start_date.isoformat(), end_date.isoformat()
    start_dt = datetime.combine(start_date, time.min, UTC)
    try:
        end_exclusive_dt = datetime.combine(end_date + timedelta(days=1), time.min, UTC)
    except OverflowError:
        # The inclusive end date is still a valid calendar date; only the unused exclusive bound overflows.
        end_exclusive_dt = datetime.max.replace(tzinfo=UTC)
    return start_s, end_s, start_dt, end_exclusive_dt, last_day


def _utc_datetime(value: datetime) -> datetime:
    """Treat PyMongo's default naive datetimes as UTC and return an aware UTC value."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_seconds(value: datetime) -> datetime:
    return _utc_datetime(value).replace(microsecond=0)


def _epoch_ms_to_datetime(value: int) -> datetime:
    # Values have already been checked as strict integers in the request model.
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    # The contract deliberately truncates input instants to whole seconds.
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def _datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    normalized = _utc_datetime(value)
    return int(normalized.timestamp()) * 1000 + normalized.microsecond // 1000


def _now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _round_half_up(value: Optional[float | int | Decimal], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _attendance_date(punch_in: datetime, employee: dict[str, Any]) -> str:
    """Return the IST shift-start date (R1), including overnight early-morning punches."""
    local = _truncate_seconds(punch_in).astimezone(IST)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]
    is_overnight = shift_end <= shift_start
    end_hour, end_minute = (int(part) for part in shift_end.split(":"))
    local_clock = (local.hour, local.minute, local.second)
    end_clock = (end_hour, end_minute, 0)
    day = local.date()
    if is_overnight and local_clock < end_clock:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    """Whole elapsed minutes after shift start, but only beyond the strict 10-minute grace."""
    shift_hour, shift_minute = (int(part) for part in shift_start.split(":"))
    day = _parse_date(attendance_day)
    start = datetime.combine(day, time(shift_hour, shift_minute), IST)
    elapsed_seconds = (_truncate_seconds(punch_in) - start.astimezone(UTC)).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((_truncate_seconds(punch_out) - _truncate_seconds(punch_in)).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str, shift_start: str) -> int:
    end_hour, end_minute = (int(part) for part in shift_end.split(":"))
    end_day = _parse_date(attendance_day)
    if shift_end <= shift_start:  # Overnight shift: shift end is on the following calendar day.
        end_day += timedelta(days=1)
    shift_end_dt = datetime.combine(end_day, time(end_hour, end_minute), IST).astimezone(UTC)
    elapsed_seconds = (_truncate_seconds(punch_out) - shift_end_dt).total_seconds()
    if elapsed_seconds <= 0:
        return 0
    whole_minutes = int(elapsed_seconds // 60)
    return whole_minutes if whole_minutes >= 30 else 0


def _computed_fields(
    employee: dict[str, Any], attendance_day: str, status: str,
    punch_in: Optional[datetime], punch_out: Optional[datetime],
) -> dict[str, Any]:
    if status in ("ABSENT", "LEAVE"):
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    if punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    punch_in = _truncate_seconds(punch_in)
    if _attendance_date(punch_in, employee) != attendance_day:
        raise HTTPException(422, "punch_in must belong to the record's attendance date")
    late_minutes = compute_late_minutes(punch_in, employee["shift_start"], attendance_day)
    if punch_out is None:
        return {
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }
    punch_out = _truncate_seconds(punch_out)
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0:
        raise HTTPException(422, "punch_out must be after punch_in")
    if seconds > 24 * 60 * 60:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": compute_overtime(
            punch_out, employee["shift_end"], attendance_day, employee["shift_start"]
        ),
        "half_day": Decimal(str(work_hours)) < Decimal("4.50"),
    }


def _normalize_change_value(field_name: str, value: Any) -> Any:
    if field_name in ("punch_in", "punch_out"):
        return _datetime_to_epoch_ms(value) if value is not None else None
    return value


def _same_value(field_name: str, left: Any, right: Any) -> bool:
    if field_name in ("punch_in", "punch_out"):
        return _normalize_change_value(field_name, left) == _normalize_change_value(field_name, right)
    if field_name == "work_hours":
        if left is None or right is None:
            return left is right
        return Decimal(str(left)) == Decimal(str(right))
    return left == right


def _public_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _datetime_to_epoch_ms(doc["created_at"]),
    }


def _public_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in doc.get("history", []) or []:
        changes = {}
        for field_name, pair in (entry.get("changes") or {}).items():
            changes[field_name] = {
                "from": _normalize_change_value(field_name, pair.get("from")),
                "to": _normalize_change_value(field_name, pair.get("to")),
            }
        history.append({
            "at": _datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": _datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def _require_optional_timestamp_was_not_null(model: BaseModel, field_name: str) -> None:
    if field_name in model.model_fields_set and getattr(model, field_name) is None:
        raise HTTPException(422, f"{field_name} must be an integer epoch-millisecond timestamp")


def _validate_date_range(date_from: Optional[Date], date_to: Optional[Date]) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")


def _build_date_array(start_dt: datetime, count: int) -> dict[str, Any]:
    return {
        "$map": {
            "input": {"$range": [0, count]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}},
        }
    }


def _mongo_round_half_up(expression: Any, places: int) -> dict[str, Any]:
    """Mongo expression for nonnegative values: round half-up (Mongo $round is half-even)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def _weekday_condition(date_expression: Any) -> dict[str, Any]:
    # Mongo $dayOfWeek numbers Sunday=1, Monday=2, ..., Saturday=7.
    return {"$in": [{"$dayOfWeek": date_expression}, [2, 3, 4, 5, 6]]}


def _attendance_query(
    emp_code: Optional[str], date_from: Optional[Date], date_to: Optional[Date], status: Optional[str]
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter: dict[str, str] = {}
        if date_from is not None:
            date_filter["$gte"] = date_from.isoformat()
        if date_to is not None:
            date_filter["$lte"] = date_to.isoformat()
        query["date"] = date_filter
    if status:
        query["status"] = status
    return query


def _build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start_s, end_s, start_dt, _end_exclusive, last_day = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {"_month_days": _build_date_array(start_dt, last_day)}},
        {"$set": {"working_days": {"$size": {"$filter": {
            "input": "$_month_days", "as": "day",
            "cond": {"$and": [
                {"$gte": [{"$dateToString": {"date": "$$day", "format": "%Y-%m-%d", "timezone": "UTC"}}, "$joined_on"]},
                _weekday_condition("$$day"),
            ]},
        }}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$emp"]},
                    {"$gte": ["$date", start_s]},
                    {"$lte": ["$date", end_s]},
                    {"$gte": ["$date", "$$joined"]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "month": {"$first": month},
            "working_days": {"$first": "$working_days"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, PRESENCE_STATUSES]},
                    _weekday_condition({"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}),
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
        }},
        {"$set": {"attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]},
            {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]},
            None,
        ]}}},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "month": 1, "working_days": 1,
            "present_days": 1, "leave_days": 1, "late_count": 1,
            "total_late_minutes": 1, "total_overtime_minutes": 1, "attendance_pct": 1,
        }},
    ]


def _build_department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start_s, end_s, _start_dt, _end_exclusive, _last_day = _month_bounds(month)
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department is not None:
        employee_match["department"] = department
    log_present_weekday = {"$and": [
        {"$in": [{"$ifNull": ["$logs.status", ""]}, PRESENCE_STATUSES]},
        _weekday_condition({"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}),
    ]}
    log_present_worked = {"$and": [
        {"$in": [{"$ifNull": ["$logs.status", ""]}, PRESENCE_STATUSES]},
        {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
    ]}
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$emp"]},
                    {"$gte": ["$date", start_s]},
                    {"$lte": ["$date", end_s]},
                    {"$gte": ["$date", "$$joined"]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1,
                              "late_minutes": 1, "overtime_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                log_present_weekday,
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [log_present_worked, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [log_present_worked, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                None,
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def _build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict[str, Any]]:
    start_s, end_s, *_ = _month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"
        }},
        {"$unwind": "$employee"},  # Logs with no matching employee are intentionally ignored.
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # The rank sort deliberately has no emp_code tie-breaker: equal totals share a rank.
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},  # Contract applies limit after ranking.
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": 1, "department": 1,
            "total_late_minutes": 1, "late_count": 1, "rank": 1,
        }},
    ])
    return pipeline


def _build_department_trend_pipeline(department: str, date_from: Date, date_to: Date) -> list[dict[str, Any]]:
    day_count = (date_to - date_from).days + 1
    from_dt = datetime.combine(date_from, time.min, UTC)
    return [
        # This indexed employee match is the pipeline anchor; dates are generated in MongoDB, not Python.
        {"$match": {"department": department}},
        {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
        {"$set": {"_days": _build_date_array(from_dt, day_count)}},
        {"$unwind": "$_days"},
        {"$set": {
            "_day_str": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "_is_working_day": _weekday_condition("$_days"),
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code", "day": "$_day_str"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$emp"]},
                    {"$eq": ["$date", "$$day"]},
                ]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "_log",
        }},
        {"$unwind": {"path": "$_log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$_days",
            "date": {"$first": "$_day_str"},
            "is_working_day": {"$first": "$_is_working_day"},
            "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_day_str"]}, 1, 0]}},
            "present_count": {"$sum": {"$cond": [
                {"$in": [{"$ifNull": ["$_log.status", ""]}, PRESENCE_STATUSES]},
                {"$cond": [{"$eq": [{"$ifNull": ["$_log.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_log.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _mongo_round_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"_moving_avg_raw": {
                "$avg": "$attendance_rate", "window": {"documents": [-6, 0]}
            }},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$_moving_avg_raw", None]},
                None,
                _mongo_round_half_up("$_moving_avg_raw", 4),
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


def _explain_collection_aggregate(
    collection_name: str, pipeline: list[dict[str, Any]], hint: dict[str, int]
) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True, "hint": hint},
        verbosity="executionStats",
    )



def _explain_find(
    query: dict[str, Any],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    # Employee-specific queries should use the employee/date index.
    # Otherwise, use the date-first index for date-ordered listing.
    index_hint = (
        {"emp_code": ASCENDING, "date": ASCENDING}
        if "emp_code" in query
        else {"date": DESCENDING, "emp_code": ASCENDING}
    )

    return db.command(
        "explain",
        {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": index_hint,
        },
        verbosity="executionStats",
    )


def _create_indexes() -> None:
    """Create the required indexes idempotently at startup."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="employee_joined_department")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING), ("date", DESCENDING)],
        name="attendance_employee_punchin",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp_code"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_late_emp",
    )


def _ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    with _INDEX_LOCK:
        if _indexes_ready:
            return
        _create_indexes()
        _indexes_ready = True


@app.on_event("startup")
def startup() -> None:
    try:
        client.admin.command("ping")
        _ensure_indexes()
    except PyMongoError:
        # Keep the process alive so /health can report 503 while MongoDB is unavailable.
        logger.exception("MongoDB is not ready; health checks will retry index creation")


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    @classmethod
    def trim_string_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_calendar_date(cls, value: str) -> str:
        _parse_date(value)
        return value

    @model_validator(mode="after")
    def shift_must_have_duration(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        if not self.name or not self.department:
            raise ValueError("name and department must not be blank")
        if not self.email:
            raise ValueError("email must not be blank")
        return self


EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class PunchInIn(APIModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(APIModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(APIModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("reason", "regularized_by")
    @classmethod
    def strip_audit_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def reject_explicit_null_change_fields(self) -> "RegularizeIn":
        for field_name in ("status", "punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null; omit it to leave it unchanged")
        if len(self.reason) < 5:
            raise ValueError("reason must contain at least 5 non-padding characters")
        if not self.regularized_by:
            raise ValueError("regularized_by must not be blank")
        return self


# --------------------------------------------------------------------------- #
# System and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
        _ensure_indexes()
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable or indexes are not ready")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError:
        logger.exception("Failed to create employee")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _public_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [_public_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    _require_optional_timestamp_was_not_null(body, "punched_at")
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = _truncate_seconds(_epoch_ms_to_datetime(body.punched_at)) if body.punched_at is not None else _now_utc_seconds()
    attendance_day = _attendance_date(punched_at, employee)
    record = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        **_computed_fields(employee, attendance_day, body.status, punched_at, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this attendance date")
    return _public_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    _require_optional_timestamp_was_not_null(body, "punched_at")
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = _truncate_seconds(_epoch_ms_to_datetime(body.punched_at)) if body.punched_at is not None else _now_utc_seconds()
    current = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if current is None:
        # Distinguish a bad early timestamp from an employee who has never punched in.
        latest_punch = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$type": "date"}},
            sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
        )
        if latest_punch is not None and punched_at <= _truncate_seconds(latest_punch["punch_in"]):
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="no punch-in found before the supplied time")
    if current.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that attendance record is already punched out")
    punch_in_at = _truncate_seconds(current["punch_in"])
    elapsed_seconds = (punched_at - punch_in_at).total_seconds()
    if elapsed_seconds <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    computed = _computed_fields(employee, current["date"], current["status"], punch_in_at, punched_at)
    # Compare-and-set: concurrent requests can close the same record only once.
    condition: dict[str, Any] = {"_id": current["_id"], "punch_in": current["punch_in"]}
    if "punch_out" in current:
        condition["punch_out"] = current["punch_out"]
    else:
        condition["punch_out"] = {"$exists": False}
    if current.get("punch_out") is None:
        condition["$or"] = [{"punch_out": None}, {"punch_out": {"$exists": False}}]
        condition.pop("punch_out", None)
    result = db.attendance_logs.update_one(condition, {"$set": computed})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record was punched out concurrently")
    updated = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": current["date"]})
    return _public_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    _validate_date_range(date_from, date_to)
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    index_hint = (
        [("emp_code", ASCENDING), ("date", ASCENDING)]
        if emp_code is not None
        else [("date", DESCENDING), ("emp_code", ASCENDING)]
    )

    cursor = (
        db.attendance_logs.find(query)
        .hint(index_hint)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [_public_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: Date, body: RegularizeIn) -> dict[str, Any]:
    attendance_day = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day})
    if current is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    fields_set = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and (("punch_in" in fields_set) or ("punch_out" in fields_set)):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot be combined with punch times")

    status = body.status if "status" in fields_set else current["status"]
    punch_in_at = _epoch_ms_to_datetime(body.punch_in) if "punch_in" in fields_set else current.get("punch_in")
    punch_out_at = _epoch_ms_to_datetime(body.punch_out) if "punch_out" in fields_set else current.get("punch_out")
    final_fields = _computed_fields(employee, attendance_day, status, punch_in_at, punch_out_at)
    candidate = {"status": status, **final_fields}

    old_values = {
        "status": current.get("status"),
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    changes: dict[str, Any] = {}
    for field_name in HISTORY_FIELDS:
        old_value = old_values[field_name]
        new_value = candidate[field_name]
        if not _same_value(field_name, old_value, new_value):
            changes[field_name] = {
                "from": _normalize_change_value(field_name, old_value),
                "to": _normalize_change_value(field_name, new_value),
            }
    if not changes:
        raise HTTPException(status_code=422, detail="regularization does not change the record")

    audit_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            field_name: {
                "from": _history_storage_value(field_name, pair["from"]),
                "to": _history_storage_value(field_name, pair["to"]),
            }
            for field_name, pair in changes.items()
        },
    }

    # Optimistic concurrency: match the snapshot, including history. A competing correction makes this update miss.
    condition = {"_id": current["_id"]}
    for field_name in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if field_name in current:
            condition[field_name] = current[field_name]
        elif field_name in ("late_minutes", "overtime_minutes", "half_day"):
            condition[field_name] = {"$exists": False}
        else:
            condition[field_name] = {"$exists": False}
    if "history" in current:
        condition["history"] = current["history"]
    else:
        condition["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(condition, {"$set": candidate, "$push": {"history": audit_entry}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently; retry the correction")
    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day})
    return _public_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints - all record analytics run in MongoDB aggregation.
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    # Also validates the actual calendar month (e.g. rejects a year outside datetime's range).
    pipeline = _build_employee_monthly_pipeline(emp_code, month)
    result = next(db.employees.aggregate(pipeline, allowDiskUse=True, hint={"emp_code": 1}), None)
    if result is None:
        raise HTTPException(status_code=404, detail="employee not found")
    if result.get("attendance_pct") is not None:
        result["attendance_pct"] = _round_half_up(result["attendance_pct"], 4)
    result["present_days"] = float(result.get("present_days", 0))
    return result


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    pipeline = _build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline, allowDiskUse=True, hint={"joined_on": 1, "department": 1}))
    items = []
    for item in results:
        item["present_days"] = float(item.get("present_days", 0))
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = _round_half_up(item["avg_work_hours"], 2)
        items.append(item)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    pipeline = _build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(
        pipeline, allowDiskUse=True, hint={"date": 1, "late_minutes": 1, "emp_code": 1}
    ))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Date = Query(alias="from"),
    to: Date = Query(),
) -> dict[str, Any]:
    if to < from_:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to - from_).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 calendar days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = _build_department_trend_pipeline(department, from_, to)
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True, hint={"department": 1, "joined_on": 1}))
    # Numbers are rounded half-up inside MongoDB so the window average uses the reported daily rates.
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint - runs the same builders as the corresponding real endpoint.
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_: Optional[Date] = Query(default=None, alias="from"),
    to: Optional[Date] = None,
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        _validate_date_range(date_from, date_to)
        query = _attendance_query(emp_code, date_from, date_to, status)
        explain_doc = _explain_find(query, page, page_size)
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        pipeline = _build_employee_monthly_pipeline(emp_code, month)
        explain_doc = _explain_collection_aggregate("employees", pipeline, {"emp_code": 1})
        collection_name = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        pipeline = _build_department_summary_pipeline(month, department)
        explain_doc = _explain_collection_aggregate("employees", pipeline, {"joined_on": 1, "department": 1})
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = _build_late_leaderboard_pipeline(month, limit, department)
        explain_doc = _explain_collection_aggregate("attendance_logs", pipeline, {"date": 1, "late_minutes": 1, "emp_code": 1})
        collection_name = "attendance_logs"
    else:  # department_trend
        if not department or from_ is None or to is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        if to < from_:
            raise HTTPException(status_code=422, detail="to must be on or after from")
        if (to - from_).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range cannot exceed 92 calendar days")
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="department not found")
        pipeline = _build_department_trend_pipeline(department, from_, to)
        explain_doc = _explain_collection_aggregate("employees", pipeline, {"department": 1, "joined_on": 1})
        collection_name = "employees"

    # Extended JSON keeps BSON-only values JSON-compatible for FastAPI response encoding.
    return {"endpoint": endpoint, "collection": collection_name, "explain": json.loads(json_util.dumps(explain_doc))}


def _history_storage_value(field_name: str, public_value: Any) -> Any:
    """Convert public epoch-ms values in the audit entry back to BSON datetimes."""
    if field_name in ("punch_in", "punch_out"):
        return _epoch_ms_to_datetime(public_value) if public_value is not None else None
    return public_value
