#!/usr/bin/env python3
"""Opt-in destroyed-volume PostgreSQL and MinIO recovery acceptance."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/disaster-recovery.sh"
CONFIRM = "--confirm-disposable-synthetic-destroyed-volume-run"
PASS = "PASS_REAL_DESTROYED_VOLUME_RESTORE"
POSTGRES_IMAGE = "sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
MINIO_IMAGE = "sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0"
MC_IMAGE = "sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc"
API_IMAGE = "sha256:cd276d9b3b8c3c083bb037cc88be592306f531337b0268be85cd8e29a14a8d92"
HOLD_IMAGE = "sha256:bbb170b5a8eed05a179db672e7528b222e8c93c433ffdf79605fd9e8045d57ef"


class AcceptanceFailure(RuntimeError):
    pass


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def docker_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    result = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR")
        if key in os.environ
    }
    result["DOCKER_CLI_HINTS"] = "false"
    if extra:
        result.update(extra)
    return result


def run(
    command: list[str],
    *,
    environment: dict[str, str],
    secrets_to_hide: tuple[str, ...],
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    combined = completed.stdout + completed.stderr
    if any(secret and secret in combined for secret in secrets_to_hide):
        raise AcceptanceFailure("synthetic credential appeared in command output")
    if check and completed.returncode != 0:
        safe = "\n".join(combined.splitlines()[:40])
        raise AcceptanceFailure(
            f"command failed ({completed.returncode}): {command[0]}\n{safe}"
        )
    return completed


def inventory(environment: dict[str, str]) -> dict[str, tuple[str, ...]]:
    commands = {
        "containers": ["docker", "container", "ls", "-aq", "--no-trunc"],
        "networks": ["docker", "network", "ls", "-q", "--no-trunc"],
        "volumes": ["docker", "volume", "ls", "-q"],
    }
    return {
        name: tuple(sorted(run(command, environment=environment, secrets_to_hide=()).stdout.split()))
        for name, command in commands.items()
    }


def inventory_evidence(value: dict[str, tuple[str, ...]]) -> dict[str, object]:
    return {
        "counts": {key: len(items) for key, items in value.items()},
        "fingerprint": digest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ),
    }


SEED_PROGRAM = r'''
import json, os, sys
from minio import Minio
sys.path.insert(0, "/app")
from database import PostgresRepository
from domain import Actor, IngestionService, OCRDomainService, OCRRecoveryService, ReviewService
from retention_enforcement import ExactVersionRetentionEnforcer, HttpLegalHoldMediator, MinioExactVersionRetentionStorage
from storage import MinioObjectStorage
from validation import UploadValidationError

repo = PostgresRepository(os.environ["DATABASE_URL"])
ocr_repo = PostgresRepository(os.environ["OCR_DATABASE_URL"])
review_repo = PostgresRepository(os.environ["REVIEW_DATABASE_URL"])
actor = Actor("usr_restore", "Synthetic Restore Reviewer")
repo.ensure_local_user(actor.user_id, actor.display_name, "restore@example.invalid")
client = Minio(os.environ["MINIO_ENDPOINT"], access_key=os.environ["MINIO_ACCESS_KEY"], secret_key=os.environ["MINIO_SECRET_KEY"], secure=False)
ingestion = IngestionService(repo, MinioObjectStorage(client), 52428800, ExactVersionRetentionEnforcer(MinioExactVersionRetentionStorage(client), HttpLegalHoldMediator(os.environ["LEGAL_HOLD_APPLIER_URL"], os.environ["LEGAL_HOLD_APPLIER_CALL_TOKEN"])))
ocr = OCRDomainService(ocr_repo)
review = ReviewService(review_repo, True)

def uploaded(name, body):
    return ingestion.ingest(actor, name, body, "LAB_NOTE", "Synthetic destroyed-volume restore evidence.", None)
def completed(item, text):
    run_id = ocr.start(item["ingestion_id"], "paddleocr", "synthetic-restore-1", {"synthetic": True})
    return ocr.complete(item["ingestion_id"], run_id, text, [], json.dumps({"text": text}).encode(), "synthetic/ocr.json")

mode = os.environ.get("SEED_MODE", "source")
if mode == "post":
    item = uploaded("post-restore.png", b"\x89PNG\r\n\x1a\npost-restore-IEND\xaeB`\x82")
    draft = completed(item, "Post restore verified")
    result = review.review(draft["silver_draft_id"], actor, "Post restore verified", "APPROVED_NO_CHANGES", "", True)
    assert result and result["status"] == "VERIFIED"
    print(json.dumps({"post_restore_ingestion": item["ingestion_id"], "status": result["status"]}, sort_keys=True))
    raise SystemExit(0)

states = []
first_body = b"\x89PNG\r\n\x1a\nrestore-one-IEND\xaeB`\x82"
for index in range(4):
    item = uploaded(f"verified-{index}.png", first_body if index == 0 else b"\x89PNG\r\n\x1a\n" + str(index).encode() + b"-IEND\xaeB`\x82")
    draft = completed(item, f"Verified synthetic text {index}")
    result = review.review(draft["silver_draft_id"], actor, f"Verified synthetic text {index}", "APPROVED_NO_CHANGES", "", True)
    assert result and result["status"] == "VERIFIED"
    states.append(result["status"])
duplicate = uploaded("duplicate.png", first_body)
draft = completed(duplicate, "Duplicate rejected")
assert review.review(draft["silver_draft_id"], actor, "", "REJECTED_UNREADABLE", "Synthetic unreadable", True) is None
states.append("REVIEW_REJECTED")
failed = uploaded("ocr-failure.pdf", b"%PDF-1.4\nsynthetic malformed body\n%%EOF")
run_id = ocr.start(failed["ingestion_id"], "paddleocr", "synthetic-restore-1", {})
ocr_repo.mark_ocr_failed(failed["ingestion_id"], "synthetic first failure")
OCRRecoveryService(ocr_repo, 2).retry(failed["ingestion_id"], actor)
run_id = ocr.start(failed["ingestion_id"], "paddleocr", "synthetic-restore-1", {})
ocr_repo.mark_ocr_failed(failed["ingestion_id"], "synthetic terminal failure")
states.append("OCR_FAILED")
try:
    uploaded("unsupported.txt", b"synthetic unsupported")
except UploadValidationError:
    validation_rejected = True
else:
    validation_rejected = False
assert validation_rejected
print(json.dumps({"states": states, "validation_rejected": True}, sort_keys=True))
'''


OBJECT_PROGRAM = r'''
import hashlib, json, os, sys
from minio import Minio
sys.path.insert(0, "/app")
from database import PostgresRepository
from retention_enforcement import ExactVersionTarget, HttpLegalHoldMediator
repo = PostgresRepository(os.environ["DATABASE_URL"])
client = Minio(os.environ["MINIO_ENDPOINT"], access_key=os.environ["MINIO_ACCESS_KEY"], secret_key=os.environ["MINIO_SECRET_KEY"], secure=False)
mediator = HttpLegalHoldMediator(os.environ["LEGAL_HOLD_APPLIER_URL"], os.environ["LEGAL_HOLD_APPLIER_CALL_TOKEN"])
rows = []
with repo.connection() as connection:
    objects = connection.execute("SELECT bucket_name, object_key, object_kind, object_version_id, sha256, retention_mode, retain_until_utc FROM bronze_objects ORDER BY bucket_name, object_key").fetchall()
for item in objects:
    bucket, key, version = item["bucket_name"], item["object_key"], item["object_version_id"]
    object_kind = item["object_kind"]
    expected_sha, db_mode, db_until = item["sha256"], item["retention_mode"], item["retain_until_utc"]
    response = client.get_object(bucket, key, version_id=version)
    try: body = response.read()
    finally: response.close(); response.release_conn()
    retention = client.get_object_retention(bucket, key, version_id=version)
    hold = mediator.read_status(ExactVersionTarget(bucket, key, version, object_kind))
    rows.append({"bucket": bucket, "key": key, "version": version, "sha256": hashlib.sha256(body).hexdigest(), "expected_sha256": expected_sha, "db_mode": db_mode, "db_until": db_until.isoformat(), "storage_mode": str(retention.mode), "storage_until": retention.retain_until_date.isoformat(), "hold": hold})
assert rows and all(row["sha256"] == row["expected_sha256"] and "COMPLIANCE" in row["storage_mode"] and row["hold"] == "ON" for row in rows)
print(json.dumps(rows, sort_keys=True, separators=(",", ":")))
'''


SEMANTIC_PROGRAM = r'''
import json, os, sys
import psycopg
from psycopg import sql
sys.path.insert(0, "/opt/smartcoat-postgres")
from provision_runtime_roles import validate_installed_contract

result = {}
with psycopg.connect(os.environ["POSTGRES_ROLE_ADMIN_URL"]) as connection:
    validate_installed_contract(connection)
    result["rbac_exact_contract"] = True
    tables = [row[0] for row in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
    ).fetchall()]
    table_evidence = {}
    for table in tables:
        query = sql.SQL("SELECT count(*), coalesce(md5(string_agg(row_hash, '' ORDER BY row_hash)), md5('')) FROM (SELECT md5(t.*::text) AS row_hash FROM {} AS t) AS rows").format(sql.Identifier("public", table))
        count, row_hash = connection.execute(query).fetchone()
        table_evidence[table] = {"count": count, "row_hash": row_hash}
    result["public_tables"] = table_evidence

    sequences = {}
    sequence_rows = connection.execute(
        "SELECT sequence_schema, sequence_name FROM information_schema.sequences WHERE sequence_schema NOT IN ('pg_catalog','information_schema') ORDER BY sequence_schema, sequence_name"
    ).fetchall()
    for schema, name in sequence_rows:
        value, called = connection.execute(
            sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(schema, name))
        ).fetchone()
        sequences[f"{schema}.{name}"] = {"last_value": value, "is_called": called}
    result["sequences"] = sequences

    result["roles"] = [list(row) for row in connection.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (["smartcoat_ingestion", "smartcoat_ocr", "smartcoat_review", "smartcoat_backup"],),
    ).fetchall()]
    result["table_grants"] = [list(row) for row in connection.execute(
        "SELECT grantee, table_schema, table_name, privilege_type FROM information_schema.table_privileges WHERE grantee = ANY(%s) ORDER BY grantee, table_schema, table_name, privilege_type",
        (["smartcoat_ingestion", "smartcoat_ocr", "smartcoat_review", "smartcoat_backup"],),
    ).fetchall()]
    result["column_grants"] = [list(row) for row in connection.execute(
        "SELECT grantee, table_schema, table_name, column_name, privilege_type FROM information_schema.column_privileges WHERE grantee = ANY(%s) ORDER BY grantee, table_schema, table_name, column_name, privilege_type",
        (["smartcoat_ingestion", "smartcoat_ocr", "smartcoat_review", "smartcoat_backup"],),
    ).fetchall()]
    result["schema_grants"] = [list(row) for row in connection.execute(
        "SELECT grantee, object_schema, privilege_type FROM information_schema.usage_privileges WHERE object_type='SCHEMA' AND grantee = ANY(%s) ORDER BY grantee, object_schema, privilege_type",
        (["smartcoat_ingestion", "smartcoat_ocr", "smartcoat_review", "smartcoat_backup"],),
    ).fetchall()]
    result["triggers"] = [list(row) for row in connection.execute(
        "SELECT n.nspname, c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname IN ('public','smartcoat_migrations','smartcoat_state') ORDER BY n.nspname,c.relname,t.tgname"
    ).fetchall()]
    ledger = connection.execute(
        "SELECT version, name, sha256, count(*) OVER (PARTITION BY version) FROM smartcoat_migrations.applied_migrations ORDER BY version"
    ).fetchall()
    result["migration_ledger"] = [list(row) for row in ledger]
    result["ledger_0001_0010_once"] = [row[0] for row in ledger] == list(range(1, 11)) and all(row[3] == 1 for row in ledger)
    legal = connection.execute("SELECT count(*) FROM smartcoat_state.legal_upload_transitions").fetchone()[0]
    result["transition_graph"] = {"legal": legal, "illegal": 90 - legal, "total": 90}

append_targets = [
    ("public", "bronze_objects"), ("public", "silver_verified_records"),
    ("public", "review_decisions"), ("public", "audit_events"),
    ("public", "canonical_retention_classes"), ("public", "retention_policy_versions"),
    ("public", "retention_category_rules"), ("public", "bronze_retention_assignments"),
    ("public", "bronze_retention_enforcement_evidence"), ("public", "bronze_pairs"),
    ("smartcoat_migrations", "applied_migrations"),
    ("smartcoat_migrations", "adoption_decisions"),
    ("smartcoat_state", "legal_upload_transitions"),
]
enforcement = {}
for schema, table in append_targets:
    outcomes = {}
    for action in ("UPDATE", "DELETE"):
        with psycopg.connect(os.environ["POSTGRES_ROLE_ADMIN_URL"]) as probe:
            exists = probe.execute(
                sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(sql.Identifier(schema, table))
            ).fetchone()[0]
            if not exists:
                outcomes[action.lower()] = "NO_ROW_CATALOG_GUARD_ONLY"
                continue
            statement = (
                sql.SQL("UPDATE {} SET {} = {} WHERE ctid=(SELECT ctid FROM {} LIMIT 1)").format(
                    sql.Identifier(schema, table), sql.Identifier(probe.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(schema, table))).description[0].name), sql.Identifier(probe.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(schema, table))).description[0].name), sql.Identifier(schema, table)
                ) if action == "UPDATE" else
                sql.SQL("DELETE FROM {} WHERE ctid=(SELECT ctid FROM {} LIMIT 1)").format(sql.Identifier(schema, table), sql.Identifier(schema, table))
            )
            try:
                probe.execute(statement)
            except Exception:
                probe.rollback()
                outcomes[action.lower()] = "REJECTED"
            else:
                probe.rollback()
                outcomes[action.lower()] = "NOT_REJECTED"
    enforcement[f"{schema}.{table}"] = outcomes
result["append_only_enforcement"] = enforcement
assert result["ledger_0001_0010_once"]
assert result["transition_graph"] == {"legal": 11, "illegal": 79, "total": 90}
assert all(value in {"REJECTED", "NO_ROW_CATALOG_GUARD_ONLY"} for outcomes in enforcement.values() for value in outcomes.values())
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''


def preserve_failure_diagnostics(
    *,
    project: str,
    evidence: dict[str, object],
    source_dump: str | None,
    restored_dump: str | None,
    secrets_to_hide: tuple[str, ...],
) -> Path:
    destination = Path.home() / "Desktop" / "smartcoat-wp9-diagnostics" / project
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    artifacts: dict[str, bytes] = {
        "evidence.json": json.dumps(evidence, sort_keys=True, indent=2).encode() + b"\n"
    }
    if source_dump is not None:
        artifacts["source-data.sql"] = source_dump.encode()
    if restored_dump is not None:
        artifacts["restored-data.sql"] = restored_dump.encode()
    if source_dump is not None and restored_dump is not None:
        artifacts["postgres-data.diff"] = "".join(difflib.unified_diff(
            source_dump.splitlines(keepends=True),
            restored_dump.splitlines(keepends=True),
            fromfile="source-data.sql",
            tofile="restored-data.sql",
        )).encode()
    for name, content in artifacts.items():
        if any(secret and secret.encode() in content for secret in secrets_to_hide):
            raise AcceptanceFailure("diagnostic artifact contains synthetic credential")
        path = destination / name
        path.write_bytes(content)
        path.chmod(0o600)
    manifest = "".join(
        f"{digest((destination / name).read_bytes())}  {name}\n"
        for name in sorted(artifacts)
    )
    (destination / "SHA256SUMS").write_text(manifest)
    (destination / "SHA256SUMS").chmod(0o600)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(CONFIRM, action="store_true", dest="confirmed")
    args = parser.parse_args()
    if not args.confirmed:
        print("BLOCKED_ISOLATION: explicit authorization required")
        return 2

    docker_env = docker_environment()
    before = inventory(docker_env)
    evidence: dict[str, object] = {"inventory_before": inventory_evidence(before)}
    root = Path(tempfile.mkdtemp(prefix="smartcoat-wp8-dr-"))
    project = f"sc-wp8-{secrets.token_hex(5)}"
    postgres_data = root / "postgres"
    minio_data = root / "minio"
    backup = root / "backup"
    for path in (postgres_data, minio_data):
        path.mkdir(mode=0o700)
    (root / ".smartcoat-disaster-recovery-owned").write_text("synthetic-only\n")

    values = {name: secrets.token_urlsafe(32) for name in (
        "POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "POSTGRES_INGESTION_PASSWORD",
        "POSTGRES_OCR_PASSWORD", "POSTGRES_REVIEW_PASSWORD", "POSTGRES_BACKUP_PASSWORD",
        "MINIO_ROOT_PASSWORD", "MINIO_APP_SECRET_KEY", "MINIO_OCR_SECRET_KEY",
        "MINIO_BACKUP_SECRET_KEY", "MINIO_HOLD_APPLIER_SECRET_KEY",
        "LEGAL_HOLD_APPLIER_CALL_TOKEN", "SESSION_SECRET", "LOCAL_USER_PASSWORD",
    )}
    values.update({
        "POSTGRES_DB": "smartcoat_wp8", "POSTGRES_USER": "smartcoat_admin",
        "POSTGRES_APP_USER": "smartcoat_app", "MINIO_ROOT_USER": "wp8-root",
        "MINIO_APP_ACCESS_KEY": "wp8-app", "MINIO_OCR_ACCESS_KEY": "wp8-ocr",
        "MINIO_BACKUP_ACCESS_KEY": "wp8-backup", "MINIO_HOLD_APPLIER_ACCESS_KEY": "wp8-hold",
        "POSTGRES_DATA_DIR": str(postgres_data), "MINIO_DATA_DIR": str(minio_data),
        "DISASTER_RECOVERY_SCOPE_ROOT": str(root), "COMPOSE_PROJECT_NAME": project,
        "LOCAL_USER_ID": "usr_restore", "LOCAL_USER_DISPLAY_NAME": "Synthetic Restore Reviewer",
        "LOCAL_USER_EMAIL": "restore@example.invalid", "ALLOW_PHASE_1_SOLO_SELF_REVIEW": "true",
    })
    values["MIGRATION_DATABASE_URL"] = f"postgresql://smartcoat_admin:{values['POSTGRES_PASSWORD']}@postgres:5432/smartcoat_wp8"
    values["POSTGRES_ROLE_ADMIN_URL"] = values["MIGRATION_DATABASE_URL"]
    values["DATABASE_INGESTION_URL"] = f"postgresql://smartcoat_ingestion:{values['POSTGRES_INGESTION_PASSWORD']}@postgres:5432/smartcoat_wp8"
    values["DATABASE_OCR_URL"] = f"postgresql://smartcoat_ocr:{values['POSTGRES_OCR_PASSWORD']}@postgres:5432/smartcoat_wp8"
    values["DATABASE_REVIEW_URL"] = f"postgresql://smartcoat_review:{values['POSTGRES_REVIEW_PASSWORD']}@postgres:5432/smartcoat_wp8"
    env_file = root / "synthetic.env"
    env_file.write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    )
    env_file.chmod(0o600)
    override = root / "compose.override.yaml"
    override.write_text(f'''services:
  postgres:
    image: {POSTGRES_IMAGE}
    ports: !reset []
  minio:
    image: {MINIO_IMAGE}
    ports: !reset []
  minio-bootstrap:
    image: {MC_IMAGE}
  postgres-migrate:
    image: {API_IMAGE}
  postgres-role-provision:
    image: {API_IMAGE}
  legal-hold-applier:
    image: {HOLD_IMAGE}
  api:
    image: {API_IMAGE}
    ports: !reset []
  web:
    profiles: [disabled]
  ocr-worker:
    profiles: [disabled]
networks:
  backend:
    name: {project}-backend
  edge:
    name: {project}-edge
''')
    seed = root / "seed.py"
    seed.write_text(SEED_PROGRAM)
    object_check = root / "objects.py"
    object_check.write_text(OBJECT_PROGRAM)
    semantic_check = root / "semantic.py"
    semantic_check.write_text(SEMANTIC_PROGRAM)
    compose_file = f"{ROOT / 'compose.yaml'}:{override}"
    environment = docker_environment({
        **values, "ENV_FILE": str(env_file), "COMPOSE_FILE": compose_file,
        "SMARTCOAT_DISASTER_RECOVERY_CONFIRM": "DESTROYED_FRESH_VOLUMES_SYNTHETIC_ONLY",
    })
    hidden = tuple(values[name] for name in values if "PASSWORD" in name or "SECRET" in name or "TOKEN" in name or name.endswith("_URL"))

    def compose(*parts: str, check: bool = True, timeout: int = 300):
        return run(["docker", "compose", "--project-name", project, *parts], environment=environment, secrets_to_hide=hidden, check=check, timeout=timeout)
    def compose_quiet(*parts: str, timeout: int = 300) -> None:
        completed = subprocess.run(
            ["docker", "compose", "--project-name", project, *parts],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise AcceptanceFailure(
                f"silent Docker Compose operation failed: {parts[0]}"
            )
    def one_shot(program: Path, mode: str = "source"):
        return compose("run", "--rm", "--no-deps", "--entrypoint", "python", "-e", f"SEED_MODE={mode}", "-v", f"{program}:/tmp/program.py:ro", "api", "/tmp/program.py", timeout=300)
    def semantic_snapshot():
        return compose(
            "run", "--rm", "--no-deps", "--entrypoint", "python",
            "-v", f"{semantic_check}:/tmp/semantic.py:ro",
            "postgres-role-provision", "/tmp/semantic.py", timeout=300,
        ).stdout.splitlines()[-1]
    source_dump: str | None = None
    restored_dump: str | None = None
    failure: Exception | None = None
    try:
        compose("up", "-d", "--wait", "postgres", "minio", timeout=300)
        compose("run", "--rm", "postgres-migrate", "adopt", values["POSTGRES_DB"])
        compose("run", "--rm", "postgres-migrate", "apply")
        compose("run", "--rm", "postgres-role-provision")
        compose_quiet("run", "--rm", "minio-bootstrap")
        compose("up", "-d", "--wait", "legal-hold-applier", timeout=300)
        evidence["source_seed"] = json.loads(one_shot(seed).stdout.splitlines()[-1])
        source_objects = one_shot(object_check).stdout.splitlines()[-1]
        evidence["source_objects_sha256"] = digest(source_objects.encode())
        source_dump = compose("exec", "-T", "postgres", "pg_dump", "--data-only", "--inserts", "--no-owner", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"]).stdout
        evidence["source_data_sha256"] = digest(source_dump.encode())
        source_semantics = semantic_snapshot()
        evidence["source_semantics_sha256"] = digest(source_semantics.encode())

        backup_start = time.monotonic()
        result = run([str(SCRIPT), "backup", str(backup)], environment=environment, secrets_to_hide=hidden, timeout=300)
        evidence["backup"] = {"marker": result.stdout.strip(), "seconds": round(time.monotonic() - backup_start, 3), "postgres_sha256": digest((backup / "postgres.dump").read_bytes()), "manifest_sha256": digest((backup / "SHA256SUMS").read_bytes())}

        compose("down", "--remove-orphans", timeout=300)
        shutil.rmtree(postgres_data)
        shutil.rmtree(minio_data)
        postgres_data.mkdir(mode=0o700)
        minio_data.mkdir(mode=0o700)
        evidence["destruction"] = {"postgres_empty": not any(postgres_data.iterdir()), "minio_empty": not any(minio_data.iterdir())}

        restore_start = time.monotonic()
        restored = run([str(SCRIPT), "restore", str(backup)], environment=environment, secrets_to_hide=hidden, timeout=600)
        evidence["restore"] = {"marker": restored.stdout.strip(), "rto_seconds": round(time.monotonic() - restore_start, 3)}
        compose("up", "-d", "--wait", "legal-hold-applier", timeout=300)
        restored_objects = one_shot(object_check).stdout.splitlines()[-1]
        evidence["restored_objects_sha256"] = digest(restored_objects.encode())
        if restored_objects != source_objects:
            raise AcceptanceFailure("restored exact-version protection evidence differs")
        restored_dump = compose("exec", "-T", "postgres", "pg_dump", "--data-only", "--inserts", "--no-owner", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"]).stdout
        evidence["restored_data_sha256"] = digest(restored_dump.encode())
        restored_semantics = semantic_snapshot()
        evidence["restored_semantics_sha256"] = digest(restored_semantics.encode())
        evidence["semantic_database_equal"] = restored_semantics == source_semantics
        if restored_semantics != source_semantics:
            raise AcceptanceFailure("restored semantic database evidence differs")

        target = json.loads(restored_objects)[0]
        deletion = compose("run", "--rm", "--no-deps", "--entrypoint", "/bin/sh", "-e", f"DELETE_BUCKET={target['bucket']}", "-e", f"DELETE_KEY={target['key']}", "-e", f"DELETE_VERSION={target['version']}", "minio-bootstrap", "-c", "mc alias set restore http://minio:9000 \"$MINIO_ROOT_USER\" \"$MINIO_ROOT_PASSWORD\" >/dev/null && mc rm --version-id \"$DELETE_VERSION\" \"restore/$DELETE_BUCKET/$DELETE_KEY\"", check=False)
        evidence["protected_delete"] = {"exit": deletion.returncode, "denied": deletion.returncode != 0}
        if deletion.returncode == 0:
            raise AcceptanceFailure("restored protected exact version was deletable")

        graph = compose("exec", "-T", "postgres", "psql", "-At", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"], "-c", "SELECT count(*) FROM smartcoat_state.legal_upload_transitions").stdout.strip()
        evidence["transition_graph"] = {"legal": int(graph), "illegal": 90 - int(graph), "total": 90}
        append_only = compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", values["POSTGRES_USER"], "-d", values["POSTGRES_DB"], "-c", "UPDATE bronze_objects SET sha256 = sha256", check=False)
        evidence["append_only_rejected"] = append_only.returncode != 0
        if append_only.returncode == 0:
            raise AcceptanceFailure("append-only trigger did not reject mutation")
        idempotent = compose("run", "--rm", "postgres-migrate", "apply")
        evidence["migration_idempotency"] = "applied_now=0" in idempotent.stdout
        if not evidence["migration_idempotency"]:
            raise AcceptanceFailure("restored migration apply was not idempotent")
        evidence["post_restore"] = json.loads(one_shot(seed, "post").stdout.splitlines()[-1])
        evidence["rpo"] = "zero records at the quiesced backup boundary"
    except Exception as exc:
        failure = exc
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        preserved = preserve_failure_diagnostics(
            project=project,
            evidence=evidence,
            source_dump=source_dump,
            restored_dump=restored_dump,
            secrets_to_hide=hidden,
        )
        print(f"PRESERVED_DIAGNOSTICS={preserved}")
    finally:
        compose("down", "--remove-orphans", check=False, timeout=300)
        shutil.rmtree(root, ignore_errors=True)
    if failure is not None:
        raise failure
    after = inventory(docker_env)
    evidence["inventory_after"] = inventory_evidence(after)
    evidence["inventory_equal"] = after == before
    if after != before:
        raise AcceptanceFailure("Docker inventory was not restored")
    print(json.dumps(evidence, sort_keys=True, indent=2))
    print(PASS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
