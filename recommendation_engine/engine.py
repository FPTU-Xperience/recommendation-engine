"""Pure deterministic filtering/ranking over a validated backend snapshot."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import math

from .config import Config
from .validation import (CAMPUSES, PILLARS, ValidationError, array, availability,
                         boolean, candidate, ids, intervals, number, object_value,
                         positive_int, require, source_metadata, tags, text, timestamp)

RULE_VERSION = "SIMPLE_CONTENT_V2"


def overlaps(left, right):
    return max(left["_start"], right["_start"]) < min(left["_end"], right["_end"])


def _config(config):
    if isinstance(config, Config):
        value = {"configVersion": config.version, "clubLimit": config.club_limit,
                 "activityLimit": config.activity_limit, "weights": config.weights,
                 "weakestBoost": config.weakest_boost, "strongestBoost": config.strongest_boost,
                 "otherBoost": config.other_boost, "tieTolerance": config.tie_tolerance,
                 "walletAffinityCap": config.wallet_cap, "maxSourceAgeHours": config.max_source_age_hours}
        return Config.from_dict(value)
    return Config.from_dict(config)


def _student(raw, snapshot, config):
    object_value(raw, "student", {"userId", "campusCode", "preferences", "schedule", "experience",
                                  "memberships", "activityRegistrations", "walletSignals", "sourceVersions"})
    positive_int(raw.get("userId"), "userId")
    require(isinstance(raw.get("campusCode"), str) and raw["campusCode"] in CAMPUSES, "Unknown student campus.")
    versions = object_value(raw.get("sourceVersions"), "sourceVersions",
                            {"account", "catalog", "preferences", "schedule", "experience", "wallet"})
    as_of = timestamp(snapshot["asOf"], "asOf")
    for key, metadata in versions.items():
        source_metadata(metadata, as_of, config.max_source_age_hours, key)
        if key == "experience":
            text(metadata.get("policyVersion"), "source policyVersion")
            revision = metadata.get("ledgerRevision")
            require(type(revision) is int and 0 <= revision <= 9007199254740991, "Invalid source ledger revision.")
        else:
            text(metadata.get("version"), key + ".version")
    for key in ("account", "catalog"):
        source_metadata(versions.get(key), as_of, config.max_source_age_hours, key)
        text(versions[key].get("version"), key + ".version")
    result = {**raw, "_warnings": [], "_interests": set(), "_major": None,
              "_busy": [], "_schedule_known": False, "_targets": None, "_weak": set(),
              "_strong": set(), "_wallets": {}}
    result["_memberships"] = ids(raw.get("memberships"), "memberships")
    result["_registrations"] = ids(raw.get("activityRegistrations"), "activityRegistrations")

    preferences = raw.get("preferences")
    if preferences is None:
        result["_warnings"].append("PREFERENCES_UNAVAILABLE")
    else:
        object_value(preferences, "preferences", {"version", "interestIds", "customInterests", "declaredMajorCode"})
        meta = source_metadata(versions.get("preferences"), as_of, config.max_source_age_hours, "preferences")
        require(text(preferences.get("version"), "preferences.version") == meta.get("version"),
                "Preference version does not match source.", "SOURCE_VERSION_MISMATCH")
        result["_interests"] = tags(preferences.get("interestIds"), "interestIds", True)
        tags(preferences.get("customInterests"), "customInterests")
        major = preferences.get("declaredMajorCode")
        result["_major"] = text(major, "declaredMajorCode", 100) if major is not None else None

    schedule = raw.get("schedule")
    if schedule is not None:
        object_value(schedule, "schedule", {"version", "availability", "busyIntervals"})
        known = availability(schedule.get("availability")) == "KNOWN"
        result["_busy"] = intervals(schedule.get("busyIntervals"), "busyIntervals")
        meta = source_metadata(versions.get("schedule"), as_of, config.max_source_age_hours, "schedule")
        require(text(schedule.get("version"), "schedule.version") == meta.get("version"),
                "Schedule version does not match source.", "SOURCE_VERSION_MISMATCH")
        result["_schedule_known"] = known
    if not result["_schedule_known"]:
        result["_warnings"].append("SCHEDULE_UNKNOWN")

    experience = raw.get("experience")
    if experience is not None:
        object_value(experience, "experience", {"semesterId", "policyVersion", "ledgerRevision", "isAuthoritative", "pillars"})
        authoritative = boolean(experience.get("isAuthoritative"), "isAuthoritative")
        meta = source_metadata(versions.get("experience"), as_of, config.max_source_age_hours, "experience")
        revision = experience.get("ledgerRevision")
        require(type(revision) is int and 0 <= revision <= 9007199254740991, "Invalid ledger revision.")
        require(meta.get("ledgerRevision") == revision and meta.get("policyVersion") == experience.get("policyVersion"),
                "Experience metadata does not match source.", "SOURCE_VERSION_MISMATCH")
        compatible = (authoritative and experience.get("semesterId") == snapshot["semesterId"]
                      and experience.get("policyVersion") == snapshot["policyVersion"])
        if compatible:
            axes = array(experience.get("pillars"), "pillars", 6)
            values = {}
            for axis in axes:
                object_value(axis, "axis", {"pillar", "score"})
                pillar = axis.get("pillar")
                require(isinstance(pillar, str) and pillar in PILLARS and pillar not in values, "Invalid or repeated radar pillar.")
                values[pillar] = number(axis.get("score"), "score", 0, 100)
            require(set(values) == set(PILLARS), "Radar must contain exactly six pillars.")
            low, high = min(values.values()), max(values.values())
            weakest = {p for p in PILLARS if values[p] - low <= config.tie_tolerance}
            strongest = {p for p in PILLARS if high - values[p] <= config.tie_tolerance}
            if weakest & strongest or high - low <= config.tie_tolerance:
                result["_targets"] = dict.fromkeys(PILLARS, 1.0)
            else:
                boosts = {p: config.weakest_boost if p in weakest else
                          config.strongest_boost if p in strongest else config.other_boost for p in PILLARS}
                result["_targets"] = {p: boost / max(boosts.values()) for p, boost in boosts.items()}
                result["_weak"], result["_strong"] = weakest, strongest
    if result["_targets"] is None:
        result["_warnings"].append("EXPERIENCE_UNAVAILABLE")

    wallets = object_value(raw.get("walletSignals", {}), "walletSignals")
    require(len(wallets) <= 10000, "Too many wallet signals.")
    if wallets:
        meta = source_metadata(versions.get("wallet"), as_of, config.max_source_age_hours, "wallet")
        text(meta.get("version"), "wallet source version")
    for club, wallet in wallets.items():
        require(isinstance(club, str) and club.isascii() and club.isdecimal() and str(int(club)) == club,
                "Wallet club ID must be canonical decimal text.")
        positive_int(int(club), "wallet clubId")
        object_value(wallet, "wallet", {"availability", "earnedTotal", "revokedTotal", "version"})
        if availability(wallet.get("availability")) == "KNOWN":
            text(wallet.get("version"), "wallet.version")
            earned = wallet.get("earnedTotal")
            revoked = wallet.get("revokedTotal")
            require(type(earned) is int and type(revoked) is int
                    and 0 <= revoked <= earned <= 9007199254740991, "Invalid credit counters.")
            result["_wallets"][int(club)] = min(earned - revoked, config.wallet_cap) / config.wallet_cap
    if not result["_wallets"]:
        result["_warnings"].append("WALLET_SIGNAL_UNAVAILABLE")
    return result


def _schedule(student, row, kind, as_of):
    known = student["_schedule_known"] and row["scheduleAvailability"] == "KNOWN"
    busy = student["_busy"]
    if kind == "club":
        meetings = [x for x in row["_intervals"] if x["_start"] > as_of]
        if not known or not meetings:
            return True, None, [], None
        free = sum(not any(overlaps(x, b) for b in busy) for x in meetings)
        return True, free / len(meetings), [], None
    sessions = row["_intervals"]
    required = [x for x in sessions if x["required"]]
    if any(x["_start"] <= as_of for x in required):
        return False, None, [], None
    future = [x for x in sessions if x["_start"] > as_of]
    if not future:
        return False, None, [], None
    if not known:
        return True, None, [], min(x["_start"] for x in future)
    attendable = [x for x in future if not any(overlaps(x, b) for b in busy)]
    if any(x not in attendable for x in required) or (not required and not attendable):
        return False, None, [], None
    options = sorted((x for x in attendable if not x["required"]), key=lambda x: (x["_start"], x["id"]))
    return True, 1.0, [x["id"] for x in options], min(x["_start"] for x in (required or attendable))


def _score(student, row, kind, config, as_of):
    internal = kind == "activity" and row["isInternal"]
    if internal and row["clubId"] not in student["_memberships"]:
        return None
    if not internal and row["campusCode"] != student["campusCode"]:
        return None
    if kind == "club":
        if not row["active"] or not row["recruiting"] or row["id"] in student["_memberships"]:
            return None
        club_id = row["id"]
    else:
        if (not row["published"] or row["cancelled"] or not row["registrationOpen"]
                or not row["capacityAvailable"] or row["_deadline"] <= as_of
                or student["userId"] not in row["_eligible"] or row["id"] in student["_registrations"]):
            return None
        club_id = row["clubId"]
    allowed, schedule_fit, sessions, start = _schedule(student, row, kind, as_of)
    if not allowed:
        return None
    interests = set(row["interestIds"])
    own_interests = student["_interests"]
    major = student["_major"]
    targets = student["_targets"]
    values = {
        "interest": len(interests & own_interests) / len(interests | own_interests) if interests and own_interests else None,
        "major": float(major in row["majorCodes"]) if major and row["majorCodes"] else None,
        "pillar": math.fsum(weight * targets[p] for p, weight in row["pillarWeights"].items()) if targets else None,
        "schedule": schedule_fit,
        "walletAffinity": student["_wallets"].get(club_id),
    }
    available = math.fsum(config.weights[name] for name, value in values.items() if value is not None)
    score = math.fsum(config.weights[name] * value for name, value in values.items() if value is not None) / available if available else None
    # Only suppress tiny floating-point excursions; never round values used for ordering.
    score = min(1.0, max(0.0, score)) if score is not None else None
    dominant = min(row["pillarWeights"], key=lambda p: (-row["pillarWeights"][p], PILLARS.index(p)))
    reasons = []
    if values["interest"]:
        reasons.append({"code": "INTEREST_MATCH", "interestIds": sorted(interests & own_interests)})
    if values["major"]:
        reasons.append({"code": "MAJOR_MATCH", "majorCode": major})
    if targets:
        direction = "GROW_WEAKEST_PILLAR" if dominant in student["_weak"] else (
            "DEVELOP_STRONGEST_PILLAR" if dominant in student["_strong"] else "BALANCED_EXPLORATION")
        reasons.append({"code": direction, "pillar": dominant})
    reasons.append({"code": "SCHEDULE_UNKNOWN" if schedule_fit is None else (
        "NO_SCHEDULE_CONFLICT" if schedule_fit == 1 else "PARTIAL_SCHEDULE_CONFLICT")})
    if values["walletAffinity"]:
        reasons.append({"code": "CLUB_INVOLVEMENT"})
    if score is None:
        reasons.append({"code": "INSUFFICIENT_PROFILE"})
    return {"id": row["id"], "score": score, "availableWeight": available,
            "candidateVersion": row["version"], "targetPillar": dominant,
            "features": {name: {"value": value, "available": value is not None} for name, value in values.items()},
            "reasons": reasons,
            "scheduleStatus": "UNKNOWN" if schedule_fit is None else (
                "KNOWN_NO_CONFLICT" if schedule_fit == 1 else "KNOWN_PARTIAL_CONFLICT"),
            "suggestedSessionIds": sessions, "_club": club_id, "_start": start}


def _rank(student, candidates, kind, config, as_of):
    items = [item for row in candidates if (item := _score(student, row, kind, config, as_of)) is not None]
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    items.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), -x["availableWeight"],
                              x["_start"] or far_future, x["id"]))
    limit = config.club_limit if kind == "club" else config.activity_limit
    pillars, clubs, selected = Counter(), Counter(), []
    for item in items:
        if pillars[item["targetPillar"]] >= math.ceil(limit / 2) or (kind == "activity" and clubs[item["_club"]] >= 2):
            continue
        pillars[item["targetPillar"]] += 1
        clubs[item["_club"]] += 1
        selected.append({k: v for k, v in item.items() if not k.startswith("_")})
        selected[-1]["rank"] = len(selected)
        if len(selected) == limit:
            break
    return selected


def recommend(snapshot, config=None, *, evaluated_at=None):
    """Rank a snapshot without mutation or I/O.

    evaluated_at is optional for deterministic offline evaluation; the CLI always
    supplies its current UTC time unless an explicit evaluation time is requested.
    """
    config = _config(config)
    object_value(snapshot, "snapshot", {"schemaVersion", "snapshotId", "asOf", "expiresAt", "semesterId",
                                        "policyVersion", "students", "clubs", "activities"})
    require(snapshot.get("schemaVersion") == "1.0", "Unsupported schemaVersion.", "SCHEMA_UNSUPPORTED")
    for field in ("snapshotId", "semesterId", "policyVersion"):
        text(snapshot.get(field), field)
    as_of, expires = timestamp(snapshot.get("asOf"), "asOf"), timestamp(snapshot.get("expiresAt"), "expiresAt")
    require(expires > as_of, "expiresAt must be after asOf.")
    if evaluated_at is not None:
        now = timestamp(evaluated_at, "evaluation time")
        require(as_of <= now < expires, "Snapshot is expired or from the future.", "SNAPSHOT_EXPIRED")
    students = array(snapshot.get("students"), "students", 10000)
    user_ids = [positive_int(object_value(s, "student").get("userId"), "userId") for s in students]
    require(len(set(user_ids)) == len(user_ids), "Duplicate student IDs.", "DUPLICATE_ID")
    # Future observations invalidate the entire batch, including optional sources.
    for raw in students:
        metadata = raw.get("sourceVersions")
        if not isinstance(metadata, dict):
            continue  # Student validation below isolates a malformed per-user feed.
        for meta in metadata.values():
            if isinstance(meta, dict) and "asOf" in meta:
                try:
                    observed = timestamp(meta["asOf"], "source.asOf")
                except ValidationError:
                    continue  # Invalid metadata is reported for the owning student.
                require(observed <= as_of, "Source observation is after snapshot asOf.", "FUTURE_SOURCE")
    catalogs = {}
    for plural, kind, bound in (("clubs", "club", 10000), ("activities", "activity", 50000)):
        rows = [candidate(row, kind) for row in array(snapshot.get(plural), plural, bound)]
        require(len({r["id"] for r in rows}) == len(rows), "Duplicate candidate IDs.", "DUPLICATE_ID")
        catalogs[plural] = rows
    results = []
    for raw in sorted(students, key=lambda x: x["userId"]):
        entry = {"userId": raw["userId"], "semesterId": snapshot["semesterId"], "clubs": [], "activities": []}
        try:
            student = _student(raw, snapshot, config)
            entry["clubs"] = _rank(student, catalogs["clubs"], "club", config, as_of)
            entry["activities"] = _rank(student, catalogs["activities"], "activity", config, as_of)
            warnings = list(student["_warnings"])
            for item in entry["clubs"] + entry["activities"]:
                if item["scheduleStatus"] == "UNKNOWN":
                    warnings.append("SCHEDULE_UNKNOWN")
                if any(not feature["available"] for feature in item["features"].values()):
                    warnings.append("PARTIAL_FEATURES")
            entry.update(status="PARTIAL" if warnings else "READY", warnings=sorted(set(warnings)),
                         sourceVersions=deepcopy(raw["sourceVersions"]))
        except ValidationError as error:
            entry.update(status="UNAVAILABLE", warnings=[error.code])
        results.append(entry)
    return {"schemaVersion": "1.0", "snapshotId": snapshot["snapshotId"], "ruleVersion": RULE_VERSION,
            "configVersion": config.version, "generatedFromAsOf": snapshot["asOf"],
            "expiresAt": snapshot["expiresAt"], "results": results}
