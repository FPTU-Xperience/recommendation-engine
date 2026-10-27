"""Private HTTP transport and durable MongoDB work queue for the pure ranker.

One Mongo document owns a user's feed, generation and published result. Publication
is a single compare-and-set, so an expired lease or older feed cannot win a race.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .cli import _constant, _pairs
from .engine import recommend
from .validation import ValidationError, positive_int, text, timestamp

LOG = logging.getLogger("recommendation")
MAX_FEED_BYTES = 8 * 1024 * 1024  # Mongo's document limit includes result + feed.


def utcnow():
    return datetime.now(timezone.utc)


def micros(instant):
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def fingerprint(snapshot):
    value = deepcopy(snapshot)
    for name in ("snapshotId", "asOf", "expiresAt"):
        value.pop(name, None)
    for student in value["students"]:
        for source in student["sourceVersions"].values():
            source.pop("asOf", None)
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def feed_key(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != "1.0":
        raise ValidationError("INVALID_FEED", "Unsupported feed.")
    students = snapshot.get("students")
    if not isinstance(students, list) or len(students) != 1:
        raise ValidationError("INVALID_FEED", "Submit one user per durable job.")
    user = positive_int(students[0].get("userId"), "userId")
    semester = text(snapshot.get("semesterId"), "semesterId", 40)
    text(snapshot.get("snapshotId"), "snapshotId")
    now = utcnow()
    if not timestamp(snapshot["asOf"], "asOf") <= now < timestamp(snapshot["expiresAt"], "expiresAt"):
        raise ValidationError("SNAPSHOT_EXPIRED", "Feed is outside its validity interval.")
    if timestamp(snapshot["expiresAt"], "expiresAt") > now + timedelta(hours=24):
        raise ValidationError("INVALID_FEED", "Feed validity must not exceed 24 hours.")
    return user, semester


class MongoStore:
    def __init__(self, uri, database):
        from pymongo import MongoClient
        self.client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000,
                                  connectTimeoutMS=5000, socketTimeoutMS=10000)
        self.jobs = self.client[database].recommendations
        self.jobs.create_index([("userId", 1), ("semesterId", 1)], unique=True)
        self.jobs.create_index([("dirty", 1), ("needsFeed", 1), ("leaseUntil", 1), ("dueAt", 1)])
        # Expiry of a ranking is checked on reads; TTL only removes inactive accounts.
        self.jobs.create_index("deleteAfter", expireAfterSeconds=0)

    def health(self):
        self.client.admin.command("ping")

    def submit(self, snapshot, read_started_at=None):
        from pymongo.errors import DuplicateKeyError
        user, semester = feed_key(snapshot)
        identity = {"userId": user, "semesterId": semester}
        digest, now = fingerprint(snapshot), utcnow()
        observed = timestamp(snapshot["asOf"], "asOf")
        started = timestamp(read_started_at, "readStartedAt") if read_started_at else observed
        if started > observed:
            raise ValidationError("INVALID_FEED", "Read start must precede snapshot publication.")
        for _ in range(10):
            old = self.jobs.find_one(identity)
            if old and old.get("invalidatedAtMicros", micros(started)) > micros(started):
                return "RETRY_SOURCE_CHANGED"
            # BSON dates truncate to milliseconds; preserve ordering separately.
            if old and micros(observed) < old.get("observedAtMicros", micros(timestamp(old["feed"]["asOf"], "asOf"))):
                return "IGNORED_OLDER_FEED"
            refresh = not old or old.get("fingerprint") != digest or old.get("needsFeed", False)
            refresh = refresh or bool(old and old.get("result") and old.get("resultExpiresAt", now) <= now + timedelta(minutes=5))
            if not refresh:
                self.jobs.update_one({**identity, "generation": old["generation"]}, {"$set": {
                    "deleteAfter": now + timedelta(days=30), "lastFeedAt": now}})
                return "UNCHANGED"
            fields = {"feed": snapshot, "fingerprint": digest, "observedAt": observed, "observedAtMicros": micros(observed),
                      "lastFeedAt": now, "deleteAfter": now + timedelta(days=30),
                      "dirty": True, "needsFeed": False, "dueAt": now + timedelta(seconds=2)}
            if old:
                # A concurrent invalidation also advances generation and cannot be lost.
                changed = self.jobs.update_one({**identity, "generation": old["generation"]},
                                              {"$set": fields, "$inc": {"generation": 1},
                                               "$unset": {"leaseUntil": "", "leaseToken": ""}})
                if changed.modified_count:
                    return "QUEUED"
            else:
                try:
                    self.jobs.insert_one({**identity, **fields, "generation": 1})
                    return "QUEUED"
                except DuplicateKeyError:
                    continue
        raise RuntimeError("Concurrent feed submission; retry.")

    def invalidate(self, user_ids=None):
        query = {} if user_ids is None else {"userId": {"$in": user_ids}}
        self.jobs.update_many(query, {"$set": {"dirty": True, "needsFeed": True, "invalidatedAtMicros": micros(utcnow())},
                                      "$inc": {"generation": 1}})

    def claim(self):
        from pymongo import ReturnDocument
        now = utcnow()
        return self.jobs.find_one_and_update({"dirty": True, "needsFeed": False,
            "dueAt": {"$lte": now}, "$or": [{"leaseUntil": {"$exists": False}}, {"leaseUntil": {"$lte": now}}]},
            {"$set": {"leaseToken": uuid4().hex, "leaseUntil": now + timedelta(minutes=5)}},
            return_document=ReturnDocument.AFTER, sort=[("dueAt", 1)])

    def publish(self, job, batch):
        now = utcnow()
        result = batch["results"][0]
        expiry = timestamp(batch["expiresAt"], "expiresAt")
        if expiry <= now:
            self.fail(job, "SNAPSHOT_EXPIRED")
            return False
        # Time-based eligibility changes without an event; expire at the next boundary.
        boundaries = [expiry]
        selected_ids = {item["id"] for item in result["activities"]}
        for activity in job["feed"]["activities"]:
            if activity["id"] in selected_ids and (timestamp(activity["registrationDeadline"], "deadline") <= now
                    or any(timestamp(s["start"], "start") <= now for s in activity["sessions"])):
                self.fail(job, "SOURCE_TIME_BOUNDARY")
                return False
            boundaries.append(timestamp(activity["registrationDeadline"], "registrationDeadline"))
            boundaries.extend(timestamp(s["start"], "start") for s in activity["sessions"])
        expiry = min(t for t in boundaries if t > now) if any(t > now for t in boundaries) else now
        envelope = {k: v for k, v in batch.items() if k != "results"}
        envelope.update(result)
        envelope.update(generatedAt=now.isoformat(), expiresAt=expiry.isoformat(), generation=job["generation"])
        return self.jobs.update_one({"_id": job["_id"], "generation": job["generation"],
            "leaseToken": job["leaseToken"], "leaseUntil": {"$gt": now}, "needsFeed": False},
            {"$set": {"result": envelope, "resultExpiresAt": expiry, "dirty": False},
             "$unset": {"leaseUntil": "", "leaseToken": "", "lastError": ""}}).modified_count == 1

    def fail(self, job, code):
        self.jobs.update_one({"_id": job["_id"], "generation": job["generation"], "leaseToken": job["leaseToken"]},
            {"$set": {"needsFeed": True, "lastError": code}, "$unset": {"leaseUntil": "", "leaseToken": ""}})

    def read(self, user, semester, kind):
        job = self.jobs.find_one({"userId": user, "semesterId": semester})
        if not job:
            return {"status": "MISSING", "items": [], "available": False}
        result = job.get("result")
        stale = job.get("dirty", True) or not result or job.get("resultExpiresAt", utcnow()) <= utcnow()
        if stale:
            # Never rank on a GET; next exporter sweep supplies a fresh source snapshot.
            if not job.get("dirty"):
                self.jobs.update_one({"_id": job["_id"], "generation": job["generation"]},
                    {"$set": {"dirty": True, "needsFeed": True}, "$inc": {"generation": 1}})
            return {"status": "PENDING" if not result else "STALE", "items": [], "available": False}
        response = {k: v for k, v in result.items() if k not in ("clubs", "activities")}
        response.update(items=result[kind], available=result["status"] != "UNAVAILABLE", type=kind)
        return response


class Runtime:
    def __init__(self, store, token):
        if len(token) < 32:
            raise ValueError("RECOMMENDATION_TOKEN must contain at least 32 characters.")
        self.store, self.token = store, token
        self.stop = threading.Event()
        self.last_worker_tick = utcnow()

    def worker(self):
        while not self.stop.is_set():
            job = None
            try:
                job = self.store.claim()
                self.last_worker_tick = utcnow()
                if job:
                    batch = recommend(job["feed"], evaluated_at=utcnow().isoformat())
                    self.store.publish(job, batch)
                else:
                    self.stop.wait(1)
            except Exception as error:
                code = error.code if isinstance(error, ValidationError) else "WORKER_FAILURE"
                LOG.warning("worker_failure code=%s", code)
                if job:
                    try:
                        self.store.fail(job, code)
                    except Exception:
                        LOG.warning("worker_failure code=FAILURE_RECORD_UNAVAILABLE")
                self.stop.wait(3)


def handler(runtime):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass  # URLs and user identifiers are private.

        def respond(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def dispatch(self, method):
            path = urlsplit(self.path)
            try:
                if path.path == "/health/ready" and method == "GET":
                    runtime.store.health()
                    alive = (utcnow() - runtime.last_worker_tick).total_seconds() < 360
                    return self.respond(200 if alive else 503, {"status": "READY" if alive else "WORKER_STALLED"})
                supplied = self.headers.get("X-Recommendation-Token", "")
                if not hmac.compare_digest(supplied.encode(), runtime.token.encode()):
                    return self.respond(401, {"code": "UNAUTHORIZED"})
                if method == "POST":
                    if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        return self.respond(415, {"code": "JSON_REQUIRED"})
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_FEED_BYTES:
                        return self.respond(413, {"code": "FEED_TOO_LARGE"})
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete body")
                    body = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
                    if path.path == "/internal/feeds":
                        if not isinstance(body, dict) or set(body) != {"snapshot", "readStartedAt"}:
                            raise ValueError("Feed envelope required")
                        return self.respond(202, {"status": runtime.store.submit(body["snapshot"], body["readStartedAt"])})
                    if path.path == "/internal/invalidations":
                        if not isinstance(body, dict) or set(body) != {"userIds"}:
                            raise ValueError("Invalid invalidation")
                        ids = body["userIds"]
                        if ids is not None:
                            if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
                                raise ValueError("Invalid users")
                            ids = [positive_int(x, "userId") for x in ids]
                        runtime.store.invalidate(ids)
                        return self.respond(202, {"status": "INVALIDATED"})
                parts = path.path.strip("/").split("/")
                if method == "GET" and len(parts) == 4 and parts[:3] == ["internal", "recommendations", "users"]:
                    user = positive_int(int(parts[3]), "userId")
                    query = parse_qs(path.query)
                    semester = text(query.get("semesterId", [None])[0], "semesterId", 40)
                    kind = query.get("type", ["activities"])[0]
                    if kind not in ("clubs", "activities"):
                        raise ValueError("Invalid type")
                    return self.respond(200, runtime.store.read(user, semester, kind))
                self.respond(404, {"code": "NOT_FOUND"})
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
                self.respond(400, {"code": "INVALID_REQUEST"})
            except Exception:
                LOG.warning("request_failure code=DEPENDENCY_UNAVAILABLE")
                self.respond(503, {"code": "DEPENDENCY_UNAVAILABLE"})
    return Handler


def main():
    logging.basicConfig(level=logging.INFO)
    runtime = Runtime(MongoStore(os.environ["MONGODB_URI"], os.environ.get("MONGODB_DATABASE", "recommendation")),
                      os.environ["RECOMMENDATION_TOKEN"])
    server = ThreadingHTTPServer((os.environ.get("BIND_HOST", "0.0.0.0"), int(os.environ.get("PORT", "8080"))), handler(runtime))
    # Admission is bounded by the kernel backlog + Docker memory/pid limits.
    server.daemon_threads = True
    thread = threading.Thread(target=runtime.worker, daemon=True)
    thread.start()
    def shutdown(*_):
        runtime.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        runtime.stop.set()
        server.server_close()
        thread.join(timeout=10)
        runtime.store.client.close()


if __name__ == "__main__":
    main()
