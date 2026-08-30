# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Phase 1 of a local-only, photo-first R&D data lake pilot: a solo founder ingests lab
photos/PDFs/spreadsheets, a local OCR worker drafts a machine transcription, and a human
reviewer must explicitly verify it before a `VERIFIED` Silver record exists. Everything runs
via Docker Compose on one Apple-Silicon MacBook; every published port is bound to
`127.0.0.1` and nothing leaves the machine (no cloud OCR/vision API, no public route).

The repository is standalone — no runtime dependency on `smartcoat-intelligence`. The Bronze
object keys, manifests, Silver schemas, and audit contracts are designed to move unchanged to
a future shared VPS deployment; only auth and deployment topology will change then.

Full narrative architecture: `docs/architecture/PHASE_1_ARCHITECTURE.md`. Governance policies:
`docs/governance/*.md` (Bronze immutability, Silver review, acceptance-report template).

## Commands

Local stack:
```bash
cp .env.example .env   # then replace every change-me value
docker compose up --build -d
./scripts/create-pilot-users.sh   # once postgres is healthy
```
Web `127.0.0.1:8080`, API `127.0.0.1:8000`, MinIO S3 `127.0.0.1:9000` / console `:9001`,
Postgres `127.0.0.1:5432`.

Quality gates (same ones CI runs, in `.github/workflows/ci.yml`):
```bash
ruff check apps scripts                                         # lint, no config file — ruff defaults
python apps/api/tests/validate_repository.py                    # JSON Schema contracts + Compose localhost/isolation invariants
python -m unittest discover -s apps/api/tests -p 'test_*.py' -v
python -m unittest discover -s apps/ocr-worker/tests -p 'test_*.py' -v
python apps/api/tests/repository_scan.py                        # AT-14: no secrets/real-data files tracked in git
docker compose --env-file .env.example build api web
./scripts/verify-bronze-integrity.sh                             # re-hashes every locked Bronze object against its manifest
```

Run a single test, e.g. one acceptance case:
```bash
python -m unittest apps.api.tests.test_acceptance.AcceptanceTests.test_at_04_duplicate_is_separate_and_linked -v
# (run from repo root so `apps` resolves as a package path)
```
Tests import application code via `sys.path.insert(0, ".../src")` at the top of each test
module (see `apps/api/tests/test_acceptance.py`), not via installed packages, so they only
need the plain `unittest` runner — no pytest, no package install step.

The automated suite maps every required control to **AT-01 through AT-14** (see test names in
`apps/api/tests/test_acceptance.py`). Passing it does not complete Phase 1 by itself: an
authorized 5–10-file local batch, reviewer sign-off, and a successful backup/restore drill
(`scripts/restore-drill.sh`) are separate, mandatory completion gates
(`docs/governance/PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md`).

## Repository layout

```
apps/api/src/        FastAPI app: HTTP layer + domain services + Postgres/MinIO adapters
apps/api/tests/       unittest suite against in-memory fakes (fakes.py) + repo-wide checks
apps/ocr-worker/src/  polling worker: preprocess -> PaddleOCR/openpyxl -> Silver draft
apps/ocr-worker/tests/ "contract" tests that assert on worker/engine source text (see below)
apps/web/src/          static HTML/CSS/vanilla JS SPA served by a hardened stdlib HTTP server
packages/contracts/    JSON Schemas for the Bronze manifest and Silver/review payloads
infra/postgres/init.sql   full DB schema, CHECK constraints, append-only triggers
infra/minio/            bootstrap.sh (buckets, versioning, Object Lock, service policies)
docs/architecture/, docs/governance/, docs/runbooks/   design decisions and operational policy
scripts/                pilot-user creation, batch run, integrity verification, restore drill
```

## Architecture: the ingestion pipeline

Everything is driven by one `uploads.state` state machine, transitioned only through
`PostgresRepository.transition()` (a compare-and-swap `UPDATE ... WHERE state = previous`,
each transition appended to `audit_events`):

```
RECEIVED -> BRONZE_COMMITTED -> OCR_QUEUED -> OCR_COMPLETED
-> SILVER_DRAFT_READY -> UNDER_HUMAN_REVIEW -> VERIFIED
```
Terminal/failure states: `REJECTED`, `OCR_FAILED`, `REVIEW_REJECTED`. There is no
`OCR_COMPLETED -> VERIFIED` shortcut — a human review step is structurally required.

Three domain services in `apps/api/src/domain.py` own this pipeline; `main.py` (FastAPI) is a
thin HTTP shell over them, and `apps/ocr-worker/src/jobs/worker.py` drives the second one from
a poll loop rather than HTTP:

- **`IngestionService.ingest`** — validates the upload (`validation.py`: extension vs.
  sniffed magic bytes, PDF/XLSX/XLS corruption and password checks, 50 MB pilot cap), computes
  SHA-256, writes the original + a canonical-JSON manifest to MinIO via `storage.put_once`
  (stat-before-put, refuses to overwrite an existing key — Bronze objects are one-shot), reads
  both back to verify bytes match, then commits the Bronze registry row and immediately queues
  an OCR job. A duplicate (same SHA-256) is *not* deduplicated — it becomes its own
  `ingestion_id` row with `duplicate_of_ingestion_id` pointing at the first.
- **`OCRDomainService.start`/`complete`** — called by the OCR worker, not the API. Writes only
  `DRAFT_UNVERIFIED` Silver drafts; it can never produce a `VERIFIED` record.
