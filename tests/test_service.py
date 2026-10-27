from copy import deepcopy
from datetime import timedelta
import http.client
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from recommendation_engine.engine import recommend
from recommendation_engine.service import (MongoStore, Runtime, ThreadingHTTPServer,
    feed_key, fingerprint, handler, utcnow)
from recommendation_engine.validation import ValidationError


def snapshot():
    value = json.loads((Path(__file__).parents[1] / 'docs/contracts/examples/input-v1.json').read_text())
    now = utcnow()
    value.update(snapshotId=uuid4().hex, asOf=now.isoformat(), expiresAt=(now + timedelta(minutes=15)).isoformat())
    for metadata in value['students'][0]['sourceVersions'].values():
        metadata['asOf'] = now.isoformat()
    value['students'][0]['schedule']['busyIntervals'] = []
    value['clubs'][0]['meetingIntervals'] = []
    activity = value['activities'][0]
    activity['registrationDeadline'] = (now + timedelta(minutes=10)).isoformat()
    activity['sessions'][0].update(start=(now + timedelta(hours=1)).isoformat(), end=(now + timedelta(hours=2)).isoformat())
    return value


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        self.runtime = Runtime(self.store, 'synthetic-token-' + 'x' * 32)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.runtime))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method, path, body=None, authorized=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if authorized:
            headers['X-Recommendation-Token'] = self.runtime.token
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        connection.close()
        return result

    def test_unauthorized_read_and_write_never_touch_store(self):
        self.assertEqual(self.request('GET', '/internal/recommendations/users/42?semesterId=FALL2026', authorized=False)[0], 401)
        self.assertEqual(self.request('POST', '/internal/feeds', {}, False)[0], 401)
        self.store.assert_not_called()
        self.store.read.assert_not_called()
        self.store.submit.assert_not_called()

    def test_get_only_reads_and_missing_does_not_run_ranker(self):
        self.store.read.return_value = {'status': 'MISSING', 'items': [], 'available': False}
        status, body = self.request('GET', '/internal/recommendations/users/42?semesterId=FALL2026&type=activities')
        self.assertEqual((status, body['status']), (200, 'MISSING'))
        self.store.read.assert_called_once_with(42, 'FALL2026', 'activities')
        self.store.submit.assert_not_called()
        self.store.claim.assert_not_called()

    def test_feed_is_queued_without_processing_on_request_thread(self):
        value = snapshot()
        self.store.submit.return_value = 'QUEUED'
        self.assertEqual(self.request('POST', '/internal/feeds', {'snapshot': value, 'readStartedAt': value['asOf']})[0], 202)
        self.store.claim.assert_not_called()
        self.store.submit.assert_called_once_with(value, value['asOf'])

    def test_invalid_types_and_overbroad_invalidation_are_rejected(self):
        for path in ['/internal/recommendations/users/0?semesterId=FALL2026',
                     '/internal/recommendations/users/42?semesterId=FALL2026&type=profiles']:
            self.assertEqual(self.request('GET', path)[0], 400)
        self.assertEqual(self.request('POST', '/internal/invalidations', {})[0], 400)
        self.assertEqual(self.request('POST', '/internal/invalidations', {'userIds': [True]})[0], 400)

    def test_health_requires_database_and_live_worker(self):
        self.assertEqual(self.request('GET', '/health/ready', authorized=False)[0], 200)
        self.runtime.last_worker_tick = utcnow() - timedelta(minutes=7)
        self.assertEqual(self.request('GET', '/health/ready', authorized=False)[0], 503)

    def test_fingerprint_ignores_observation_clock_but_tracks_actual_features(self):
        value = snapshot()
        other = deepcopy(value)
        other['snapshotId'] = uuid4().hex
        other['asOf'] = utcnow().isoformat()
        other['students'][0]['sourceVersions']['account']['asOf'] = other['asOf']
        self.assertEqual(fingerprint(value), fingerprint(other))
        other['students'][0]['preferences']['interestIds'] = ['sports']
        self.assertNotEqual(fingerprint(value), fingerprint(other))
        value['students'].append(deepcopy(value['students'][0]))
        with self.assertRaises(ValidationError):
            feed_key(value)


