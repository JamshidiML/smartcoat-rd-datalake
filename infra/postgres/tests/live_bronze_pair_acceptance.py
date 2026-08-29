#!/usr/bin/env python3
"""Disposable synthetic live acceptance for BRONZE_PAIR_READY.

The harness never reads the repository .env, never publishes a port, never
pulls or builds an image, accepts only explicit immutable candidate image IDs,
and labels every generated Docker resource for ownership-validated cleanup.
Five independent projects cover fresh and upgraded success, a protected-original
orphan, a protected-pair database failure, and lost orphan-evidence recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
CONFIRM_FLAG = "--confirm-disposable-synthetic-bronze-pair-run"
PASS = "PASS_BRONZE_PAIR_PRODUCTION_IMAGE"
PASS_LOST_EVIDENCE = "PASS_BRONZE_PAIR_LOST_EVIDENCE_RECOVERY"
PASS_OCR_EXACT_VERSION = "PASS_OCR_EXACT_BRONZE_VERSION_SOURCE"
FAIL = "FAIL_BRONZE_PAIR_READY"
BLOCKED = "BLOCKED_ISOLATION"
LABEL = "smartcoat.bronze-pair.project"
POSTGRES_IMAGE = "postgres:17.6-alpine"
MINIO_IMAGE = "minio/minio:RELEASE.2025-07-23T15-54-02Z"
MC_IMAGE = "minio/mc:RELEASE.2025-07-21T05-28-08Z"
LEGAL_HOLD_IMAGE = "smartcoat-rd-datalake-legal-hold-applier:latest"
MIGRATIONS = ROOT / "infra/postgres/migrations"
EXPECTED_MIGRATIONS = tuple(range(1, 9))


class AcceptanceFailure(RuntimeError):
    pass


class IsolationBlocked(AcceptanceFailure):
    pass


def run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 180,
    secrets_to_hide: set[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    clean = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR"}
    }
    if environment:
        clean.update(environment)
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=clean,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationBlocked(f"command boundary unavailable: {command[0]}") from exc
    combined = completed.stdout + completed.stderr
    if secrets_to_hide and any(secret and secret in combined for secret in secrets_to_hide):
        raise AcceptanceFailure("synthetic secret appeared in command output")
    if check and completed.returncode != 0:
        detail_lines = [
            line.strip()
            for line in (completed.stderr or completed.stdout).splitlines()
            if line.strip()
        ]
        detail = detail_lines[-1][:300] if detail_lines else "no diagnostic output"
        raise AcceptanceFailure(
            f"sanitized command failed: executable={command[0]} "
            f"exit={completed.returncode} detail={detail}"
        )
    return completed


def docker(
    *arguments: str,
    check: bool = True,
    timeout: int = 180,
    environment: dict[str, str] | None = None,
    secrets_to_hide: set[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["docker", *arguments],
        environment=environment,
        check=check,
        timeout=timeout,
        secrets_to_hide=secrets_to_hide,
    )


def image_id(reference: str) -> str:
    completed = docker("image", "inspect", reference, "--format", "{{.Id}}", check=False)
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value.startswith("sha256:") or len(value) != 71:
        raise IsolationBlocked(f"required immutable local image unavailable: {reference}")
    return value


def inventory() -> dict[str, list[str]]:
    return {
        "containers": sorted(filter(None, docker("container", "ls", "-aq").stdout.splitlines())),
        "networks": sorted(filter(None, docker("network", "ls", "-q").stdout.splitlines())),
        "volumes": sorted(filter(None, docker("volume", "ls", "-q").stdout.splitlines())),
    }


def inventory_evidence(value: dict[str, list[str]]) -> dict[str, Any]:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "counts": {kind: len(identifiers) for kind, identifiers in value.items()},
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def owned(project: str) -> dict[str, list[str]]:
    return {
        "containers": sorted(filter(None, docker(
            "container", "ls", "-aq", "--filter", f"label={LABEL}={project}"
        ).stdout.splitlines())),
        "networks": sorted(filter(None, docker(
            "network", "ls", "-q", "--filter", f"label={LABEL}={project}"
        ).stdout.splitlines())),
        "volumes": sorted(filter(None, docker(
            "volume", "ls", "-q", "--filter", f"label={LABEL}={project}"
        ).stdout.splitlines())),
    }


def cleanup(project: str) -> None:
    resources = owned(project)
    if resources["containers"]:
        docker("container", "rm", "-f", *resources["containers"], check=False)
    for network in owned(project)["networks"]:
        docker("network", "rm", network, check=False)
    for volume in owned(project)["volumes"]:
        docker("volume", "rm", volume, check=False)
    if any(owned(project).values()):
        raise IsolationBlocked("owned disposable resources survived cleanup")


def wait_for_postgres(container: str, admin: str, database: str) -> None:
    for _ in range(60):
        completed = docker(
            "exec", container, "pg_isready", "-U", admin, "-d", database,
            check=False,
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise IsolationBlocked("owned PostgreSQL did not become ready")


def mc_run(
    project: str,
    network: str,
    image: str,
    script: str,
    environment: dict[str, str],
    secrets_to_hide: set[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        "run", "--rm", "--pull=never", "--network", network,
        "--label", f"{LABEL}={project}",
        "--mount", f"type=bind,src={ROOT / 'infra/minio'},dst=/bootstrap,readonly",
    ]
    for name in environment:
        command.extend(["--env", name])
    command.extend(["--entrypoint", "/bin/sh", image, "-c", script])
    return docker(
        *command,
        environment=environment,
        secrets_to_hide=secrets_to_hide,
        check=check,
    )


def wait_for_minio(
    project: str,
    network: str,
    mc_image: str,
    root: dict[str, str],
    secrets_to_hide: set[str],
) -> None:
    script = (
        'mc alias set --quiet local http://minio:9000 "$ROOT_USER" '
        '"$ROOT_PASSWORD" >/dev/null 2>&1 && mc ready local >/dev/null 2>&1'
    )
    for _ in range(60):
        completed = mc_run(
            project, network, mc_image, script, root, secrets_to_hide, check=False
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise IsolationBlocked("owned MinIO did not become ready")


def psql(container: str, admin: str, database: str, sql: str, *, check: bool = True) -> str:
    completed = docker(
        "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At",
        "-U", admin, "-d", database, "-c", sql,
        check=check,
    )
    return completed.stdout.strip()


INGEST_PROGRAM = r'''
import json, os, sys
sys.path.insert(0, '/app')
from minio import Minio
from database import PostgresRepository
from domain import Actor, IngestionService
from retention_enforcement import (
    ExactVersionRetentionEnforcer, HttpLegalHoldMediator,
    MinioExactVersionRetentionStorage,
)
from storage import MinioObjectStorage

base_repository = PostgresRepository(os.environ['DATABASE_URL'])
mode = os.environ['MODE']
class Repository:
    def __init__(self, inner):
        self.inner = inner
    def __getattr__(self, name):
        return getattr(self.inner, name)
    def record_protected_orphans(self, *args, **kwargs):
        if mode == 'lost_orphan_evidence':
            raise RuntimeError('SYNTHETIC_INITIAL_ORPHAN_EVIDENCE_WRITE_FAILURE')
        return self.inner.record_protected_orphans(*args, **kwargs)
repository = Repository(base_repository)
repository.ensure_local_user('usr_bronze_live', 'Synthetic Bronze Live',
                             'bronze-live@example.invalid')
client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
base_storage = MinioObjectStorage(client)
class Storage:
    def __init__(self, inner):
        self.inner = inner
        self.puts = 0
    def put_once(self, *args, **kwargs):
        self.puts += 1
        if mode == 'manifest_upload_failure' and self.puts == 2:
            raise RuntimeError('SYNTHETIC_MANIFEST_UPLOAD_FAILURE')
        return self.inner.put_once(*args, **kwargs)
    def get_exact(self, *args, **kwargs):
        return self.inner.get_exact(*args, **kwargs)
storage = Storage(base_storage)
service = IngestionService(
    repository, storage, 1024 * 1024,
    ExactVersionRetentionEnforcer(
        MinioExactVersionRetentionStorage(client),
        HttpLegalHoldMediator('http://legal-hold-applier:8090',
                              os.environ['LEGAL_HOLD_APPLIER_CALL_TOKEN']),
    ),
)
try:
    result = service.ingest(
        Actor('usr_bronze_live', 'Synthetic Bronze Live'),
        'synthetic-live.jpg',
        b'\xff\xd8\xff\xe0synthetic-bronze-pair-live\xff\xd9',
        'LAB_NOTE', 'Synthetic disposable Bronze Pair live fixture.', None,
    )
except Exception as exc:
    print(json.dumps({'completed': False, 'error_type': type(exc).__name__},
                     sort_keys=True))
else:
    first = service.reconcile(result['ingestion_id'])
    second = service.reconcile(result['ingestion_id'])
    print(json.dumps({
        'completed': True,
        'ingestion_id': result['ingestion_id'],
        'ocr_job_id': result['ocr_job_id'],
        'first_retry_job_id': first['ocr_job_id'],
        'second_retry_job_id': second['ocr_job_id'],
        'retry_statuses': [first['status'], second['status']],
    }, sort_keys=True))
'''


RECONCILE_PROGRAM = r'''
import json, os, sys
sys.path.insert(0, '/app')
from minio import Minio
from database import PostgresRepository
from domain import IngestionService
from retention_enforcement import (
    ExactVersionRetentionEnforcer, HttpLegalHoldMediator,
    MinioExactVersionRetentionStorage,
)
from storage import MinioObjectStorage
client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
service = IngestionService(
    PostgresRepository(os.environ['DATABASE_URL']), MinioObjectStorage(client),
    1024 * 1024,
    ExactVersionRetentionEnforcer(
        MinioExactVersionRetentionStorage(client),
        HttpLegalHoldMediator('http://legal-hold-applier:8090',
                              os.environ['LEGAL_HOLD_APPLIER_CALL_TOKEN']),
    ),
)
first = service.reconcile(os.environ['INGESTION_ID'])
second = service.reconcile(os.environ['INGESTION_ID'])
print(json.dumps({
    'first_status': first['status'], 'second_status': second['status'],
    'first_job_id': first['ocr_job_id'], 'second_job_id': second['ocr_job_id'],
}, sort_keys=True))
'''


APP_DISCOVERY_PROGRAM = r'''
import hashlib, json, os, sys
sys.path.insert(0, '/app')
from minio import Minio
from storage import MinioObjectStorage
client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
storage = MinioObjectStorage(client)
rows = []
for target in json.loads(os.environ['TARGETS']):
    versions = storage.list_exact_versions(target['bucket'], target['key'])
    if len(versions) != 1:
        raise RuntimeError('EXACT_VERSION_DISCOVERY_NOT_UNIQUE')
    body = storage.get_exact(target['bucket'], target['key'], versions[0])
    rows.append({
        'bucket': target['bucket'], 'key': target['key'], 'kind': target['kind'],
        'version_id': versions[0], 'sha256': hashlib.sha256(body).hexdigest(),
    })
print(json.dumps(rows, sort_keys=True))
'''


OCR_EXACT_VERSION_PROGRAM = r'''
import ast, hashlib, json, os, pathlib, sys
sys.path.insert(0, '/worker')
from minio import Minio
from database import PostgresRepository
from domain import ORIGINALS_BUCKET
from storage import MinioObjectStorage

job = PostgresRepository(os.environ['DATABASE_URL']).claim_next_job()
if job is None:
    raise RuntimeError('QUEUED_JOB_NOT_FOUND')
version_id = str(job['original_object_version_id'])
if version_id != os.environ['EXPECTED_VERSION_ID']:
    raise RuntimeError('QUEUED_JOB_VERSION_MISMATCH')
client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
body = MinioObjectStorage(client).get_exact(
    ORIGINALS_BUCKET, str(job['stored_object_key']), version_id,
)
digest = hashlib.sha256(body).hexdigest()
if digest != os.environ['EXPECTED_SHA256']:
    raise RuntimeError('EXACT_VERSION_CONTENT_MISMATCH')

source = pathlib.Path('/worker/jobs/worker.py').read_text(encoding='utf-8')
tree = ast.parse(source)
version_assignment = False
exact_read = False
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == 'source_version_id'
        for target in node.targets
    ):
        version_assignment = 'original_object_version_id' in ast.unparse(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == 'get_exact' and len(node.args) >= 3:
            exact_read = ast.unparse(node.args[2]) == 'source_version_id'
if not version_assignment or not exact_read:
    raise RuntimeError('WORKER_EXACT_VERSION_CONTRACT_MISSING')
print(json.dumps({
    'ingestion_id': str(job['ingestion_id']),
    'original_object_version_id': version_id,
    'sha256': digest,
    'worker_version_assignment_verified': version_assignment,
    'worker_get_exact_verified': exact_read,
}, sort_keys=True))
'''


READBACK_PROGRAM = r'''
import hashlib, json, os
from minio import Minio
client = Minio('minio:9000', access_key=os.environ['ROOT_USER'],
               secret_key=os.environ['ROOT_PASSWORD'], secure=False)
targets = json.loads(os.environ['TARGETS'])
rows = []
for item in targets:
    response = client.get_object(item['bucket'], item['key'],
                                 version_id=item['version_id'])
    try: body = response.read()
    finally:
        response.close(); response.release_conn()
    retention = client.get_object_retention(
        item['bucket'], item['key'], version_id=item['version_id'])
    hold = client.is_object_legal_hold_enabled(
        item['bucket'], item['key'], version_id=item['version_id'])
    rows.append({
        'kind': item['kind'], 'version_id': item['version_id'],
        'sha256': hashlib.sha256(body).hexdigest(),
        'retention_mode': str(retention.mode).split('.')[-1].upper(),
        'retain_until_present': retention.retain_until_date is not None,
        'legal_hold': 'ON' if hold else 'OFF',
    })
print(json.dumps(rows, sort_keys=True))
'''


class Scenario:
    def __init__(self, name: str, image_ids: dict[str, str]) -> None:
        self.name = name
        self.project = f"bronzepair-{secrets.token_hex(6)}"
        self.network = f"{self.project}-backend"
        self.pg_volume = f"{self.project}-pg"
        self.minio_volume = f"{self.project}-minio"
        self.pg_container = f"{self.project}-postgres"
        self.minio_container = f"{self.project}-minio"
        self.hold_container = f"{self.project}-legal-hold-applier"
        self.database = f"bronze_pair_{secrets.token_hex(4)}"
        self.admin = f"admin_{secrets.token_hex(4)}"
        self.admin_password = secrets.token_hex(24)
        self.legacy_password = secrets.token_hex(24)
        self.runtime_passwords = {
            role: secrets.token_hex(24)
            for role in ("ingestion", "ocr", "review", "backup")
        }
        self.root = {
            "ROOT_USER": f"root{secrets.token_hex(8)}",
            "ROOT_PASSWORD": secrets.token_hex(24),
        }
        self.app = {
            "MINIO_APP_ACCESS_KEY": f"app{secrets.token_hex(6)}",
            "MINIO_APP_SECRET_KEY": secrets.token_hex(24),
        }
        self.ocr = {
            "MINIO_OCR_ACCESS_KEY": f"ocr{secrets.token_hex(6)}",
            "MINIO_OCR_SECRET_KEY": secrets.token_hex(24),
        }
        self.mediator = {
            "MINIO_HOLD_APPLIER_ACCESS_KEY": f"hold{secrets.token_hex(6)}",
            "MINIO_HOLD_APPLIER_SECRET_KEY": secrets.token_hex(24),
        }
        self.call_token = secrets.token_urlsafe(48)
        self.secrets = {
            self.admin_password, self.legacy_password, self.call_token,
            *self.runtime_passwords.values(), *self.root.values(),
            *self.app.values(), *self.ocr.values(), *self.mediator.values(),
        }
        self.images = image_ids
        self.initial_inventory = inventory()
        self.temp = Path(tempfile.mkdtemp(prefix=f"{self.project}-"))
        self.fixture = self.temp / "migrations"
        self.fixture.mkdir(mode=0o700)
        self.constructed = False
        self.evidence: dict[str, Any] = {
            "scenario": name,
            "project": self.project,
            "images": image_ids,
            "initial_inventory": inventory_evidence(self.initial_inventory),
        }

    @property
    def admin_url(self) -> str:
        return f"postgresql://{self.admin}:{self.admin_password}@postgres:5432/{self.database}"

    @property
    def ingestion_url(self) -> str:
        password = self.runtime_passwords["ingestion"]
        return f"postgresql://smartcoat_ingestion:{password}@postgres:5432/{self.database}"

    @property
    def ocr_url(self) -> str:
        password = self.runtime_passwords["ocr"]
        return f"postgresql://smartcoat_ocr:{password}@postgres:5432/{self.database}"

    def copy_migrations(self, versions: tuple[int, ...]) -> dict[str, str]:
        result: dict[str, str] = {}
        for version in versions:
            matches = list(MIGRATIONS.glob(f"{version:04d}__*.sql"))
            if len(matches) != 1:
                raise AcceptanceFailure(f"migration {version:04d} identity is ambiguous")
            destination = self.fixture / matches[0].name
            if not destination.exists():
                destination.write_bytes(matches[0].read_bytes())
                destination.chmod(0o400)
            result[destination.name] = hashlib.sha256(destination.read_bytes()).hexdigest()
        return result

    def run_api_python(
        self,
        program: str,
        environment: dict[str, str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "run", "--rm", "--pull=never", "--network", self.network,
            "--label", f"{LABEL}={self.project}",
        ]
        for name in environment:
            command.extend(["--env", name])
        command.extend(["--entrypoint", "python", self.images["api"], "-c", program])
        return docker(
            *command,
            environment=environment,
            secrets_to_hide=self.secrets,
            check=check,
        )

    def run_ocr_python(
        self,
        program: str,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "run", "--rm", "--pull=never", "--network", self.network,
            "--label", f"{LABEL}={self.project}",
        ]
        for name in environment:
            command.extend(["--env", name])
        command.extend(["--entrypoint", "python", self.images["ocr"], "-c", program])
        return docker(
            *command,
            environment=environment,
            secrets_to_hide=self.secrets,
        )

    def migrate(self, operation: str, *extra: str) -> dict[str, Any]:
        environment = {"MIGRATION_DATABASE_URL": self.admin_url}
        completed = docker(
            "run", "--rm", "--pull=never", "--network", self.network,
            "--label", f"{LABEL}={self.project}",
            "--env", "MIGRATION_DATABASE_URL",
            "--mount", f"type=bind,src={ROOT / 'infra/postgres'},dst=/infra,readonly",
            "--mount", f"type=bind,src={self.fixture},dst=/fixture,readonly",
            "--entrypoint", "python", self.images["api"],
            "/infra/migrate.py", "--migrations-dir", "/fixture", operation, *extra,
            environment=environment,
            secrets_to_hide=self.secrets,
        )
        output = completed.stdout.strip()
        if operation == "adopt":
            match = re.fullmatch(
                r"Adoption result: status=([A-Z_]+) database=([^ ]+) "
                r"oid=([0-9]+) evidence_inserted=(true|false)",
                output,
            )
            if match is None:
                raise AcceptanceFailure("adoption output shape was not recognized")
            return {
                "status": match.group(1),
                "database": match.group(2),
                "evidence_inserted": match.group(4) == "true",
                "adopted": match.group(1) in {"ADOPTED", "ALREADY_ADOPTED"},
            }
        match = re.fullmatch(
            r"Migration run complete: discovered=([0-9]+) "
            r"already_applied=([0-9]+) applied_now=([0-9]+)",
            output,
        )
        if match is None:
            raise AcceptanceFailure("migration output shape was not recognized")
        return {
            "discovered": int(match.group(1)),
            "already_applied": int(match.group(2)),
            "applied_now": int(match.group(3)),
        }

    def provision_roles(self) -> None:
        environment = {
            "POSTGRES_ROLE_ADMIN_URL": self.admin_url,
            **{
                f"POSTGRES_{role.upper()}_PASSWORD": value
                for role, value in self.runtime_passwords.items()
            },
        }
        command = [
            "run", "--rm", "--pull=never", "--network", self.network,
            "--label", f"{LABEL}={self.project}",
        ]
        for name in environment:
            command.extend(["--env", name])
        command.extend([
            "--mount", f"type=bind,src={ROOT / 'infra/postgres'},dst=/infra,readonly",
            "--entrypoint", "python", self.images["api"],
            "/infra/provision_runtime_roles.py",
        ])
        docker(
            *command,
            environment=environment,
            secrets_to_hide=self.secrets,
        )

    def construct(self, upgraded: bool) -> None:
        docker("network", "create", "--internal", "--label", f"{LABEL}={self.project}", self.network)
        self.constructed = True
        docker("volume", "create", "--label", f"{LABEL}={self.project}", self.pg_volume)
        docker("volume", "create", "--label", f"{LABEL}={self.project}", self.minio_volume)
        pg_environment = {
            "POSTGRES_DB": self.database,
            "POSTGRES_USER": self.admin,
            "POSTGRES_PASSWORD": self.admin_password,
            "POSTGRES_APP_PASSWORD": self.legacy_password,
        }
        docker(
            "run", "-d", "--pull=never", "--name", self.pg_container,
            "--label", f"{LABEL}={self.project}", "--network", self.network,
            "--network-alias", "postgres",
            *sum((["--env", name] for name in pg_environment), []),
            "--mount", f"type=volume,src={self.pg_volume},dst=/var/lib/postgresql/data",
            "--mount", f"type=bind,src={ROOT / 'infra/postgres/init.sql'},dst=/docker-entrypoint-initdb.d/001-init.sql,readonly",
            self.images["postgres"],
            environment=pg_environment,
            secrets_to_hide=self.secrets,
        )
        wait_for_postgres(self.pg_container, self.admin, self.database)

        self.copy_migrations((1,))
        adopted = self.migrate("adopt", self.database)
        if not adopted.get("adopted"):
            raise AcceptanceFailure("fresh synthetic bootstrap was not explicitly adopted")
        before_0008 = tuple(range(2, 8)) if upgraded else tuple(range(2, 9))
        migration_hashes = self.copy_migrations(before_0008)
        applied = self.migrate("apply")
        if upgraded:
            psql(
                self.pg_container, self.admin, self.database,
                "INSERT INTO users (user_id,display_name,email,role,active,created_at_utc) "
                "VALUES ('usr_upgrade_fixture','Synthetic Upgrade','upgrade@example.invalid','UPLOADER',true,now());"
                "INSERT INTO uploads (ingestion_id,department,uploader_user_id,uploader_display_name,uploaded_at_utc,"
                "original_filename,stored_object_key,manifest_object_key,detected_mime_type,declared_file_type,"
                "document_category,context_note,byte_size,source_sha256,source_channel,state) VALUES "
                "('00000000-0000-7000-8000-000000000801','RND','usr_upgrade_fixture','Synthetic Upgrade',now(),"
                "'legacy.jpg','rd/legacy/original.jpg','rd/legacy/manifest.json','image/jpeg','PHOTO','LAB_NOTE',"
                "'Synthetic upgraded-volume compatibility fixture.',42,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "'WEB_UPLOAD','RECEIVED');",
            )
            migration_hashes.update(self.copy_migrations((8,)))
            applied_0008 = self.migrate("apply")
            preserved = psql(
                self.pg_container, self.admin, self.database,
                "SELECT count(*)||':'||min(state) FROM uploads WHERE ingestion_id="
                "'00000000-0000-7000-8000-000000000801'",
            )
            if preserved != "1:RECEIVED":
                raise AcceptanceFailure("0008 changed the upgraded compatibility row")
            self.evidence["upgraded"] = {
                "pre_0008_apply": applied,
                "migration_0008_apply": applied_0008,
                "legacy_row_preserved": True,
            }
        self.provision_roles()
        self.evidence["migration_hashes"] = migration_hashes

        minio_environment = {
            "MINIO_ROOT_USER": self.root["ROOT_USER"],
            "MINIO_ROOT_PASSWORD": self.root["ROOT_PASSWORD"],
        }
        docker(
            "run", "-d", "--pull=never", "--name", self.minio_container,
            "--label", f"{LABEL}={self.project}", "--network", self.network,
            "--network-alias", "minio", "--env", "MINIO_ROOT_USER", "--env",
            "MINIO_ROOT_PASSWORD", "--mount",
            f"type=volume,src={self.minio_volume},dst=/data", self.images["minio"],
            "server", "/data", "--console-address", ":9001",
            environment=minio_environment,
            secrets_to_hide=self.secrets,
        )
        wait_for_minio(self.project, self.network, self.images["mc"], self.root, self.secrets)
        bootstrap_environment = {
            "MINIO_ROOT_USER": self.root["ROOT_USER"],
            "MINIO_ROOT_PASSWORD": self.root["ROOT_PASSWORD"],
            **self.app,
            **self.ocr,
            "MINIO_BACKUP_ACCESS_KEY": f"backup{secrets.token_hex(6)}",
            "MINIO_BACKUP_SECRET_KEY": secrets.token_hex(24),
            **self.mediator,
        }
        self.secrets.update(bootstrap_environment.values())
        mc_run(
            self.project, self.network, self.images["mc"],
            "/bin/sh /bootstrap/bootstrap.sh >/dev/null",
            bootstrap_environment, self.secrets,
        )
        hold_environment = {
            "MINIO_HOLD_APPLIER_ENDPOINT": "minio:9000",
            "MINIO_HOLD_APPLIER_SECURE": "false",
            **self.mediator,
            "LEGAL_HOLD_APPLIER_CALL_TOKEN": self.call_token,
            "LEGAL_HOLD_APPLIER_PORT": "8090",
        }
        docker(
            "run", "-d", "--pull=never", "--name", self.hold_container,
            "--label", f"{LABEL}={self.project}", "--network", self.network,
            "--network-alias", "legal-hold-applier",
            *sum((["--env", name] for name in hold_environment), []),
            self.images["legal_hold"],
            environment=hold_environment,
            secrets_to_hide=self.secrets,
        )
        health_program = (
            "import urllib.request; "
            "urllib.request.urlopen('http://legal-hold-applier:8090/healthz',timeout=2)"
        )
        for _ in range(30):
            completed = self.run_api_python(health_program, {}, check=False)
            if completed.returncode == 0:
                break
            time.sleep(1)
        else:
            raise IsolationBlocked("owned Legal Hold mediator did not become ready")

    def ingestion_environment(self, mode: str) -> dict[str, str]:
        return {
            "MODE": mode,
            "DATABASE_URL": self.ingestion_url,
            "MINIO_ACCESS_KEY": self.app["MINIO_APP_ACCESS_KEY"],
            "MINIO_SECRET_KEY": self.app["MINIO_APP_SECRET_KEY"],
            "LEGAL_HOLD_APPLIER_CALL_TOKEN": self.call_token,
        }

    def query_state(self) -> dict[str, Any]:
        encoded = psql(
            self.pg_container, self.admin, self.database,
            "SELECT json_build_object("
            "'uploads',count(DISTINCT u.ingestion_id),"
            "'states',coalesce(json_agg(DISTINCT u.state) FILTER (WHERE u.state IS NOT NULL),'[]'::json),"
            "'pairs',count(DISTINCT p.bronze_pair_id),"
            "'objects',count(DISTINCT b.bronze_object_id),"
            "'assignments',count(DISTINCT a.retention_assignment_id),"
            "'evidence',count(DISTINCT e.enforcement_evidence_id),"
            "'orphans',count(DISTINCT o.protected_orphan_id),"
            "'reconciliations',count(DISTINCT r.reconciliation_event_id),"
            "'jobs',count(DISTINCT j.ocr_job_id)) "
            "FROM uploads u LEFT JOIN bronze_pairs p ON p.ingestion_id=u.ingestion_id "
            "LEFT JOIN bronze_objects b ON b.ingestion_id=u.ingestion_id "
            "LEFT JOIN bronze_retention_assignments a ON a.bronze_object_id=b.bronze_object_id "
            "LEFT JOIN bronze_retention_enforcement_evidence e ON e.retention_assignment_id=a.retention_assignment_id "
            "LEFT JOIN bronze_protected_orphans o ON o.ingestion_id=u.ingestion_id "
            "LEFT JOIN bronze_reconciliation_events r ON r.ingestion_id=u.ingestion_id "
            "LEFT JOIN ocr_jobs j ON j.ingestion_id=u.ingestion_id "
            "WHERE u.uploader_user_id='usr_bronze_live'",
        )
        return json.loads(encoded)

    def exact_targets(self, orphan: bool) -> list[dict[str, str]]:
        table = "bronze_protected_orphans" if orphan else "bronze_objects"
        rows = psql(
            self.pg_container, self.admin, self.database,
            f"SELECT coalesce(json_agg(json_build_object('bucket',bucket_name,'key',object_key,"
            f"'kind',object_kind,'version_id',object_version_id,'sha256',sha256) ORDER BY object_kind),'[]'::json) FROM {table} "
            "WHERE ingestion_id IN (SELECT ingestion_id FROM uploads WHERE uploader_user_id='usr_bronze_live')",
        )
        return json.loads(rows)

    def deterministic_targets(self) -> list[dict[str, str]]:
        encoded = psql(
            self.pg_container, self.admin, self.database,
            "SELECT json_build_array("
            "json_build_object('bucket','sc-rd-bronze-originals','key',stored_object_key,'kind','ORIGINAL'),"
            "json_build_object('bucket','sc-rd-bronze-manifests','key',manifest_object_key,'kind','MANIFEST')) "
            "FROM uploads WHERE uploader_user_id='usr_bronze_live'",
        )
        return json.loads(encoded)

    def ordinary_app_discovery(self) -> list[dict[str, str]]:
        environment = {
            "MINIO_ACCESS_KEY": self.app["MINIO_APP_ACCESS_KEY"],
            "MINIO_SECRET_KEY": self.app["MINIO_APP_SECRET_KEY"],
            "TARGETS": json.dumps(self.deterministic_targets(), separators=(",", ":")),
        }
        return json.loads(
            self.run_api_python(APP_DISCOVERY_PROGRAM, environment).stdout
        )

    def storage_readback(self, targets: list[dict[str, str]]) -> list[dict[str, Any]]:
        environment = {**self.root, "TARGETS": json.dumps(targets, separators=(",", ":"))}
        return json.loads(self.run_api_python(READBACK_PROGRAM, environment).stdout)

    def verify_ocr_exact_version_source(self, targets: list[dict[str, str]]) -> None:
        originals = [item for item in targets if item["kind"] == "ORIGINAL"]
        if len(originals) != 1:
            raise AcceptanceFailure("OCR smoke requires exactly one committed original")
        original = originals[0]
        environment = {
            "DATABASE_URL": self.ocr_url,
            "MINIO_ACCESS_KEY": self.ocr["MINIO_OCR_ACCESS_KEY"],
            "MINIO_SECRET_KEY": self.ocr["MINIO_OCR_SECRET_KEY"],
            "EXPECTED_VERSION_ID": original["version_id"],
            "EXPECTED_SHA256": original["sha256"],
        }
        evidence = json.loads(
            self.run_ocr_python(OCR_EXACT_VERSION_PROGRAM, environment).stdout
        )
        if not (
            evidence["original_object_version_id"] == original["version_id"]
            and evidence["sha256"] == original["sha256"]
            and evidence["worker_version_assignment_verified"] is True
            and evidence["worker_get_exact_verified"] is True
        ):
            raise AcceptanceFailure("OCR production image did not consume the exact Bronze version")
        self.evidence["ocr_exact_version_source"] = evidence

    def verify_success(self, result: dict[str, Any]) -> None:
        state = self.query_state()
        if not (
            result.get("completed") is True
            and result["ocr_job_id"] == result["first_retry_job_id"] == result["second_retry_job_id"]
            and state["pairs"] == 1 and state["objects"] == 2
            and state["assignments"] == 2 and state["evidence"] == 2
            and state["jobs"] == 1 and state["states"] == ["OCR_QUEUED"]
        ):
            raise AcceptanceFailure("successful Bronze pair facts were not exact and idempotent")
        targets = self.exact_targets(False)
        readback = self.storage_readback(targets)
        if len(readback) != 2 or any(
            row["retention_mode"] != "COMPLIANCE"
            or row["legal_hold"] != "ON"
            or row["sha256"] != next(item["sha256"] for item in targets if item["kind"] == row["kind"])
            for row in readback
        ):
            raise AcceptanceFailure("exact-version storage protection readback failed")
        self.evidence.update({
            "ingestion": result,
            "database_state": state,
            "exact_targets": targets,
            "storage_readback": readback,
        })
        if self.name == "fresh_success":
            self.verify_ocr_exact_version_source(targets)

    def execute(self) -> None:
        upgraded = self.name == "upgraded_success"
        self.construct(upgraded)
        if self.name in {"fresh_success", "upgraded_success"}:
            result = json.loads(
                self.run_api_python(
                    INGEST_PROGRAM, self.ingestion_environment("success")
                ).stdout
            )
            self.verify_success(result)
            return
        if self.name == "manifest_failure":
            result = json.loads(
                self.run_api_python(
                    INGEST_PROGRAM,
                    self.ingestion_environment("manifest_upload_failure"),
                ).stdout
            )
            state = self.query_state()
            targets = self.exact_targets(True)
            readback = self.storage_readback(targets)
            if not (
                result.get("completed") is False and state["pairs"] == 0
                and state["jobs"] == 0 and state["orphans"] == 1
                and len(targets) == 1 and targets[0]["kind"] == "ORIGINAL"
                and readback[0]["legal_hold"] == "ON"
                and readback[0]["retention_mode"] == "COMPLIANCE"
            ):
                raise AcceptanceFailure("manifest fault did not preserve one protected orphan")
            self.evidence.update({
                "failure": result, "database_state": state,
                "protected_orphans": targets, "storage_readback": readback,
                "downstream_blocked": True,
            })
            return
        if self.name in {
            "transaction_failure_reconciliation", "lost_evidence_recovery",
        }:
            psql(
                self.pg_container, self.admin, self.database,
                "CREATE SCHEMA synthetic_bronze_fault;"
                "CREATE FUNCTION synthetic_bronze_fault.reject_pair() RETURNS trigger LANGUAGE plpgsql AS "
                "$f$ BEGIN RAISE EXCEPTION 'SYNTHETIC_BRONZE_PAIR_TRANSACTION_FAILURE'; END $f$;"
                "CREATE TRIGGER synthetic_bronze_pair_failure BEFORE INSERT ON bronze_pairs "
                "FOR EACH ROW EXECUTE FUNCTION synthetic_bronze_fault.reject_pair();",
            )
            result = json.loads(
                self.run_api_python(
                    INGEST_PROGRAM,
                    self.ingestion_environment(
                        "lost_orphan_evidence"
                        if self.name == "lost_evidence_recovery"
                        else "transaction_failure"
                    ),
                ).stdout
            )
            failed_state = self.query_state()
            targets = self.exact_targets(True)
            discovery: list[dict[str, str]] | None = None
            if self.name == "lost_evidence_recovery":
                if not (
                    result.get("completed") is False and failed_state["pairs"] == 0
                    and failed_state["objects"] == 0 and failed_state["jobs"] == 0
                    and failed_state["orphans"] == 0 and targets == []
                ):
                    raise AcceptanceFailure("initial orphan-evidence write did not fail closed")
                discovery = self.ordinary_app_discovery()
                if len(discovery) != 2 or {
                    item["kind"] for item in discovery
                } != {"ORIGINAL", "MANIFEST"}:
                    raise AcceptanceFailure(
                        "ordinary application identity did not rediscover both exact versions"
                    )
                failed_readback = self.storage_readback(discovery)
            else:
                failed_readback = self.storage_readback(targets)
                if not (
                    result.get("completed") is False and failed_state["pairs"] == 0
                    and failed_state["objects"] == 0 and failed_state["jobs"] == 0
                    and failed_state["orphans"] == 2 and len(targets) == 2
                    and all(row["legal_hold"] == "ON" for row in failed_readback)
                ):
                    raise AcceptanceFailure("database fault did not preserve two protected orphans")
            psql(
                self.pg_container, self.admin, self.database,
                "DROP TRIGGER synthetic_bronze_pair_failure ON bronze_pairs;"
                "DROP FUNCTION synthetic_bronze_fault.reject_pair();"
                "DROP SCHEMA synthetic_bronze_fault;",
            )
            ingestion_id = psql(
                self.pg_container, self.admin, self.database,
                "SELECT ingestion_id FROM uploads WHERE uploader_user_id='usr_bronze_live'",
            )
            environment = {
                **self.ingestion_environment("reconcile"),
                "INGESTION_ID": ingestion_id,
            }
            reconciliation = json.loads(
                self.run_api_python(RECONCILE_PROGRAM, environment).stdout
            )
            final_state = self.query_state()
            final_targets = self.exact_targets(False)
            expected_versions = {
                item["version_id"] for item in (discovery or targets)
            }
            if not (
                reconciliation["first_status"] == "RECONCILED"
                and reconciliation["second_status"] == "ALREADY_COMMITTED"
                and reconciliation["first_job_id"] == reconciliation["second_job_id"]
                and final_state["pairs"] == 1 and final_state["objects"] == 2
                and final_state["jobs"] == 1 and final_state["reconciliations"] == 1
                and {item["version_id"] for item in final_targets}
                == expected_versions
            ):
                raise AcceptanceFailure("protected-orphan reconciliation was not exact and idempotent")
            if self.name == "lost_evidence_recovery" and final_state["orphans"] != 2:
                raise AcceptanceFailure("rediscovered exact versions did not gain durable orphan evidence")
            self.evidence.update({
                "failure": result,
                "failure_database_state": failed_state,
                "protected_orphans": targets,
                "ordinary_application_exact_version_discovery": discovery,
                "failure_storage_readback": failed_readback,
                "reconciliation": reconciliation,
                "final_database_state": final_state,
                "final_exact_targets": final_targets,
                "downstream_blocked_until_reconciliation": True,
            })
            return
        raise AcceptanceFailure("unknown live scenario")

    def finalize(self) -> None:
        cleanup_error: Exception | None = None
        try:
            if self.constructed:
                cleanup(self.project)
        except Exception as exc:  # cleanup must override product success
            cleanup_error = exc
        final = inventory()
        self.evidence["final_inventory"] = inventory_evidence(final)
        self.evidence["inventory_equal"] = final == self.initial_inventory
        self.evidence["cleanup_remaining"] = owned(self.project)
        shutil.rmtree(self.temp, ignore_errors=True)
        if cleanup_error:
            raise cleanup_error
        if final != self.initial_inventory or any(self.evidence["cleanup_remaining"].values()):
            raise IsolationBlocked("cleanup did not restore the pre-existing Docker inventory")


def discover_migrations() -> tuple[int, ...]:
    values = tuple(
        int(path.name[:4])
        for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]__*.sql"))
    )
    if values != EXPECTED_MIGRATIONS:
        raise AcceptanceFailure("migration sequence is not exactly 0001 through 0008")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    parser.add_argument("--api-image-id")
    parser.add_argument("--ocr-image-id")
    args = parser.parse_args(argv)
    if not getattr(args, CONFIRM_FLAG[2:].replace("-", "_")):
        print(json.dumps({"authorized": False, "classification": BLOCKED}, sort_keys=True))
        print(BLOCKED)
        return 2
    if not args.api_image_id or not args.ocr_image_id:
        print(json.dumps({
            "authorized": True,
            "classification": BLOCKED,
            "reason": "explicit immutable API and OCR image IDs are required",
        }, sort_keys=True))
        print(BLOCKED)
        return 2
    scenarios: list[dict[str, Any]] = []
    try:
        migrations = discover_migrations()
        images = {
            "postgres": image_id(POSTGRES_IMAGE),
            "minio": image_id(MINIO_IMAGE),
            "mc": image_id(MC_IMAGE),
            "api": image_id(args.api_image_id),
            "ocr": image_id(args.ocr_image_id),
            "legal_hold": image_id(LEGAL_HOLD_IMAGE),
        }
        if images["api"] != args.api_image_id or images["ocr"] != args.ocr_image_id:
            raise IsolationBlocked("candidate images did not resolve to the explicit immutable IDs")
        for name in (
            "fresh_success",
            "upgraded_success",
            "manifest_failure",
            "transaction_failure_reconciliation",
            "lost_evidence_recovery",
        ):
            scenario = Scenario(name, images)
            try:
                scenario.execute()
            finally:
                scenario.finalize()
            scenarios.append(scenario.evidence)
    except IsolationBlocked as exc:
        print(json.dumps({
            "classification": BLOCKED, "reason": str(exc), "scenarios": scenarios,
        }, sort_keys=True))
        print(BLOCKED)
        return 2
    except Exception as exc:
        print(json.dumps({
            "classification": FAIL, "reason": str(exc), "scenarios": scenarios,
        }, sort_keys=True))
        print(FAIL)
        return 1
    print(json.dumps({
        "classification": PASS,
        "migration_sequence": list(migrations),
        "scenarios": scenarios,
    }, sort_keys=True))
    print(PASS)
    print(PASS_LOST_EVIDENCE)
    print(PASS_OCR_EXACT_VERSION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
