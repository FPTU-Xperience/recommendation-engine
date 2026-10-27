"""Small explicit validators for the trusted, but versioned, BE feed boundary."""

from datetime import datetime, timedelta, timezone
import math

PILLARS = ("Academic", "Research", "Global", "CultureSports", "Community", "Entrepreneurship")
INTERESTS = {"technology", "design", "business", "arts", "sports", "community", "languages", "leadership"}
CAMPUSES = {"HAN", "HCM", "DAN", "CAN", "QNH"}


class ValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message, code="INVALID_INPUT"):
    if not condition:
        raise ValidationError(code, message)


def object_value(value, name, allowed=None):
    require(isinstance(value, dict), f"{name} must be an object.")
    if allowed is not None:
        require(set(value) <= allowed, f"{name} has unsupported fields.")
    return value


def text(value, name, limit=200):
    require(isinstance(value, str) and 0 < len(value.strip()) <= limit
            and not any(ord(c) < 32 for c in value), f"{name} must be bounded nonempty text.")
    return value.strip()


def positive_int(value, name, maximum=2147483647):
    require(type(value) is int and 1 <= value <= maximum, f"{name} must be a positive bounded integer.")
    return value


def number(value, name, minimum, maximum):
    require(type(value) in (float, int) and minimum <= value <= maximum
            and math.isfinite(value), f"{name} is outside its numeric bounds.")
    return value


def boolean(value, name):
    require(type(value) is bool, f"{name} must be boolean.")
    return value


def array(value, name, maximum=10000):
    require(isinstance(value, list) and len(value) <= maximum, f"{name} must be a bounded array.")
    return value


def ids(value, name):
    result = [positive_int(x, name) for x in array(value, name)]
    require(len(set(result)) == len(result), f"{name} contains duplicate IDs.")
    return set(result)


def tags(value, name, canonical=False):
    values = [text(x, name, 100) for x in array(value, name, 30)]
    require(len(set(values)) == len(values), f"{name} contains duplicate values.")
    if canonical:
        require(set(values) <= INTERESTS, f"{name} contains an unknown canonical interest.")
    return set(values)


def timestamp(value, name):
    try:
        parsed = datetime.fromisoformat(text(value, name).replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), f"{name} must use UTC.")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError) as error:
        if isinstance(error, ValidationError):
            raise
        raise ValidationError("INVALID_INPUT", f"{name} must be an ISO UTC instant.") from error


def intervals(value, name, sessions=False):
    result = []
    seen = set()
    for row in array(value, name, 2000):
        object_value(row, name, {"start", "end", "id", "required"} if sessions else {"start", "end"})
        start, end = timestamp(row.get("start"), name), timestamp(row.get("end"), name)
        require(start < end, f"{name} interval must have start < end.")
        if sessions:
            identity = positive_int(row.get("id"), "session.id")
            require(identity not in seen, "Duplicate session ID.")
            seen.add(identity)
            boolean(row.get("required"), "session.required")
        result.append({**row, "_start": start, "_end": end})
    return result


def availability(value):
    require(isinstance(value, str) and value in ("KNOWN", "UNKNOWN"), "availability must be KNOWN or UNKNOWN.")
    return value


def source_metadata(metadata, as_of, max_age, name):
    object_value(metadata, name, {"version", "asOf", "policyVersion", "ledgerRevision"})
    observed = timestamp(metadata.get("asOf"), name + ".asOf")
    require(observed <= as_of, "Source observation is after snapshot asOf.", "FUTURE_SOURCE")
    require(as_of - observed <= timedelta(hours=max_age), "Source observation is too old.", "STALE_SOURCE")
    return metadata


def candidate(row, kind):
    common = {"id", "version", "campusCode", "interestIds", "majorCodes", "pillarWeights", "scheduleAvailability"}
    allowed = common | ({"active", "recruiting", "meetingIntervals"} if kind == "club" else
                        {"clubId", "published", "cancelled", "registrationOpen", "registrationDeadline",
                         "capacityAvailable", "eligibleUserIds", "sessions"})
    object_value(row, kind, allowed)
    positive_int(row.get("id"), kind + ".id")
    text(row.get("version"), kind + ".version")
    require(isinstance(row.get("campusCode"), str) and row["campusCode"] in CAMPUSES, "Unknown candidate campus.")
    tags(row.get("interestIds"), "interestIds", True)
    tags(row.get("majorCodes"), "majorCodes")
    vector = object_value(row.get("pillarWeights"), "pillarWeights", set(PILLARS))
    require(bool(vector), "A candidate requires a pillar vector.")
    for v in vector.values():
        number(v, "pillarWeights", 0, 1)
    require(math.isclose(sum(vector.values()), 1, abs_tol=1e-6), "Pillar weights must sum to one.")
    availability(row.get("scheduleAvailability"))
    result = dict(row)
    total = math.fsum(vector.values())
    result["pillarWeights"] = {p: weight / total for p, weight in vector.items()}
    if kind == "club":
        boolean(row.get("active"), "active")
        boolean(row.get("recruiting"), "recruiting")
        result["_intervals"] = intervals(row.get("meetingIntervals"), "meetingIntervals")
    else:
        positive_int(row.get("clubId"), "activity.clubId")
        for flag in ("published", "cancelled", "registrationOpen", "capacityAvailable"):
            boolean(row.get(flag), flag)
        result["_eligible"] = ids(row.get("eligibleUserIds"), "eligibleUserIds")
        result["_deadline"] = timestamp(row.get("registrationDeadline"), "registrationDeadline")
        result["_intervals"] = intervals(row.get("sessions"), "sessions", True)
    return result