@unittest.skipUnless(os.environ.get('MONGODB_TEST_URI'), 'Real Mongo tests run in service CI')
class MongoAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.database = 'test_recommendation_' + uuid4().hex
        self.store = MongoStore(os.environ['MONGODB_TEST_URI'], self.database)

    def tearDown(self):
        self.store.client.drop_database(self.database)
        self.store.client.close()

    def claim(self):
        self.store.jobs.update_many({}, {'$set': {'dueAt': utcnow() - timedelta(seconds=1)}})
        return self.store.claim()

    def process(self, job):
        return self.store.publish(job, recommend(job['feed'], evaluated_at=utcnow().isoformat()))

    def test_feed_rank_persist_restart_and_read(self):
        value = snapshot()
        self.assertEqual(self.store.submit(value), 'QUEUED')
        self.assertEqual(self.store.submit(value), 'UNCHANGED')
        self.assertEqual(self.store.read(42, 'FALL2026', 'activities')['status'], 'PENDING')
        job = self.claim()
        self.assertTrue(self.process(job))
        second = MongoStore(os.environ['MONGODB_TEST_URI'], self.database)
        try:
            result = second.read(42, 'FALL2026', 'activities')
            self.assertTrue(result['available'])
            self.assertEqual(result['items'][0]['id'], 91)
            self.assertEqual(result['snapshotId'], value['snapshotId'])
        finally:
            second.client.close()

    def test_old_worker_cannot_publish_over_new_feed_or_invalidation(self):
        value = snapshot()
        self.store.submit(value)
        old = self.claim()
        updated = snapshot()
        updated['students'][0]['preferences']['interestIds'] = ['sports']
        self.store.submit(updated)
        self.assertFalse(self.process(old))
        # A stale lease can be reclaimed even after the worker process has died.
        self.store.jobs.update_many({}, {'$set': {'leaseUntil': utcnow() - timedelta(seconds=1)}})
        current = self.claim()
        self.store.invalidate([42])
        self.assertFalse(self.process(current))
        self.assertEqual(self.store.submit(updated, value['asOf']), 'RETRY_SOURCE_CHANGED')
        fresh = snapshot()
        self.assertEqual(self.store.submit(fresh), 'QUEUED')

    def test_expiry_is_enforced_without_waiting_for_ttl_monitor(self):
        self.store.submit(snapshot())
        self.assertTrue(self.process(self.claim()))
        self.store.jobs.update_many({}, {'$set': {'resultExpiresAt': utcnow() - timedelta(seconds=1)}})
        result = self.store.read(42, 'FALL2026', 'activities')
        self.assertEqual((result['status'], result['items'], result['available']), ('STALE', [], False))
        self.assertTrue(self.store.jobs.find_one({})['needsFeed'])

    def test_expired_lease_owner_cannot_publish_after_reclaim(self):
        self.store.submit(snapshot())
        old = self.claim()
        self.store.jobs.update_many({}, {'$set': {'leaseUntil': utcnow() - timedelta(seconds=1)}})
        new = self.claim()
        self.assertNotEqual(old['leaseToken'], new['leaseToken'])
        self.assertFalse(self.process(old))
        self.assertTrue(self.process(new))

    def test_older_feed_and_time_boundary_do_not_restore_invalid_results(self):
        older = snapshot()
        newer = snapshot()
        self.store.submit(newer)
        self.assertEqual(self.store.submit(older), 'IGNORED_OLDER_FEED')
        job = self.claim()
        batch = recommend(job['feed'], evaluated_at=utcnow().isoformat())
        job['feed']['activities'][0]['registrationDeadline'] = (utcnow() - timedelta(seconds=1)).isoformat()
        self.assertFalse(self.store.publish(job, batch))
        self.assertFalse(self.store.read(42, 'FALL2026', 'activities')['available'])
