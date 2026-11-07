# How this repository was split

This repository, `retail-data-platform-ops`, is one of three public sibling repositories split from a single earlier monorepo, `retail-data-platform`:

| Repository | Owns |
|---|---|
| `retail-data-pipeline` | `pipeline/` and `data/`: synthetic data, incremental load, tests, offline demo (source of truth for those directories) |
| **`retail-data-platform-ops`** (this one) | CI, Dockerfile, AWS Terraform (offline-validated only, never planned or applied), local monitoring/alerting (`retail_ops/`, `platform/`) |
| `retail-ai-support-app` | SYNTHETIC support-ticket triage (`ai_app/`) with a fake/offline provider and evaluation harness |

## Rules of the split

- **No shared runtime package.** Each repo installs and runs on its own.
- This repo **vendors a copy of `pipeline/` and `data/`** from `retail-data-pipeline` so the monitoring demo runs standalone. The copy **must not drift silently**: re-sync deliberately when the pipeline repo changes, and re-run the checks here.
- Showcase video: `docs/showcase.mp4` (silent, offline-rendered).
- This product was built in the earlier monorepo; the videos and the split were done afterwards.
- Verification of the split itself is in [`../EVIDENCE.md`](../EVIDENCE.md).
