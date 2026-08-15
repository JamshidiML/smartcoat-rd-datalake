# SmartCoat R&D Data Lake — Phase 1

Local-only, photo-first R&D ingestion pilot. The system preserves every source file and provenance manifest in immutable Bronze storage, creates machine-produced Silver drafts, and requires an explicit human review before a verified Silver record exists.

## Scope and safety

- Bronze: MinIO originals and manifests with versioning, object lock, and 365-day compliance retention.
- Silver: versioned PostgreSQL drafts, review decisions, verified records, and append-only audit events.
- OCR: local PaddleOCR with a Tesseract benchmark. No source is sent to a cloud OCR or vision API.
- Deployment: Docker Compose on the founder's Apple-Silicon MacBook. Every published port is bound to `127.0.0.1`.
- Data: only synthetic fixtures belong in Git. Real pilot files stay in local MinIO and the ignored `.local-data/` directory.

Gold data, dashboards, ML discovery, embeddings, vector databases, SmartCoat integration clients, SSO, multi-department rollout, and public hosting are deliberately out of scope.

## Start locally

1. Copy `.env.example` to `.env` and replace every `change-me` value.
2. Run `docker compose up --build -d`.
3. Run `./scripts/create-pilot-users.sh` once PostgreSQL is healthy.
4. Open `http://127.0.0.1:8080` and sign in with the local pilot credentials.

MinIO is available only on `http://127.0.0.1:9000`; its local console is `http://127.0.0.1:9001`. The API is `http://127.0.0.1:8000`. See [the local deployment runbook](docs/runbooks/VPS_DEPLOYMENT.md) for bootstrap, credential rotation, backup, and troubleshooting.

Activity and Review queue update automatically while OCR is queued or processing. The local default is `OCR_PIPELINE_PROFILE=fast`: deterministic EXIF normalization, a 2400-pixel long-edge cap, contrast normalization, and deskew run before Paddle detection/recognition, without the three expensive orientation/unwarping models. Set `OCR_PIPELINE_PROFILE=accurate` and rebuild the worker only when a difficult skewed or distorted batch justifies the slower profile. Every OCR run records the selected profile and preprocessing version in PostgreSQL and its raw artifact.

## Quality gates

```bash
python3 -m unittest discover -s apps/api/tests -p 'test_*.py' -v
python3 -m unittest discover -s apps/ocr-worker/tests -p 'test_*.py' -v
./scripts/verify-bronze-integrity.sh
```

The automated suite maps every required control to AT-01 through AT-14. It does not complete Phase 1 by itself: the authorized 5–10-file local batch, reviewer sign-off, and successful backup/restore drill remain mandatory completion gates.

## Repository boundaries

This is a standalone repository with no runtime dependency on `smartcoat-intelligence`. The local-only topology is intentionally temporary; the Bronze and Silver contracts are designed to move unchanged to a future shared VPS deployment.
