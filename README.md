# Recommendation Engine

Python service/job for suggesting FPTU-Xperience clubs and activities.

V1 is a deterministic, content-based ranker using ordinary Python collections and the standard library. The CLI remains dependency-free. The private HTTP API and durable worker use MongoDB through the optional `requirements-service.txt` dependencies. ClubHub exports authoritative feeds and serves processed results through its authenticated .NET API. No Spark or model training is required.

- [V1 integration and ranking contract](docs/contracts/recommendation-v1.md)
- [Example input](docs/contracts/examples/input-v1.json)
- [Example output](docs/contracts/examples/output-v1.json)
- [Default configuration](config/default.json)

Backend snapshots feed the durable worker; it evaluates candidates and atomically publishes versioned results in MongoDB. Reads only load processed results. The backend rechecks live availability and hydrates current activity/club DTOs. The same ranking boundary can later be reused by a training or Spark pipeline.

- [Service, provisioning and deployment runbook](docs/service-deployment.md)

## Run

Python 3.11 or newer, from the repository root:

```shell
python -m recommendation_engine --input data/snapshot.json --output output/recommendations.json --config config/default.json
```

Create the destination directory first. The CLI rejects expired/future snapshots using the current UTC clock, validates versions and writes output via atomic replacement. It never overwrites its input/config. Exit code 1 means input/config/publication failed; a published batch can contain individual `UNAVAILABLE` students, reported in the aggregate counts. Logs do not print student payloads. Protect input/output directories with deployment access controls; these are private per-user recommendations, not public assets. Real snapshots and generated files are gitignored.

Replay the committed synthetic fixture independently of today's date:

```shell
python -m recommendation_engine --input docs/contracts/examples/input-v1.json --output output/example.json --evaluation-time 2026-10-27T00:00:00Z
```

`--evaluation-time` is for offline evaluation only; production jobs should use the default clock. The pure `recommend(snapshot, config)` function is deterministic and does no I/O; callers serving results must enforce expiration. CLI input is limited to 50 MiB and config to 64 KiB. Maximum feed cardinalities are 10,000 students, 10,000 clubs, 50,000 activities; these are validation bounds, not throughput claims.

## Test

```shell
python -m unittest discover -s tests -v
```

Tests cover ranking, cold start, privacy, schedule conflicts, eligibility, source versions, malformed inputs and atomic file failures. GitHub Actions runs the same suite on Linux/Windows with Python 3.11/3.13. Optional package/console-script installation: `python -m pip install .`; packaging needs setuptools, while running from the checkout has no external dependencies.

## Layout

| Path | Responsibility |
| --- | --- |
| `recommendation_engine/engine.py` | Pure filtering, features, pillar balance, diversity caps and ranking. |
| `recommendation_engine/validation.py` | Bounded schema, types, versions and timestamps. |
| `recommendation_engine/config.py` | Versioned configurable scoring coefficients. |
| `recommendation_engine/cli.py` | JSON transport, expiry checks, private aggregate logs and atomic publication. |
| `tests/` | Synthetic engine and CLI acceptance tests. |

Default weights are heuristics, not a trained or empirically validated model. Missing signals stay unknown; wallet affinity uses real net earning history and never sample credits or spendable balance. The backend remains responsible for live access, capacity and registration checks.
