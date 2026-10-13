import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, ObjectId, Timestamp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all indexes idempotently at startup to guarantee IXSCAN and concurrency safety
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("status", ASCENDING), ("date", DESCENDING)]
    )
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers (Rules R1 - R8 & Serialization)
# --------------------------------------------------------------------------- #
def round_half_up(value: float | Decimal, decimals: int = 2) -> float:
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def mongo_round_half_up(expr: Any, decimals: int) -> dict[str, Any]:
    """MongoDB aggregation expression for non-negative half-up rounding (Rule R8)."""
    factor = 10**decimals
    return {
        "$toDouble": {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": [{"$toDecimal": expr}, factor]},
                            {"$toDecimal": "0.5"},
                        ]
                    }
                },
                factor,
            ]
        }
    }


def ms_to_utc_dt(ms: int) -> datetime:
    """Truncates epoch ms to whole seconds (Rule R1) and returns UTC datetime."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Current UTC time truncated to whole seconds (Rule R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts stored BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Computes the IST attendance date for a punch_in instant (Rule R1)."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    if shift_end <= shift_start and (
        punch_in_ist.hour,
        punch_in_ist.minute,
        punch_in_ist.second,
    ) < (eh, em, 0):
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def get_shift_bounds(
    date_str: str, shift_start: str, shift_end: str
) -> tuple[datetime, datetime]:
    """Returns (shift_start_dt, shift_end_dt) in IST for the given attendance date."""
    att_date = date.fromisoformat(date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_dt = datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)
    end_date = att_date + timedelta(days=1) if shift_end <= shift_start else att_date
    end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    return start_dt, end_dt


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    date_str: Optional[str] = None,
    shift_end: str = "18:30",
) -> int:
    """Rule R2: Grace period 10:00. Strictly > 600s late gives floor(elapsed_minutes)."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if date_str is None:
        date_str = get_attendance_date(punch_in, shift_start, shift_end)
    start_dt, _ = get_shift_bounds(date_str, shift_start, shift_end)
    diff_seconds = int((punch_in - start_dt).total_seconds())
    return diff_seconds // 60 if diff_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Rule R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    diff_seconds = Decimal(int((punch_out - punch_in).total_seconds()))
    hours = diff_seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30"
) -> int:
    """Rule R3: Whole minutes (floor) from shift_end to punch_out, if >= 30 else 0."""
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    _, end_dt = get_shift_bounds(date_str, shift_start, shift_end)
    diff_seconds = int((punch_out - end_dt).total_seconds())
    minutes = diff_seconds // 60
    return minutes if minutes >= 30 else 0


