"""Private synthetic feed -> worker -> Mongo -> HTTP read deployment check."""
from datetime import datetime, timedelta, timezone
import json
import os
import time
from urllib.request import Request, urlopen
from uuid import uuid4
from pymongo import MongoClient


def main():
    now = datetime.now(timezone.utc)
    at = now.isoformat()
    user, semester = 2147483647, "SMOKE-" + uuid4().hex
    meta = {"version": "deployment-smoke", "asOf": at}
    snapshot = {
        "schemaVersion": "1.0", "snapshotId": semester, "asOf": at,
        "expiresAt": (now + timedelta(minutes=10)).isoformat(), "semesterId": semester,
        "policyVersion": "deployment-smoke", "clubs": [],
        "students": [{"userId": user, "campusCode": "HAN", "preferences": None,
            "schedule": None, "experience": None, "memberships": [], "activityRegistrations": [],
            "walletSignals": {}, "sourceVersions": {"account": meta, "catalog": meta}}],
        "activities": [{"id": 1, "version": "deployment-smoke", "clubId": 1,
            "campusCode": "HAN", "published": True, "cancelled": False, "registrationOpen": True,
            "registrationDeadline": (now + timedelta(minutes=8)).isoformat(),
            "capacityAvailable": True, "eligibleUserIds": [user], "interestIds": [],
            "majorCodes": [], "pillarWeights": {"Research": 1}, "scheduleAvailability": "KNOWN",
            "sessions": [{"id": 1, "required": True, "start": (now + timedelta(hours=1)).isoformat(),
                          "end": (now + timedelta(hours=2)).isoformat()}]}]
    }
    def call(path, body=None):
        request = Request("http://127.0.0.1:8080" + path,
            data=json.dumps(body).encode() if body else None,
            headers={"X-Recommendation-Token": os.environ["RECOMMENDATION_TOKEN"], "Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    mongo = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
    collection = mongo[os.environ.get("MONGODB_DATABASE", "recommendation")].recommendations
    try:
        accepted = call("/internal/feeds", {"readStartedAt": at, "snapshot": snapshot})
        assert accepted["status"] == "QUEUED", "Synthetic feed was not accepted"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = call(f"/internal/recommendations/users/{user}?semesterId={semester}&type=activities")
            if result["available"]:
                assert result["snapshotId"] == semester and result["items"][0]["id"] == 1
                assert collection.count_documents({"userId": user, "semesterId": semester, "dirty": False}) == 1
                print(json.dumps({"status": "PASSED", "checks": ["feed", "async-worker", "mongo-publication", "result-api"]}))
                return
            time.sleep(0.5)
        raise RuntimeError("Synthetic recommendation processing timed out")
    finally:
        collection.delete_one({"userId": user, "semesterId": semester})
        mongo.close()


if __name__ == "__main__":
    main()
