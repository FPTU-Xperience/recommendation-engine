"""Synthetic acceptance tests; no live account, storage or API is accessed."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from recommendation_engine import Config, ValidationError, recommend
from recommendation_engine.validation import PILLARS

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    return json.loads((ROOT / "docs/contracts/examples/input-v1.json").read_text(encoding="utf-8"))


def result(data):
    return recommend(data)["results"][0]


def clone_activity(data, identity, pillar="Research", club=7):
    activity = deepcopy(data["activities"][0])
    activity.update(id=identity, clubId=club, pillarWeights={pillar: 1})
    return activity


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()
        self.student = self.data["students"][0]

    def test_example_is_scored_without_mutation_and_deterministic(self):
        before = deepcopy(self.data)
        first = recommend(self.data)
        self.assertEqual(self.data, before)
        self.assertEqual(first, recommend(self.data))
        row = first["results"][0]["activities"][0]
        self.assertAlmostEqual(row["score"], 1)
        self.assertAlmostEqual(row["availableWeight"], .95)
        self.assertEqual(row["targetPillar"], "Research")

    def test_published_example_output_matches_actual_engine(self):
        expected = json.loads((ROOT / "docs/contracts/examples/output-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(recommend(self.data), expected)

    def test_published_default_configuration_is_loadable(self):
        config = json.loads((ROOT / "config/default.json").read_text(encoding="utf-8"))
        self.assertEqual(recommend(self.data, config), recommend(self.data))

    def test_unknown_schedule_is_not_free(self):
        self.student["schedule"]["availability"] = "UNKNOWN"
        row = result(self.data)["activities"][0]
        self.assertIsNone(row["features"]["schedule"]["value"])
        self.assertEqual(row["scheduleStatus"], "UNKNOWN")
        self.assertIn({"code": "SCHEDULE_UNKNOWN"}, row["reasons"])
        self.assertNotIn({"code": "NO_SCHEDULE_CONFLICT"}, row["reasons"])

    def test_known_empty_schedule_is_free(self):
        self.student["schedule"]["busyIntervals"] = []
        self.assertEqual(result(self.data)["activities"][0]["features"]["schedule"]["value"], 1)

    def test_required_overlap_excludes_activity(self):
        self.student["schedule"]["busyIntervals"] = [{"start": "2026-10-27T08:30:00Z", "end": "2026-10-27T09:30:00Z"}]
        self.assertEqual(result(self.data)["activities"], [])
        club = result(self.data)["clubs"][0]
        self.assertEqual(club["features"]["schedule"]["value"], 0)
        self.assertEqual(club["scheduleStatus"], "KNOWN_PARTIAL_CONFLICT")

    def test_back_to_back_sessions_are_allowed(self):
        self.student["schedule"]["busyIntervals"] = [{"start": "2026-10-27T07:00:00Z", "end": "2026-10-27T08:00:00Z"}]
        self.assertEqual(len(result(self.data)["activities"]), 1)

    def test_optional_only_activity_requires_an_attendable_session(self):
        session = self.data["activities"][0]["sessions"][0]
        session["required"] = False
        self.assertEqual(result(self.data)["activities"][0]["suggestedSessionIds"], [901])
        self.student["schedule"]["busyIntervals"] = [{"start": session["start"], "end": session["end"]}]
        self.assertEqual(result(self.data)["activities"], [])

    def test_optional_conflict_does_not_exclude_required_attendable_activity(self):
        self.data["activities"][0]["sessions"].append({"id": 902, "required": False,
                                                       "start": "2026-10-27T00:30:00Z", "end": "2026-10-27T01:00:00Z"})
        self.assertEqual(len(result(self.data)["activities"]), 1)
        self.assertEqual(result(self.data)["activities"][0]["suggestedSessionIds"], [])

    def test_past_mandatory_session_cannot_be_satisfied(self):
        self.data["activities"][0]["sessions"][0]["start"] = "2026-10-26T23:00:00Z"
        self.assertEqual(result(self.data)["activities"], [])

    def test_absent_future_sessions_are_not_recommended(self):
        self.data["activities"][0].update(sessions=[], scheduleAvailability="UNKNOWN")
        self.assertEqual(result(self.data)["activities"], [])

    def test_club_membership_and_registration_filters_are_separate(self):
        self.student["memberships"] = [7]
        self.assertEqual(result(self.data)["clubs"], [])
        self.assertEqual(len(result(self.data)["activities"]), 1)
        self.student["activityRegistrations"] = [91]
        self.assertEqual(result(self.data)["activities"], [])

    def test_activity_filters(self):
        mutations = [{"campusCode": "HCM"}, {"published": False}, {"cancelled": True},
                     {"registrationOpen": False}, {"capacityAvailable": False}, {"eligibleUserIds": []},
                     {"registrationDeadline": self.data["asOf"]}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data = deepcopy(self.data)
                data["activities"][0].update(mutation)
                self.assertEqual(result(data)["activities"], [])

    def test_club_filters(self):
        for mutation in ({"active": False}, {"recruiting": False}, {"campusCode": "DAN"}):
            with self.subTest(mutation=mutation):
                data = deepcopy(self.data)
                data["clubs"][0].update(mutation)
                self.assertEqual(result(data)["clubs"], [])

    def test_internal_activities_require_membership_and_allow_member_club_across_campus(self):
        activity = self.data["activities"][0]
        activity["isInternal"] = True
        self.assertEqual(result(self.data)["activities"], [])
        self.student["memberships"] = [activity["clubId"]]
        self.assertEqual(len(result(self.data)["activities"]), 1)
        activity["campusCode"] = "HCM"
        self.assertEqual(len(result(self.data)["activities"]), 1)
        activity["isInternal"] = False
        self.assertEqual(result(self.data)["activities"], [])

    def test_internal_flag_requires_a_real_boolean(self):
        self.data["activities"][0]["isInternal"] = "false"
        with self.assertRaises(ValidationError):
            recommend(self.data)

    def test_weak_strong_and_four_middle_axes(self):
        self.data["activities"] = [clone_activity(self.data, i + 100, p, i + 10) for i, p in enumerate(PILLARS)]
        rows = result(self.data)["activities"]
        self.assertEqual(rows[0]["targetPillar"], "Research")
        self.assertEqual(rows[1]["targetPillar"], "Entrepreneurship")
        middle = [x["features"]["pillar"]["value"] for x in rows[2:]]
        self.assertEqual(middle, [.5] * 4)

    def test_equal_or_zero_radar_is_balanced(self):
        for score in (0, 50):
            with self.subTest(score=score):
                for axis in self.student["experience"]["pillars"]:
                    axis["score"] = score
                item = result(self.data)["activities"][0]
                self.assertEqual(item["features"]["pillar"]["value"], 1)
                self.assertIn({"code": "BALANCED_EXPLORATION", "pillar": "Research"}, item["reasons"])

    def test_tied_extrema_share_boosts(self):
        self.student["experience"]["pillars"][0]["score"] = 10.5
        self.data["activities"] = [clone_activity(self.data, 101, "Academic"), clone_activity(self.data, 102, "Research")]
        self.assertEqual([x["features"]["pillar"]["value"] for x in result(self.data)["activities"]], [1, 1])

    def test_overlapping_tie_groups_are_balanced(self):
        for i, axis in enumerate(self.student["experience"]["pillars"]):
            axis["score"] = i * .3
        self.assertEqual(result(self.data)["activities"][0]["features"]["pillar"]["value"], 1)

    def test_incompatible_or_non_authoritative_radar_is_not_zero(self):
        for mutation in ({"semesterId": "SPRING2026"}, {"isAuthoritative": False}, {"policyVersion": "other"}):
            data = deepcopy(self.data)
            data["students"][0]["experience"].update(mutation)
            if "policyVersion" in mutation:
                data["students"][0]["sourceVersions"]["experience"]["policyVersion"] = "other"
            item = result(data)["activities"][0]
            self.assertIsNone(item["features"]["pillar"]["value"])

    def test_cold_start_can_fallback_without_false_personalization(self):
        self.student.update(preferences=None, schedule=None, experience=None)
        self.data["clubs"][0]["scheduleAvailability"] = "UNKNOWN"
        item = result(self.data)["clubs"][0]
        self.assertIsNone(item["score"])
        self.assertIn({"code": "INSUFFICIENT_PROFILE"}, item["reasons"])

    def test_empty_interests_and_missing_major_are_unknown(self):
        self.student["preferences"].update(interestIds=[], declaredMajorCode=None)
        item = result(self.data)["activities"][0]
        self.assertIsNone(item["features"]["interest"]["value"])
        self.assertIsNone(item["features"]["major"]["value"])

    def test_measured_mismatch_remains_zero_in_denominator(self):
        self.student["preferences"].update(interestIds=["arts"], declaredMajorCode="DESIGN")
        row = result(self.data)["activities"][0]
        self.assertEqual(row["features"]["interest"]["value"], 0)
        self.assertAlmostEqual(row["score"], .4 / .95)

    def test_wallet_optional_and_known_zero_are_distinct(self):
        self.assertIsNone(result(self.data)["activities"][0]["features"]["walletAffinity"]["value"])
        self.student["walletSignals"] = {"7": {"availability": "KNOWN", "earnedTotal": 0, "revokedTotal": 0, "version": "w1"}}
        self.student["sourceVersions"]["wallet"] = {"version": "wallet-feed-1", "asOf": self.data["asOf"]}
        row = result(self.data)["activities"][0]
        self.assertEqual(row["features"]["walletAffinity"]["value"], 0)
        self.assertAlmostEqual(row["score"], .95)

    def test_affinity_uses_net_earning_not_spendable_balance(self):
        self.student["walletSignals"] = {"7": {"availability": "KNOWN", "earnedTotal": 2000, "revokedTotal": 1500, "version": "w1"}}
        self.student["sourceVersions"]["wallet"] = {"version": "wallet-feed-1", "asOf": self.data["asOf"]}
        row = result(self.data)["activities"][0]
        self.assertEqual(row["features"]["walletAffinity"]["value"], .5)
        self.student["walletSignals"]["7"]["balance"] = 200
        self.assertEqual(result(self.data)["status"], "UNAVAILABLE")  # Local/sample balance is not feed data.

    def test_caps_return_fewer_without_ineligible_refill(self):
        self.data["activities"] = [clone_activity(self.data, i) for i in range(100, 110)]
        self.assertEqual(len(result(self.data)["activities"]), 2)
        for i, row in enumerate(self.data["activities"]):
            row["clubId"] = i + 10
        self.assertEqual(len(result(self.data)["activities"]), 3)

    def test_stable_id_tie_and_candidate_order_independence(self):
        a, b = clone_activity(self.data, 102), clone_activity(self.data, 101)
        self.data["activities"] = [a, b]
        expected = recommend(self.data)
        self.data["activities"].reverse()
        self.assertEqual(expected, recommend(self.data))
        self.assertEqual([x["id"] for x in expected["results"][0]["activities"]], [101, 102])

    def test_future_source_rejects_batch(self):
        self.student["sourceVersions"]["schedule"]["asOf"] = "2026-10-27T00:00:01Z"
        with self.assertRaisesRegex(ValidationError, "after snapshot"):
            recommend(self.data)

    def test_invalid_student_does_not_affect_another_account(self):
        other = deepcopy(self.student)
        other["userId"] = 43
        self.data["students"].append(other)
        self.student["schedule"]["version"] = "stale"
        rows = recommend(self.data)["results"]
        self.assertEqual(rows[0]["status"], "UNAVAILABLE")
        self.assertEqual(rows[0]["activities"], [])
        self.assertEqual(rows[1]["activities"], [])  # Other account has no backend eligibility for activity 91.
        self.assertEqual(len(rows[1]["clubs"]), 1)

    def test_stale_required_source_is_unavailable(self):
        self.student["sourceVersions"]["account"]["asOf"] = "2026-10-20T00:00:00Z"
        self.assertEqual(result(self.data)["warnings"], ["STALE_SOURCE"])

    def test_unknown_metadata_cannot_smuggle_private_fields_into_results(self):
        self.student["preferences"] = None
        self.student["sourceVersions"]["preferences"]["email"] = "private@example.invalid"
        output = recommend(self.data)
        self.assertEqual(output["results"][0]["status"], "UNAVAILABLE")
        self.assertNotIn("private@example.invalid", json.dumps(output))

    def test_invalid_per_student_metadata_is_isolated(self):
        self.student["sourceVersions"] = None
        self.assertEqual(result(self.data)["status"], "UNAVAILABLE")

    def test_numerical_vector_tolerance_does_not_exceed_feature_bounds(self):
        self.data["activities"][0]["pillarWeights"] = {"Research": 1.0000001}
        # Individual components still must lie in [0,1].
        with self.assertRaises(ValidationError):
            recommend(self.data)
        self.data["activities"][0]["pillarWeights"] = {"Research": .9999999}
        self.assertLessEqual(result(self.data)["activities"][0]["features"]["pillar"]["value"], 1)

    def test_duplicate_ids_and_invalid_vectors_reject_batch(self):
        self.data["clubs"].append(deepcopy(self.data["clubs"][0]))
        with self.assertRaises(ValidationError):
            recommend(self.data)
        self.data = snapshot()
        self.data["activities"][0]["pillarWeights"] = {"Research": .5}
        with self.assertRaises(ValidationError):
            recommend(self.data)

    def test_bad_dates_flags_and_required_fields(self):
        for mutation in ({"registrationDeadline": "2026-10-27"}, {"published": "true"}, {"campusCode": {}},
                         {"sessions": [{"id": 1, "start": "2026-10-27T08:00:00Z", "end": "2026-10-27T07:00:00Z", "required": True}]}):
            data = deepcopy(self.data)
            data["activities"][0].update(mutation)
            with self.assertRaises(ValidationError):
                recommend(data)

    def test_nonfinite_bool_id_and_radar_duplicate_are_invalid(self):
        self.student["userId"] = True
        with self.assertRaises(ValidationError):
            recommend(self.data)
        self.data = snapshot()
        self.data["students"][0]["experience"]["pillars"][0]["score"] = float("nan")
        self.assertEqual(result(self.data)["status"], "UNAVAILABLE")

    def test_expiration_and_explicit_evaluation(self):
        recommend(self.data, evaluated_at=self.data["asOf"])
        with self.assertRaises(ValidationError):
            recommend(self.data, evaluated_at=self.data["expiresAt"])

    def test_config_changes_require_a_new_version(self):
        with self.assertRaises(ValidationError):
            Config.from_dict({"configVersion": "initial-defaults-1", "clubLimit": 3})
        config = {"configVersion": "test-small", "clubLimit": 1, "activityLimit": 1}
        self.assertEqual(recommend(self.data, config)["configVersion"], "test-small")
        with self.assertRaises(ValidationError):
            recommend(self.data, {"configVersion": "bad", "weights": {"interest": 1}})


if __name__ == "__main__":
    unittest.main()