def get_month_bounds(month_str: str) -> tuple[str, str]:
    """Returns (first_day_str, last_day_str) for a YYYY-MM string."""
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """Rule R7: Mon-Fri in the month on or after joined_on."""
    start_str, end_str = get_month_bounds(month_str)
    if joined_on_str > end_str:
        return 0
    cur = max(date.fromisoformat(start_str), date.fromisoformat(joined_on_str))
    end_d = date.fromisoformat(end_str)
    count = 0
    while cur <= end_d:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def sanitize_bson(obj: Any) -> Any:
    """Converts BSON types in raw explain outputs into JSON-safe primitives."""
    if isinstance(obj, dict):
        return {k: sanitize_bson(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_bson(v) for v in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Timestamp):
        return {"t": obj.time, "i": obj.inc}
    if isinstance(obj, Decimal128):
        return float(obj.to_decimal())
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_attendance(doc: dict) -> dict:
    raw_history = doc.get("history") or []
    serialized_history = []
    for entry in raw_history:
        changes_out = {}
        for field_name, delta in (entry.get("changes") or {}).items():
            if field_name in ("punch_in", "punch_out"):
                changes_out[field_name] = {
                    "from": dt_to_ms(delta.get("from"))
                    if isinstance(delta.get("from"), datetime)
                    else delta.get("from"),
                    "to": dt_to_ms(delta.get("to"))
                    if isinstance(delta.get("to"), datetime)
                    else delta.get("to"),
                }
            else:
                changes_out[field_name] = {
                    "from": delta.get("from"),
                    "to": delta.get("to"),
                }
        serialized_history.append(
            {
                "at": dt_to_ms(entry["at"])
                if isinstance(entry.get("at"), datetime)
                else entry.get("at"),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes_out,
            }
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": serialized_history,
    }


# --------------------------------------------------------------------------- #
# Validation Types & Models
# --------------------------------------------------------------------------- #
def check_epoch_ms(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Instant must be an integer in epoch milliseconds")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Instant must be between 100000000000 and 4102444800000")
    return v


def check_iso_date(v: str) -> str:
    if not isinstance(v, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        raise ValueError("Date must be YYYY-MM-DD")
    date.fromisoformat(v)
    return v


def check_month_str(v: str) -> str:
    if not isinstance(v, str) or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", v):
        raise ValueError("Month must be YYYY-MM")
    return v


EpochMillis = Annotated[int, BeforeValidator(check_epoch_ms)]
DateStr = Annotated[str, AfterValidator(check_iso_date)]
MonthStr = Annotated[str, AfterValidator(check_month_str)]
StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainEndpointLiteral = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: DateStr

    @model_validator(mode="after")
    def validate_shifts_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusLiteral = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusLiteral] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Query & Pipeline Builders (Shared by Analytics and Explain Endpoints)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def build_employee_monthly_pipeline(
    emp_code: str, month: str, working_days: int
) -> list[dict[str, Any]]:
    month_start, month_end = get_month_bounds(month)
    is_weekday_expr = {
        "$lte": [
            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            5,
        ]
    }
    present_val_expr = {
        "$cond": [
            {
                "$and": [
                    {"$in": ["$status", PRESENT_STATUSES]},
                    is_weekday_expr,
                ]
            },
            {
                "$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    {"$toDecimal": "0.5"},
                    {"$toDecimal": "1.0"},
                ]
            },
            {"$toDecimal": "0"},
        ]
    }

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days_dec": {"$sum": present_val_expr},
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": {"$literal": emp_code},
                "month": {"$literal": month},
                "working_days": {"$literal": working_days},
                "present_days": {"$toDouble": "$present_days_dec"},
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": (
                    {"$literal": None}
                    if working_days == 0
                    else mongo_round_half_up(
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_days_dec",
                                        {"$toDecimal": working_days},
                                    ]
                                },
                                100,
                            ]
                        },
                        2,
                    )
                ),
            }
        },
    ]


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> list[dict[str, Any]]:
    month_start, month_end = get_month_bounds(month)
    emp_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": month_start, "$lte": month_end}}}
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", PRESENT_STATUSES]},
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {
                                                "$eq": [
                                                    {"$ifNull": ["$$l.half_day", False]},
                                                    True,
                                                ]
                                            },
                                            {"$toDecimal": "0.5"},
                                            {"$toDecimal": "1.0"},
                                        ]
                                    },
                                    {"$toDecimal": "0"},
                                ]
                            },
                        }
                    }
                },
                "emp_wh_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", PRESENT_STATUSES]},
                                            {
                                                "$ne": [
                                                    {"$ifNull": ["$$l.work_hours", None]},
                                                    None,
                                                ]
                                            },
                                        ]
                                    },
                                    {"$toDecimal": "$$l.work_hours"},
                                    {"$toDecimal": "0"},
                                ]
                            },
                        }
                    }
                },
                "emp_wh_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", PRESENT_STATUSES]},
                                            {
                                                "$ne": [
                                                    {"$ifNull": ["$$l.work_hours", None]},
                                                    None,
                                                ]
                                            },
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$$l.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_mins": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days_dec": {"$sum": "$emp_present_days"},
                "wh_sum": {"$sum": "$emp_wh_sum"},
                "wh_count": {"$sum": "$emp_wh_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_late_mins"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$toDouble": "$present_days_dec"},
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$wh_count", 0]},
                        None,
                        mongo_round_half_up(
                            {"$divide": ["$wh_sum", {"$toDecimal": "$wh_count"}]}, 2
                        ),
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list[dict[str, Any]]:
    month_start, month_end = get_month_bounds(month)
    emp_lookup_pipeline: list[dict[str, Any]] = []
    if department is not None:
        emp_lookup_pipeline.append({"$match": {"department": department}})

    return [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
                "pipeline": emp_lookup_pipeline,
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def build_department_trend_pipeline(
    department: str, from_str: str, to_str: str
) -> list[dict[str, Any]]:
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    if to_d < from_d or (to_d - from_d).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Invalid date range: 'to' must be >= 'from' and range <= 92 days",
        )

    start_dt_utc = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    end_exclusive_d = to_d + timedelta(days=1)
    end_dt_exclusive_utc = datetime(
        end_exclusive_d.year,
        end_exclusive_d.month,
        end_exclusive_d.day,
        tzinfo=timezone.utc,
    )

    rate_dec_expr = {
        "$divide": [
            {
                "$floor": {
                    "$add": [
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_count_dec",
                                        {"$toDecimal": "$headcount"},
                                    ]
                                },
                                10000,
                            ]
                        },
                        {"$toDecimal": "0.5"},
                    ]
                }
            },
            {"$toDecimal": "10000"},
        ]
    }

    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {"date": {"$gte": from_str, "$lte": to_str}},
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": None,
                "joined_dates": {"$push": "$joined_on"},
                "logs_nested": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "day": {"$literal": start_dt_utc},
                "joined_dates": 1,
                "all_logs": {
                    "$reduce": {
                        "input": "$logs_nested",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt_utc, end_dt_exclusive_utc],
                },
            }
        },
        {
            "$fill": {
                "sortBy": {"day": 1},
                "output": {
                    "joined_dates": {"method": "locf"},
                    "all_logs": {"method": "locf"},
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$joined_dates",
                            "as": "jd",
                            "cond": {"$lte": ["$$jd", "$date"]},
                        }
                    }
                },
                "day_logs": {
                    "$filter": {
                        "input": "$all_logs",
                        "as": "l",
                        "cond": {"$eq": ["$$l.date", "$date"]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "present_count_dec": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "dl",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$dl.status", PRESENT_STATUSES]},
                                    {
                                        "$cond": [
                                            {"$eq": ["$$dl.half_day", True]},
                                            {"$toDecimal": "0.5"},
                                            {"$toDecimal": "1.0"},
                                        ]
                                    },
                                    {"$toDecimal": "0"},
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "dl",
                            "cond": {"$gt": ["$$dl.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate_dec": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        rate_dec_expr,
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate_dec",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$toDouble": "$present_count_dec"},
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$eq": ["$attendance_rate_dec", None]},
                        None,
                        {"$toDouble": "$attendance_rate_dec"},
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_raw", None]},
                        None,
                        mongo_round_half_up("$moving_avg_raw", 4),
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Part A Endpoints (Reviewed & Fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        ms_to_utc_dt(body.punched_at)
        if body.punched_at is not None
        else now_utc_truncated()
    )
    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts, emp["shift_start"], att_date, emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[StatusLiteral] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = build_attendance_list_query(emp_code, date_from, date_to, status)
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Part B Endpoints: Punch-Out & Regularization
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        ms_to_utc_dt(body.punched_at)
        if body.punched_at is not None
        else now_utc_truncated()
    )

    open_after = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$gt": ts}, "punch_out": None}
    )
    if open_after:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not doc:
        any_punch = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}}
        )
        if any_punch:
            raise HTTPException(
                status_code=422, detail="punched_at must be after punch_in"
            )
        raise HTTPException(status_code=404, detail="No punch-in record found")

    punch_in_dt = doc["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_seconds = (ts - punch_in_dt).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(
            status_code=422, detail="punched_at must be strictly after punch_in"
        )
    if diff_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    if doc.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime_mins = compute_overtime(
        ts, emp["shift_end"], doc["date"], emp["shift_start"]
    )
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(
            status_code=404, detail="Attendance record not found for this date"
        )

    cur_status = doc["status"]
    cur_punch_in = doc.get("punch_in")
    if cur_punch_in is not None and cur_punch_in.tzinfo is None:
        cur_punch_in = cur_punch_in.replace(tzinfo=timezone.utc)
    cur_punch_out = doc.get("punch_out")
    if cur_punch_out is not None and cur_punch_out.tzinfo is None:
        cur_punch_out = cur_punch_out.replace(tzinfo=timezone.utc)
    cur_work_hours = doc.get("work_hours")
    cur_late_minutes = doc.get("late_minutes", 0) or 0
    cur_overtime_minutes = doc.get("overtime_minutes", 0) or 0
    cur_half_day = bool(doc.get("half_day", False))

    new_status = body.status if body.status is not None else cur_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_punch_in = (
            ms_to_utc_dt(body.punch_in) if body.punch_in is not None else cur_punch_in
        )
        new_punch_out = (
            ms_to_utc_dt(body.punch_out)
            if body.punch_out is not None
            else cur_punch_out
        )

        if new_punch_in is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires punch_in"
            )

        expected_date = get_attendance_date(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if expected_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not fall on the record's attendance date",
            )

        new_late_minutes = compute_late_minutes(
            new_punch_in, emp["shift_start"], date, emp["shift_end"]
        )

        if new_punch_out is not None:
            diff_seconds = (new_punch_out - new_punch_in).total_seconds()
            if diff_seconds <= 0 or diff_seconds > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    field_pairs = [
        ("status", cur_status, new_status),
        ("punch_in", cur_punch_in, new_punch_in),
        ("punch_out", cur_punch_out, new_punch_out),
        ("work_hours", cur_work_hours, new_work_hours),
        ("late_minutes", cur_late_minutes, new_late_minutes),
        ("overtime_minutes", cur_overtime_minutes, new_overtime_minutes),
        ("half_day", cur_half_day, new_half_day),
    ]
    changes: dict[str, dict[str, Any]] = {}
    for fname, old_val, new_val in field_pairs:
        if old_val != new_val:
            changes[fname] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes no fields")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    concurrency_filter: dict[str, Any] = {
        "_id": doc["_id"],
        "status": doc.get("status"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
    }
    if "history" in doc:
        concurrency_filter["history"] = doc["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        concurrency_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409, detail="Concurrent modification conflict"
        )

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Part B Endpoints: Analytics Aggregation Pipelines
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month, working_days)
    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        return res[0]

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
        "attendance_pct": 0.0 if working_days > 0 else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: DateStr = Query(..., alias="from"),
    to_date: DateStr = Query(..., alias="to"),
):
    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C Endpoint: Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointLiteral,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[StatusLiteral] = None,
    from_date: Optional[DateStr] = Query(default=None, alias="from"),
    to_date: Optional[DateStr] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        raw = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "projection": {"_id": 0},
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": sanitize_bson(raw),
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )
        emp = db.employees.find_one({"emp_code": emp_code})
        working_days = (
            count_working_days(month, emp["joined_on"])
            if emp
            else count_working_days(month, "1970-01-01")
        )
        pipeline = build_employee_monthly_pipeline(emp_code, month, working_days)
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )
        pipeline = build_department_summary_pipeline(month, department)
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        collection_name = "employees"

    raw = db.command(
        "explain",
        {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
    )
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": sanitize_bson(raw),
    }