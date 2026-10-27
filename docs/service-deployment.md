# Recommendation service (rcm)

## Ownership and data flow

ClubHub SQL services remain the source of truth. AdminService's background exporter reads a bounded page of active accounts from Auth, authoritative normalized radar from Admin, club scope from Club and eligible candidate/session data from Activity. It sends one user/semester snapshot to rcm. No email, names, phone numbers, date of birth, subject names, room names or evidence is exported.

`POST /internal/feeds` accepts `{ "readStartedAt": "UTC instant", "snapshot": { ...v1 snapshot... } }` and returns 202 (`QUEUED`, `UNCHANGED`, `IGNORED_OLDER_FEED`, `RETRY_SOURCE_CHANGED`). Ranking happens in a separate worker thread. Reconciliation retries rejected/failed feeds.

`GET /internal/recommendations/users/{userId}?semesterId=...&type=activities|clubs` reads indexed Mongo state only. `MISSING`, `PENDING` and `STALE` return no items with `available:false`. `READY`/`PARTIAL` include ranked IDs, score/reasons, source versions, generation, rule/config versions and expiry. A missing record is populated by the next exporter sweep; the GET never computes a result.

`POST /internal/invalidations` takes `{ "userIds": [42] }`; explicit `null` invalidates all currently stored scopes. The API is private and requires `X-Recommendation-Token` (minimum 32 characters). `/health/ready` probes Mongo and the worker heartbeat. Configure transport TLS if services move off the private Docker network.

## Update and failure semantics

- Startup and reconciliation sweep every 60 seconds by default (`Recommendation:RefreshSeconds`, 15–3600). One active semester is required; there is no arbitrary choice if multiple semesters are active. Per-user failures retain previous expiry and retry next sweep.
- Source commits write `recommendation.source.changed` to a transactional outbox. Auth covers profile/onboarding/account/timetable; Club covers club/membership/manager changes; Activity covers activity/session/registration; Admin covers ledger effects, scoring policies, academic calendar and semester changes. IDs only, no profile payloads.
- Admin's independent Redis consumer acknowledges only after Mongo invalidation succeeds, then wakes the exporter. Retries can invalidate again; reconciliation provides recovery if stream retention loses an event. User-specific events affect their stored semester scopes. Catalogue/calendar events conservatively invalidate all; optimize fan-out after measuring scale.
- Each Mongo document owns feed + generation + lease + complete club/activity result for `(userId, semesterId)`. A unique compound index prevents duplicate scopes. Generation and lease-token compare-and-set rejects obsolete workers; expired leases allow crash recovery. A source read started before a recorded invalidation must be retried.
- Changed source features queue work after a two-second delay; unchanged snapshots do not rerank. Results refresh within five minutes of their expiry. Snapshot validity is 15 minutes in the ClubHub exporter and capped at 24 hours at ingestion. Deadlines/session starts shorten result expiry even without events.
- Reads check expiry explicitly. TTL removes inactive documents after 30 days; TTL is not the freshness mechanism. Rcm does not serve stale rankings as fresh. Public .NET GETs batch-check live eligibility/capacity/deadlines and candidate versions; invalid items are removed and trigger invalidation.
- Existing source APIs are not a distributed SQL snapshot. Version checks, invalidation and reconciliation give eventual consistency. Current account/eligibility checks remain authoritative at delivery and registration.

## Data currently unavailable

V1 is reranking, not ML training. Impression/click/dismiss feedback ingestion and scheduled model training are not implemented by this release.

Interest and major labels require controlled mappings. Set `Recommendation:InterestMappings:<label>` and `Recommendation:MajorMappings:<label>`; canonical interest IDs pass through. Unmapped labels remain unknown. Activity tags/majors can be supplied through `Recommendation:Activities:<id>:Interests|MajorCodes`; pillar weights come from approved activity data. Wallet signals remain unavailable.

Club matching uses the live ClubService recruitment flag and requires explicit `Recommendation:Clubs:<clubCode>:PillarWeights`, optional `Interests` and `MajorCodes`. Clubs without validated metadata are excluded rather than assigned fabricated scores. Meeting times remain unknown. Published activity sessions currently have no optional/required flag: all are treated as required conservatively. Legacy activities without published sessions are excluded. Both timetable blocks with exact calendar versions are required to claim known schedule availability; missing/stale blocks remain unknown. Slot times match the student UI and are converted from UTC+7 to UTC.

## Provisioning on the API host

The initial cost-conscious topology is separate Docker Compose projects on the existing Ubuntu API host. The API's external Docker network is shared with the rcm service; Mongo is isolated on a dedicated internal data network. Neither container publishes a host port. Mongo data lives in the stable named volume `fptux-recommendation-mongo`. Compute caps are Python 384 MiB/0.5 CPU and Mongo 640 MiB/0.5 CPU; these are caps, not capacity guarantees. Check available RAM/disk and monitor OOM/restart counts on the shared host. Moving to another VPC machine later requires no public API change.

Preferred deployment entry point: ClubHub API's **Deploy to Ubuntu VPC** workflow with `deploy_rcm=true`, `rcm_ref=<commit SHA>`. It validates both revisions, copies immutable releases, discovers the live API Compose project's network, and provisions rcm before rolling the API. It reuses the existing internal workflow token as the trusted service token; no new public credential is introduced. A random Mongo password is generated on the host once and retained in a mode-600 file outside release folders. The Python container alone receives the Mongo credential. Existing SQL/Redis and their volumes are retained.

For independent deployments use this repo's **Deploy recommendation to API VPC** workflow with VPC_HOST, VPC_USERNAME, VPC_SSH_KEY (+ optional passphrase), RECOMMENDATION_TOKEN and RECOMMENDATION_MONGODB_PASSWORD secrets, plus API_COMPOSE_NETWORK variable. Standalone token must match the API configuration. Password must be a 32–128 character URL-safe value. Initialization variables cannot rotate a password on an existing Mongo volume; rotate in Mongo deliberately before changing deployment secrets. The deployment script rejects accidental credential changes.

`ops/deploy.sh` builds an immutable SHA-tagged image, preserves the Mongo container/volume, waits for application readiness, and rolls the Python container back to its previous image on failure. Releases and previous images remain available. API rollout has its own rollback. This is not a distributed transaction: rcm can remain on its new backward-compatible revision if API deployment later fails.

Enable API settings `Recommendation:Enabled=true`, `BaseUrl=http://recommendation-service:8080/`, `Token=<matching token>`. Disabled mode retains the prior in-process activity recommendation baseline. Enabled mode does not silently switch back to the baseline during outages. Public endpoints remain `/api/v2/recommendations/activities` and `/api/v2/recommendations/clubs` with self-user authorization and PERIOD scope.

Before production operation, schedule encrypted off-host Mongo backups using `mongodump` and test a restore; a Docker volume alone is not a backup. Do not run `docker compose down -v`. Recomputable results do not replace SQL backups. Check private `/health/ready`, API health, worker failures, Mongo disk usage, memory pressure, event lag and result age after each rollout. Do not log tokens or feed bodies.

## Validation

`python -m unittest discover -s tests -v` runs engine and HTTP tests. Set `MONGODB_TEST_URI` for real Mongo persistence, generation races, lease recovery and expiration acceptance tests. Service CI supplies Mongo 8 and builds the real image. Deployment runs validation again against the exact rcm revision. No production data is used in tests.