- **`ReviewService.review`/`edit_verified`** — the *only* path that can write a verified Silver
  revision. Requires `explicit_confirmation: true` and non-empty `verified_text` for an
  approval decision. Detects self-review (reviewer == uploader) and, in Phase 1
  (`ALLOW_PHASE_1_SOLO_SELF_REVIEW=true`, the default), auto-applies a recorded exception
  (`solo_exception_applied`, reason `PHASE_1_SOLO_FOUNDER_REVIEW`) instead of rejecting it —
  this flag must be set `false` before a second reviewer is introduced. Editing an already-
  verified record doesn't mutate it: it creates a new `DRAFT_UNVERIFIED` draft, drops the
  upload back to `UNDER_HUMAN_REVIEW`, and a fresh approval creates `silver_revision + 1`.
  Verified rows and review decisions are append-only (enforced by Postgres triggers, not just
  application code).

Object storage buckets (`domain.py` constants): `sc-rd-bronze-originals`,
`sc-rd-bronze-manifests` (both versioned + Object Lock, 365-day compliance retention — see
`docs/governance/BRONZE_IMMUTABILITY_POLICY.md`), `sc-rd-ocr-artifacts` (preprocessed images +
raw OCR/benchmark JSON, unlocked). Keys are namespaced `rd/{yyyy}/{mm}/{ingestion_id}/...` and
never contain identity or document content — `identifiers.uuid7()` generates the sortable ID.

## OCR worker

`apps/ocr-worker/src/jobs/worker.py` polls `claim_next_job()` every `OCR_POLL_SECONDS`. On
`RUNNING` jobs left over from a crash it calls `recover_interrupted_ocr_jobs()` at startup, which
resets them to `QUEUED` and audits `OCR_JOB_RECOVERED`. Excel files go through `extract/excel.py`
(openpyxl, no image path); everything else goes through `preprocess/documents.py` +
`preprocess/images.py` (EXIF-transpose, long-edge cap at `OCR_MAX_IMAGE_SIDE`, autocontrast,
deskew) into `extract/paddle_engine.py` (PaddleOCR) with `extract/tesseract_benchmark.py` run
against the *same* preprocessed images for comparison. Preprocessed images and raw OCR JSON are
written to `sc-rd-ocr-artifacts` under a `v{PREPROCESSING_VERSION}-max-{MAX_IMAGE_SIDE}/` prefix
so a retry with identical inputs reuses the same key (`StateConflict` on mismatch, not silent
overwrite).

`OCR_PIPELINE_PROFILE` (`fast` default vs `accurate`) toggles PaddleOCR's document-orientation/
unwarping/textline-orientation models — `fast` skips them for speed; `accurate` needs a worker
rebuild. Every run records its profile and preprocessing version in Postgres and the raw
artifact. PaddlePaddle 3.2.2 is deliberately pinned below the tested-broken 3.3.x (oneDNN CPU
regression) with `FLAGS_enable_pir_api=0` set before importing `paddleocr` — do not bump either
without re-reading the comment at the top of `paddle_engine.py`. The worker is the only service
running under `platform: linux/amd64` (Docker's compatibility layer on Apple Silicon, because
the Linux PaddlePaddle wheel is x86-64 only); everything else in `compose.yaml` runs native.

**`apps/ocr-worker/tests/test_worker_contract.py` is a "contract" suite that asserts on the
literal source text** of `worker.py`, `paddle_engine.py`, `Dockerfile`, `compose.yaml`, etc.
(e.g. it requires the exact string `'os.getenv("OCR_PIPELINE_PROFILE", "fast")'` to appear).
It's there to pin architectural decisions (pinned versions, no external OCR API, fast-profile
flags, retry/recovery behavior) so a refactor that changes wording without changing behavior
can still break CI — check this file before renaming things it inspects.

## Trust boundaries and conventions worth preserving

- Upload type is decided by sniffing magic bytes (`validation._detect_mime`), never by
  filename/browser-supplied content type; extension and detected type must agree.
- MinIO service identities are separated by function: app/ingestion can put/get Bronze but not
  delete, OCR can read originals and write artifacts, backup is read-only, and humans get no
  MinIO credential at all (`infra/minio/bootstrap.sh`, `infra/minio/policies/*.json`).
  Root MinIO credentials only ever reach `minio-bootstrap`.
- Postgres has a separate non-superuser app role (`smartcoat_app`, created in `init.sql` from
  `POSTGRES_APP_PASSWORD`) distinct from the bootstrap/admin role.
- Auth is a single local env-configured password (`hmac.compare_digest`) plus a short-lived
  HMAC-signed bearer session (`security.py`) — no external identity provider yet, but the
  `users`/`uploader_user_id`/`reviewer_user_id`/audit actor columns are already normalized for
  one to be dropped in later.
- `packages/contracts/*.schema.json` are the source of truth for the Bronze manifest and
  Silver/review payload shapes; `validate_repository.py` checks they're valid Draft 2020-12
  schemas and that `compose.yaml` still only publishes `127.0.0.1` ports with the backend
  network `internal: true` and the OCR worker attached only to `backend`.
- Only synthetic fixtures belong in git (`fixtures/synthetic/`); real pilot files stay in local
  MinIO / the gitignored `.local-data/`. `repository_scan.py` (AT-14) enforces this by failing
  CI on tracked files with real-data extensions outside `fixtures/synthetic/` or on secret-like
  patterns in tracked text files.
- API tests run entirely against in-memory fakes (`apps/api/tests/fakes.py`: `MemoryRepository`,
  `MemoryStorage`) implementing the same `Repository`/`ObjectStorage` `Protocol`s as the real
  Postgres/MinIO adapters — no database or object store needed to run them.
