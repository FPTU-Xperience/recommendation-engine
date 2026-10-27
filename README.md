# Recommendation Engine

Python service/job for suggesting FPTU-Xperience clubs and activities.

V1 is a deterministic, content-based ranker using ordinary Python collections and the standard library. No Spark, distributed processing, model training or vector database is required. This repository currently contains the design contract; the engine is not implemented yet.

- [V1 integration and ranking contract](docs/contracts/recommendation-v1.md)
- [Example input](docs/contracts/examples/input-v1.json)
- [Example output](docs/contracts/examples/output-v1.json)

Start with backend-exported JSON snapshots and a Python batch job. Backend serves the resulting recommendations after rechecking live availability. The input/output boundary can later be reused by an HTTP worker or a Spark pipeline without changing frontend business rules.
