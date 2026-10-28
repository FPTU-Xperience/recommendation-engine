# WIP checkpoint — 2026-10-28

Continue with API branch `cuongnk/implement-api-contract` and UI branch `cuongnk21/integrate-api-email-login`. Detailed cross-repository state and next steps are in API `WIP-HANDOFF.md`; service design/provisioning is in `docs/service-deployment.md`.

Last validated rcm commit: `c9e79e3ee5c59d63625299114f2e76c056b59697` (55 tests, real Mongo, image build). Combined deployment run:

https://github.com/FPTU-Xperience/fptu-xperience-clubhub-api/actions/runs/40551731488

Mongo was provisioned, but Python failed to start because the tmpfs YAML list split `noexec`/`nosuid` into invalid mount paths. This WIP quotes the complete tmpfs entry; it has not been redeployed. The API rollout was skipped. Preserve the existing Mongo volume and host password. Launch a new combined deployment with this WIP's rcm SHA; rerunning the old failed job reuses the old SHA.

This remains a deterministic V1 ranker, not an ML training service. Taxonomy/club metadata, feedback ingestion, wallet data, backups and scale tuning remain follow-up work.
