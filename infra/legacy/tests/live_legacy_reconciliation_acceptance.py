#!/usr/bin/env python3
"""Disposable synthetic live acceptance for legacy Bronze reconciliation.

The harness is explicitly opt-in, never reads the repository .env, uses only
already-local immutable images, publishes no ports, labels every generated
resource, and restores the exact pre-existing Docker inventory.
"""

from __future__ import annotations

import argparse
import atexit
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
CONFIRM_FLAG = "--confirm-disposable-synthetic-legacy-reconciliation-run"
PASS = "PASS_LEGACY_RECONCILED_OR_QUARANTINED"
FAIL = "FAIL_LEGACY_RECONCILIATION_ACCEPTANCE"
BLOCKED = "BLOCKED_ISOLATION"
LABEL = "smartcoat.legacy-reconciliation.project"
POSTGRES_REF = "postgres:17.6-alpine"
MINIO_REF = "minio/minio:RELEASE.2025-07-23T15-54-02Z"
MC_REF = "minio/mc:RELEASE.2025-07-21T05-28-08Z"
API_REF = "smartcoat-rd-datalake-api:latest"
MEDIATOR_REF = "smartcoat-rd-datalake-legal-hold-applier:latest"
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
MIGRATIONS = ROOT / "infra/postgres/migrations"
EXPECTED_MIGRATIONS = tuple(range(1, 10))
INTERRUPTION_MARKER = "SYNTHETIC_LEGACY_RECONCILIATION_PERSIST_INTERRUPTION"


class AcceptanceFailure(RuntimeError):
    pass


class IsolationBlocked(AcceptanceFailure):
    pass


def run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 240,
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
        raise AcceptanceFailure("synthetic secret escaped into command output")
    if check and completed.returncode != 0:
        diagnostic_lines = [
            line.strip() for line in (completed.stderr or completed.stdout).splitlines()
            if line.strip()
        ]
        diagnostic = diagnostic_lines[-1][:240] if diagnostic_lines else "no output"
        shape = " ".join(command[:5])
        raise AcceptanceFailure(
            f"sanitized command failed: shape={shape} exit={completed.returncode} "
            f"diagnostic={diagnostic}"
        )
    return completed


def docker(
    *arguments: str,
    check: bool = True,
    timeout: int = 240,
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
    if completed.returncode != 0 or not IMAGE_ID.fullmatch(value):
        raise IsolationBlocked(f"required already-local image unavailable: {reference}")
    return value


def inventory() -> dict[str, list[str]]:
    return {
        "containers": sorted(filter(None, docker("container", "ls", "-aq").stdout.splitlines())),
        "networks": sorted(filter(None, docker("network", "ls", "-q").stdout.splitlines())),
        "volumes": sorted(filter(None, docker("volume", "ls", "-q").stdout.splitlines())),
    }


def inventory_evidence(value: dict[str, list[str]]) -> dict[str, Any]:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "counts": {name: len(items) for name, items in value.items()},
        "sha256": hashlib.sha256(canonical).hexdigest(),
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


def psql(container: str, admin: str, database: str, sql: str, *, check: bool = True) -> str:
    completed = docker(
        "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At",
        "-U", admin, "-d", database, "-c", sql,
        check=check,
    )
    return completed.stdout.strip()


def mc_run(
    project: str,
    network: str,
    image: str,
    script: str,
    environment: dict[str, str],
    secrets_to_hide: set[str],
    *,
    mount_control: bool = True,
    fixture_directory: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        "run", "--rm", "--pull=never", "--network", network,
        "--label", f"{LABEL}={project}",
    ]
    for name in environment:
        command.extend(["--env", name])
    if mount_control:
        command.extend([
            "--mount", f"type=bind,src={ROOT / 'infra/minio'},dst=/control,readonly"
        ])
    if fixture_directory is not None:
        command.extend([
            "--mount", f"type=bind,src={fixture_directory},dst=/fixtures,readonly"
        ])
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
    root_environment: dict[str, str],
    secrets_to_hide: set[str],
) -> None:
    script = (
        'mc alias set --quiet local http://minio:9000 "$ROOT_USER" '
        '"$ROOT_PASSWORD" >/dev/null 2>&1 && mc ready local >/dev/null 2>&1'
    )
    for _ in range(60):
        completed = mc_run(
            project, network, mc_image, script, root_environment,
            secrets_to_hide, mount_control=False, check=False,
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise IsolationBlocked("owned MinIO did not become ready")


FIXTURE_STORAGE_PROGRAM = r'''
import hashlib, io, json, os
from datetime import UTC, datetime
from minio import Minio
from minio.commonconfig import COMPLIANCE
from minio.retention import Retention

client = Minio('minio:9000', access_key=os.environ['ROOT_USER'],
               secret_key=os.environ['ROOT_PASSWORD'], secure=False)
results = []
def put(name, bucket, key, content, *, strong=False):
    body = content.encode()
    uploaded = client.put_object(bucket, key, io.BytesIO(body), len(body))
    if strong:
        client.set_object_retention(
            bucket, key, Retention(COMPLIANCE, datetime(2045, 1, 1, tzinfo=UTC)),
            version_id=uploaded.version_id,
        )
    value = {
        'name': name, 'bucket': bucket, 'key': key,
        'version_id': uploaded.version_id,
        'sha256': hashlib.sha256(body).hexdigest(),
    }
    results.append(value)
    return value

originals = 'sc-rd-bronze-originals'
manifests = 'sc-rd-bronze-manifests'
put('exact', originals, 'rd/legacy/exact/object.bin', 'exact-content')
put('manifest', manifests, 'rd/legacy/manifest/manifest.json', 'manifest-content')
put('null_single', originals, 'rd/legacy/null-single/object.bin', 'null-single-content')
put('null_unique_match', originals, 'rd/legacy/null-unique/object.bin', 'wanted-content')
put('null_unique_other', originals, 'rd/legacy/null-unique/object.bin', 'other-content')
put('ambiguous_a', originals, 'rd/legacy/ambiguous/object.bin', 'same-content')
put('ambiguous_b', originals, 'rd/legacy/ambiguous/object.bin', 'same-content')
put('mismatch', originals, 'rd/legacy/mismatch/object.bin', 'storage-content')
put('storage_orphan', originals, 'rd/legacy/storage-orphan/object.bin', 'orphan-content')
delete_target = put('delete_target', originals, 'rd/legacy/delete-marker/object.bin', 'delete-target')
client.remove_object(originals, delete_target['key'])
put('strong', originals, 'rd/legacy/strong/object.bin', 'strong-content', strong=True)
put('no_hold', originals, 'rd/legacy/no-hold/object.bin', 'no-hold-content')
put('invalid_target', originals, 'rd/legacy/enforcement-failure/object.bin', 'invalid-target')
put('interrupted', originals, 'rd/legacy/interrupted/object.bin', 'interrupted-content')
put('contradictory', originals, 'rd/legacy/contradictory/object.bin', 'contradictory-content')
print(json.dumps(results, sort_keys=True))
'''


FIXTURE_DATABASE_PROGRAM = r'''
import json, os, psycopg

objects = {item['name']: item for item in json.loads(os.environ['FIXTURES_JSON'])}
rows = [
    ('exact', objects['exact'], 'ORIGINAL', objects['exact']['sha256'], objects['exact']['version_id']),
    ('manifest', objects['manifest'], 'MANIFEST', objects['manifest']['sha256'], objects['manifest']['version_id']),
    ('null_single', objects['null_single'], 'ORIGINAL', objects['null_single']['sha256'], None),
    ('null_unique', objects['null_unique_match'], 'ORIGINAL', objects['null_unique_match']['sha256'], None),
    ('ambiguous', objects['ambiguous_a'], 'ORIGINAL', objects['ambiguous_a']['sha256'], None),
    ('mismatch', objects['mismatch'], 'ORIGINAL', 'f' * 64, objects['mismatch']['version_id']),
    ('database_orphan', {
        'bucket': 'sc-rd-bronze-originals', 'key': 'rd/legacy/database-orphan/object.bin',
    }, 'ORIGINAL', 'e' * 64, 'missing-version'),
    ('delete_marker', objects['delete_target'], 'ORIGINAL', objects['delete_target']['sha256'], objects['delete_target']['version_id']),
    ('strong', objects['strong'], 'ORIGINAL', objects['strong']['sha256'], objects['strong']['version_id']),
    ('no_hold', objects['no_hold'], 'ORIGINAL', objects['no_hold']['sha256'], objects['no_hold']['version_id']),
    ('invalid_target', objects['invalid_target'], 'ORIGINAL', objects['invalid_target']['sha256'], objects['invalid_target']['version_id']),
    ('interrupted', objects['interrupted'], 'ORIGINAL', objects['interrupted']['sha256'], objects['interrupted']['version_id']),
    ('contradictory', objects['contradictory'], 'MANIFEST', objects['contradictory']['sha256'], objects['contradictory']['version_id']),
]
with psycopg.connect(os.environ['DATABASE_URL']) as connection:
    connection.execute(
        "INSERT INTO users (user_id,display_name,email,role,active,created_at_utc) "
        "VALUES ('usr_legacy_synthetic','Synthetic Legacy Operator','legacy@example.invalid',"
        "'UPLOADER',true,now())"
    )
    for index, (name, item, kind, sha256, version_id) in enumerate(rows, 1):
        ingestion_id = f'10000000-0000-7000-8000-{index:012d}'
        bronze_id = f'20000000-0000-7000-8000-{index:012d}'
        stored = item['key'] if kind == 'ORIGINAL' else f'rd/legacy/db/{name}/original.bin'
        manifest = item['key'] if kind == 'MANIFEST' else f'rd/legacy/db/{name}/manifest.json'
        connection.execute(
            "INSERT INTO uploads (ingestion_id,department,uploader_user_id,uploader_display_name,"
            "uploaded_at_utc,original_filename,stored_object_key,manifest_object_key,"
            "detected_mime_type,declared_file_type,document_category,context_note,byte_size,"
            "source_sha256,source_channel,state) VALUES (%s,'RND','usr_legacy_synthetic',"
            "'Synthetic Legacy Operator',now(),%s,%s,%s,'application/octet-stream','PHOTO',"
            "'LAB_NOTE','Synthetic legacy reconciliation fixture.',1,%s,'WEB_UPLOAD','RECEIVED')",
            (ingestion_id, name + '.bin', stored, manifest, sha256),
        )
        connection.execute(
            "INSERT INTO bronze_objects (bronze_object_id,ingestion_id,bucket_name,object_key,"
            "object_kind,sha256,object_version_id,retention_mode,retain_until_utc,created_at_utc) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'COMPLIANCE',now()+interval '365 days',now())",
            (bronze_id, ingestion_id, item['bucket'], item['key'], kind, sha256, version_id),
        )
print(json.dumps({'database_rows': len(rows)}, sort_keys=True))
'''


SNAPSHOT_PROGRAM = r'''
import hashlib, json, os, urllib.request
from minio import Minio

client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
values = []
for bucket in ('sc-rd-bronze-originals', 'sc-rd-bronze-manifests'):
    for item in client.list_objects(bucket, recursive=True, include_version=True):
        marker = bool(getattr(item, 'is_delete_marker', False))
        value = {
            'bucket': bucket, 'key': item.object_name, 'version_id': item.version_id,
            'delete_marker': marker,
        }
        if not marker:
            retention = client.get_object_retention(bucket, item.object_name, version_id=item.version_id)
            payload = json.dumps({
                'bucket': bucket, 'object_key': item.object_name, 'version_id': item.version_id,
            }, separators=(',', ':')).encode()
            request = urllib.request.Request(
                'http://legal-hold-applier:8090/status', data=payload,
                headers={'Authorization': 'Bearer ' + os.environ['CALL_TOKEN'],
                         'Content-Type': 'application/json'}, method='POST',
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                hold = json.loads(response.read())['legal_hold']
            value.update({
                'retention_mode': str(retention.mode),
                'retain_until': retention.retain_until_date.isoformat(),
                'legal_hold': hold,
            })
        values.append(value)
canonical = json.dumps(sorted(values, key=lambda x: (x['bucket'], x['key'], x['version_id'])),
                       sort_keys=True, separators=(',', ':')).encode()
print(json.dumps({'entities': len(values), 'sha256': hashlib.sha256(canonical).hexdigest()},
                 sort_keys=True))
'''


VERIFY_PROGRAM = r'''
import hashlib, json, os, urllib.request, psycopg
from minio import Minio

client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
with psycopg.connect(os.environ['DATABASE_URL']) as connection:
    rows = connection.execute(
        "SELECT bucket_name,object_key,object_version_id,content_sha256,"
        "requested_retain_until_utc FROM legacy_reconciliation_successes ORDER BY bucket_name,object_key"
    ).fetchall()
checked = 0
for bucket, key, version_id, expected_sha, requested_until in rows:
    response = client.get_object(bucket, key, version_id=version_id)
    try:
        observed_sha = hashlib.sha256(response.read()).hexdigest()
    finally:
        response.close(); response.release_conn()
    retention = client.get_object_retention(bucket, key, version_id=version_id)
    payload = json.dumps({'bucket': bucket, 'object_key': key, 'version_id': version_id},
                         separators=(',', ':')).encode()
    request = urllib.request.Request(
        'http://legal-hold-applier:8090/status', data=payload,
        headers={'Authorization': 'Bearer ' + os.environ['CALL_TOKEN'],
                 'Content-Type': 'application/json'}, method='POST',
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        hold = json.loads(response.read())['legal_hold']
    if observed_sha != expected_sha or str(retention.mode).upper().split('.')[-1] != 'COMPLIANCE':
        raise SystemExit(10)
    if retention.retain_until_date < requested_until or hold != 'ON':
        raise SystemExit(11)
    checked += 1
print(json.dumps({'exact_successes_verified': checked, 'all_holds_on': True,
                  'all_compliance_floors_satisfied': True}, sort_keys=True))
'''


DENIAL_PROGRAM = r'''
import io, json, os
from minio import Minio
from minio.error import S3Error

client = Minio('minio:9000', access_key=os.environ['MINIO_ACCESS_KEY'],
               secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
bucket = 'sc-rd-bronze-originals'
key = 'rd/legacy/exact/object.bin'
version = os.environ['EXACT_VERSION_ID']
denied = []
operations = [
    ('put', lambda: client.put_object(bucket, 'rd/legacy/denied.bin', io.BytesIO(b'x'), 1)),
    ('delete', lambda: client.remove_object(bucket, key)),
    ('legal_hold', lambda: client.enable_object_legal_hold(bucket, key, version_id=version)),
]
for name, operation in operations:
    try:
        operation()
    except S3Error as exc:
        if exc.code not in {'AccessDenied', 'XMinioAdminInvalidArgument'}:
            raise
        denied.append(name)
    else:
        raise SystemExit(20)
print(json.dumps({'denied_operations': sorted(denied)}, sort_keys=True))
'''


class Harness:
    def __init__(self) -> None:
        token = secrets.token_hex(6)
        self.project = f"legacy-{token}"
        self.network = f"{self.project}-backend"
        self.pg_container = f"{self.project}-postgres"
        self.minio_container = f"{self.project}-minio"
        self.mediator_container = f"{self.project}-legal-hold"
        self.pg_volume = f"{self.project}-pg"
        self.minio_volume = f"{self.project}-minio"
        self.database = "smartcoat_legacy_synthetic"
        self.admin = "legacy_admin"
        self.admin_password = secrets.token_urlsafe(36)
        self.legacy_password = secrets.token_urlsafe(36)
        self.root_user = "legacy-root-" + secrets.token_hex(6)
        self.root_password = secrets.token_urlsafe(42)
        self.operator_user = "legacy-operator-" + secrets.token_hex(5)
        self.operator_password = secrets.token_urlsafe(42)
        self.mediator_user = "legacy-mediator-" + secrets.token_hex(5)
        self.mediator_password = secrets.token_urlsafe(42)
        self.call_token = secrets.token_urlsafe(48)
        self.secrets = {
            self.admin_password, self.legacy_password, self.root_user,
            self.root_password, self.operator_user, self.operator_password,
            self.mediator_user, self.mediator_password, self.call_token,
        }
        self.admin_url = (
            f"postgresql://{self.admin}:{self.admin_password}@postgres:5432/{self.database}"
        )
        self.secrets.add(self.admin_url)
        self.temp = Path(tempfile.mkdtemp(prefix=f"{self.project}-"))
        self.fixture = self.temp / "migrations"
        self.fixture.mkdir(mode=0o755)
        mediator_policy = json.loads(
            (ROOT / "infra/minio/policies/legal-hold-applier.json").read_text()
        )
        mediator_policy["Statement"].append({
            "Effect": "Deny",
            "Action": ["s3:PutObjectLegalHold"],
            "Resource": [
                "arn:aws:s3:::sc-rd-bronze-originals/rd/legacy/"
                "enforcement-failure/object.bin"
            ],
        })
        self.fault_mediator_policy = self.temp / "synthetic-mediator-policy.json"
        self.fault_mediator_policy.write_text(
            json.dumps(mediator_policy, sort_keys=True), encoding="utf-8"
        )
        self.fault_mediator_policy.chmod(0o400)
        self.initial_inventory: dict[str, list[str]] = {}
        self.images: dict[str, str] = {}
        self.evidence: dict[str, Any] = {"project": self.project, "synthetic_only": True}
        self.finalized = False

    def finalize(self) -> None:
        if self.finalized:
            return
        self.finalized = True
        cleanup_error: Exception | None = None
        try:
            cleanup(self.project)
        except Exception as exc:
            cleanup_error = exc
        final = inventory()
        self.evidence["final_inventory"] = inventory_evidence(final)
        self.evidence["owned_resources_after_cleanup"] = owned(self.project)
        self.evidence["inventory_restored"] = final == self.initial_inventory
        if final == self.initial_inventory and not any(owned(self.project).values()):
            shutil.rmtree(self.temp, ignore_errors=True)
        if cleanup_error:
            raise cleanup_error
        if final != self.initial_inventory or any(owned(self.project).values()):
            raise IsolationBlocked("cleanup did not restore exact Docker inventory")

    def container_python(
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

    def migrate(self, operation: str, *extra: str) -> dict[str, Any]:
        environment = {"MIGRATION_DATABASE_URL": self.admin_url}
        completed = docker(
            "run", "--rm", "--pull=never", "--network", self.network,
            "--label", f"{LABEL}={self.project}", "--env", "MIGRATION_DATABASE_URL",
            "--mount", f"type=bind,src={ROOT / 'infra/postgres'},dst=/infra,readonly",
            "--mount", f"type=bind,src={self.fixture},dst=/fixture,readonly",
            "--entrypoint", "python", self.images["api"], "/infra/migrate.py",
            "--migrations-dir", "/fixture", operation, *extra,
            environment=environment,
            secrets_to_hide=self.secrets,
        )
        if operation == "adopt":
            if "status=ADOPTED" not in completed.stdout:
                raise AcceptanceFailure("fresh bootstrap was not explicitly adopted")
            return {"adopted": True}
        match = re.search(
            r"discovered=([0-9]+) already_applied=([0-9]+) applied_now=([0-9]+)",
            completed.stdout,
        )
        if not match:
            raise AcceptanceFailure("migration result shape was not recognized")
        return {
            "discovered": int(match.group(1)),
            "already_applied": int(match.group(2)),
            "applied_now": int(match.group(3)),
        }

    def operator(self, mode: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = {
            "PYTHONPATH": "/workspace/apps/api/src",
            "LEGACY_RECONCILIATION_DATABASE_URL": self.admin_url,
            "MINIO_ENDPOINT": "minio:9000",
            "MINIO_SECURE": "false",
            "MINIO_LEGACY_RECONCILIATION_ACCESS_KEY": self.operator_user,
            "MINIO_LEGACY_RECONCILIATION_SECRET_KEY": self.operator_password,
            "LEGAL_HOLD_APPLIER_URL": "http://legal-hold-applier:8090",
            "LEGAL_HOLD_APPLIER_CALL_TOKEN": self.call_token,
        }
        command = [
            "run", "--rm", "--pull=never", "--network", self.network,
            "--label", f"{LABEL}={self.project}",
        ]
        for name in environment:
            command.extend(["--env", name])
        command.extend([
            "--mount", f"type=bind,src={ROOT / 'infra/legacy'},dst=/workspace/infra/legacy,readonly",
            "--mount", f"type=bind,src={ROOT / 'apps/api/src'},dst=/workspace/apps/api/src,readonly",
            "--entrypoint", "python", self.images["api"],
            "/workspace/infra/legacy/legacy_reconciliation.py", mode,
        ])
        if mode == "--apply":
            command.append("--confirm-legacy-365-day-reconciliation")
        return docker(
            *command,
            environment=environment,
            secrets_to_hide=self.secrets,
            check=check,
        )

    @staticmethod
    def parse_operator(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            raise AcceptanceFailure("operator emitted no structured evidence")
        return json.loads(lines[0])

    def snapshot(self) -> dict[str, Any]:
        environment = {
            "MINIO_ACCESS_KEY": self.operator_user,
            "MINIO_SECRET_KEY": self.operator_password,
            "CALL_TOKEN": self.call_token,
        }
        completed = self.container_python(SNAPSHOT_PROGRAM, environment)
        return json.loads(completed.stdout)

    def run_live(self) -> dict[str, Any]:
        discovered = tuple(
            int(path.name[:4])
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]__*.sql"))
        )
        if discovered != EXPECTED_MIGRATIONS:
            raise AcceptanceFailure("migration chain is not exactly 0001 through 0009")
        self.initial_inventory = inventory()
        self.evidence["initial_inventory"] = inventory_evidence(self.initial_inventory)
        self.images = {
            "postgres": image_id(POSTGRES_REF),
            "minio": image_id(MINIO_REF),
            "mc": image_id(MC_REF),
            "api": image_id(API_REF),
            "mediator": image_id(MEDIATOR_REF),
        }
        self.evidence["immutable_images"] = dict(self.images)

        # The finalizer is installed before the first state-changing Docker command.
        atexit.register(self.finalize)
        docker("network", "create", "--internal", "--label", f"{LABEL}={self.project}", self.network)
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

        shutil.copy2(MIGRATIONS / "0001__validate_bootstrap_prerequisites.sql", self.fixture)
        self.migrate("adopt", self.database)
        for version in range(2, 10):
            source = next(MIGRATIONS.glob(f"{version:04d}__*.sql"))
            shutil.copy2(source, self.fixture / source.name)
        migration_apply = self.migrate("apply")
        migration_repeat = self.migrate("apply")
        if migration_apply != {"discovered": 9, "already_applied": 1, "applied_now": 8}:
            raise AcceptanceFailure("0002 through 0009 did not apply exactly once")
        if migration_repeat["applied_now"] != 0:
            raise AcceptanceFailure("migration reapplication was not idempotent")
        self.evidence["migration"] = {
            "apply": migration_apply,
            "repeat": migration_repeat,
            "0009_sha256": hashlib.sha256(
                (MIGRATIONS / "0009__record_legacy_bronze_reconciliation.sql").read_bytes()
            ).hexdigest(),
        }

        minio_environment = {
            "MINIO_ROOT_USER": self.root_user,
            "MINIO_ROOT_PASSWORD": self.root_password,
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
        root_environment = {"ROOT_USER": self.root_user, "ROOT_PASSWORD": self.root_password}
        wait_for_minio(
            self.project, self.network, self.images["mc"], root_environment, self.secrets
        )
        provision_environment = {
            **root_environment,
            "OPERATOR_USER": self.operator_user,
            "OPERATOR_PASSWORD": self.operator_password,
            "MEDIATOR_USER": self.mediator_user,
            "MEDIATOR_PASSWORD": self.mediator_password,
        }
        provision_script = r'''
set -eu
mc alias set --quiet local http://minio:9000 "$ROOT_USER" "$ROOT_PASSWORD" >/dev/null 2>&1
mc mb --quiet --ignore-existing --with-lock local/sc-rd-bronze-originals
mc mb --quiet --ignore-existing --with-lock local/sc-rd-bronze-manifests
mc version enable --quiet local/sc-rd-bronze-originals
mc version enable --quiet local/sc-rd-bronze-manifests
mc retention set --quiet --default COMPLIANCE 365d local/sc-rd-bronze-originals
mc retention set --quiet --default COMPLIANCE 365d local/sc-rd-bronze-manifests
mc admin policy create local legacy-reconciliation /control/policies/legacy-reconciliation.json >/dev/null
mc admin policy create local synthetic-legal-hold-applier /fixtures/synthetic-mediator-policy.json >/dev/null
mc admin user add local "$OPERATOR_USER" "$OPERATOR_PASSWORD" >/dev/null
mc admin user add local "$MEDIATOR_USER" "$MEDIATOR_PASSWORD" >/dev/null
mc admin policy attach local legacy-reconciliation --user "$OPERATOR_USER" >/dev/null
mc admin policy attach local synthetic-legal-hold-applier --user "$MEDIATOR_USER" >/dev/null
'''
        mc_run(
            self.project, self.network, self.images["mc"], provision_script,
            provision_environment, self.secrets, fixture_directory=self.temp,
        )

        mediator_environment = {
            "MINIO_HOLD_APPLIER_ENDPOINT": "minio:9000",
            "MINIO_HOLD_APPLIER_SECURE": "false",
            "MINIO_HOLD_APPLIER_ACCESS_KEY": self.mediator_user,
            "MINIO_HOLD_APPLIER_SECRET_KEY": self.mediator_password,
            "LEGAL_HOLD_APPLIER_CALL_TOKEN": self.call_token,
            "LEGAL_HOLD_APPLIER_PORT": "8090",
        }
        docker(
            "run", "-d", "--pull=never", "--read-only", "--name", self.mediator_container,
            "--label", f"{LABEL}={self.project}", "--network", self.network,
            "--network-alias", "legal-hold-applier",
            *sum((["--env", name] for name in mediator_environment), []),
            self.images["mediator"],
            environment=mediator_environment,
            secrets_to_hide=self.secrets,
        )
        time.sleep(1)

        fixture_storage = self.container_python(FIXTURE_STORAGE_PROGRAM, root_environment)
        fixtures = json.loads(fixture_storage.stdout)
        fixture_by_name = {item["name"]: item for item in fixtures}
        database_environment = {
            "DATABASE_URL": self.admin_url,
            "FIXTURES_JSON": json.dumps(fixtures, sort_keys=True),
        }
        self.container_python(FIXTURE_DATABASE_PROGRAM, database_environment)
        self.evidence["fixtures"] = {
            "database_rows": 13,
            "storage_versions_including_delete_markers": 16,
            "cases": [
                "exact_version_and_hash", "manifest_exact_version", "null_single_match",
                "null_unique_hash_among_versions", "ambiguous_versions", "hash_mismatch",
                "storage_orphan", "database_orphan", "delete_marker_over_history",
                "stronger_compliance_floor", "permanent_without_hold",
                "enforcement_failure", "interrupted_retry", "repeat_idempotency",
                "contradictory_metadata",
            ],
        }

        pre_snapshot = self.snapshot()
        dry_first = self.parse_operator(self.operator("--dry-run"))
        dry_second = self.parse_operator(self.operator("--dry-run"))
        post_dry_snapshot = self.snapshot()
        evidence_count = psql(
            self.pg_container, self.admin, self.database,
            "SELECT (SELECT count(*) FROM legacy_reconciliation_runs)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_items)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_successes)",
        )
        if (
            dry_first != dry_second
            or dry_first.get("classification") != "DRY_RUN_COMPLETE_NO_MUTATION"
            or dry_first.get("counts") != {
                "database_rows": 13,
                "original_versions": 14,
                "manifest_versions": 1,
                "delete_markers": 1,
                "exact_reconciliations": 9,
                "null_version_candidates": 2,
                "ambiguous_matches": 3,
                "hash_mismatches": 2,
                "storage_orphans": 3,
                "database_orphans": 1,
                "contradictory_metadata": 1,
                "enforcement_failures": 0,
                "unresolved_or_quarantined": 10,
            }
            or pre_snapshot != post_dry_snapshot
            or evidence_count != "0:0:0"
        ):
            raise AcceptanceFailure("mandatory dry run mutated state or produced unexpected inventory")
        self.evidence["dry_run"] = {
            "first": dry_first,
            "second_identical": True,
            "storage_snapshot_before": pre_snapshot,
            "storage_snapshot_after": post_dry_snapshot,
            "evidence_rows_after": evidence_count,
        }

        psql(
            self.pg_container, self.admin, self.database,
            "CREATE SCHEMA legacy_reconciliation_fault;"
            "CREATE FUNCTION legacy_reconciliation_fault.reject_run() RETURNS trigger "
            "LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION '"
            + INTERRUPTION_MARKER
            + "'; END$$;"
            "CREATE TRIGGER synthetic_reconciliation_interruption BEFORE INSERT ON "
            "legacy_reconciliation_runs FOR EACH ROW EXECUTE FUNCTION "
            "legacy_reconciliation_fault.reject_run();",
        )
        interrupted = self.operator("--apply", check=False)
        if interrupted.returncode == 0:
            raise AcceptanceFailure("synthetic interrupted evidence write unexpectedly succeeded")
        interrupted_result = self.parse_operator(interrupted)
        log_result = docker("logs", self.pg_container, check=False)
        log_output = log_result.stdout + log_result.stderr
        if any(secret and secret in log_output for secret in self.secrets):
            raise AcceptanceFailure("synthetic secret appeared in owned PostgreSQL logs")
        marker_observed = INTERRUPTION_MARKER in log_output
        evidence_after_interruption = psql(
            self.pg_container, self.admin, self.database,
            "SELECT (SELECT count(*) FROM legacy_reconciliation_runs)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_items)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_successes)",
        )
        if (
            interrupted_result.get("classification") != "FAIL_LEGACY_RECONCILIATION"
            or not marker_observed
            or evidence_after_interruption != "0:0:0"
        ):
            raise AcceptanceFailure(
                "interrupted execution was not independently proven: "
                f"result={interrupted_result} marker={marker_observed} "
                f"evidence={evidence_after_interruption}"
            )
        psql(
            self.pg_container, self.admin, self.database,
            "DROP TRIGGER synthetic_reconciliation_interruption ON legacy_reconciliation_runs;"
            "DROP SCHEMA legacy_reconciliation_fault CASCADE;",
        )
        self.evidence["interrupted_execution"] = {
            "exit": interrupted.returncode,
            "classification": interrupted_result["classification"],
            "sanitized_marker_observed": marker_observed,
            "database_evidence_rows": evidence_after_interruption,
            "fault_objects_removed": True,
        }

        applied = self.operator("--apply")
        applied_result = self.parse_operator(applied)
        if PASS not in applied.stdout or applied_result.get("reused_run") is not False:
            raise AcceptanceFailure("recovery apply did not produce the required PASS")
        database_summary = psql(
            self.pg_container, self.admin, self.database,
            "SELECT (SELECT count(*) FROM legacy_reconciliation_runs)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_items)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_successes)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_items WHERE outcome='RECONCILED')||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_items WHERE outcome='QUARANTINED')",
        )
        classifications = psql(
            self.pg_container, self.admin, self.database,
            "SELECT classification||'='||count(*) FROM legacy_reconciliation_items "
            "GROUP BY classification ORDER BY classification",
        ).splitlines()
        if database_summary != "1:29:8:17:12":
            failure_codes = psql(
                self.pg_container, self.admin, self.database,
                "SELECT details_json->>'failure_code'||'='||count(*) FROM "
                "legacy_reconciliation_items WHERE classification='ENFORCEMENT_FAILURE' "
                "GROUP BY details_json->>'failure_code' ORDER BY 1",
            ).splitlines()
            raise AcceptanceFailure(
                "durable accounting mismatch: "
                + database_summary
                + f" failures={failure_codes}"
            )
        required_classifications = {
            "AMBIGUOUS_VERSION_MATCH=3", "HASH_MISMATCH=2",
            "ORPHAN_STORAGE_VERSION=3", "ORPHAN_DATABASE_ROW=1",
            "CONTRADICTORY_METADATA=1", "ENFORCEMENT_FAILURE=2",
            "DELETE_MARKER_RETAINED=1",
        }
        if not required_classifications.issubset(set(classifications)):
            raise AcceptanceFailure("one or more quarantine classifications are missing")

        verification_environment = {
            "MINIO_ACCESS_KEY": self.operator_user,
            "MINIO_SECRET_KEY": self.operator_password,
            "CALL_TOKEN": self.call_token,
            "DATABASE_URL": self.admin_url,
        }
        exact_verification = json.loads(
            self.container_python(VERIFY_PROGRAM, verification_environment).stdout
        )
        if exact_verification.get("exact_successes_verified") != 8:
            raise AcceptanceFailure("exact-version success readback was incomplete")

        denial_environment = {
            "MINIO_ACCESS_KEY": self.operator_user,
            "MINIO_SECRET_KEY": self.operator_password,
            "EXACT_VERSION_ID": fixture_by_name["exact"]["version_id"],
        }
        denials = json.loads(self.container_python(DENIAL_PROGRAM, denial_environment).stdout)
        if denials.get("denied_operations") != ["delete", "legal_hold", "put"]:
            raise AcceptanceFailure("operator MinIO authority was broader than approved")

        runtime_privileges = psql(
            self.pg_container, self.admin, self.database,
            "SELECT rolname||':'||bool_or(has_table_privilege(rolname,table_name,'INSERT,UPDATE,DELETE')) "
            "FROM (VALUES ('smartcoat_ingestion'),('smartcoat_ocr'),('smartcoat_review'),"
            "('smartcoat_backup'),('smartcoat_app')) roles(rolname) CROSS JOIN "
            "(VALUES ('legacy_reconciliation_runs'),('legacy_reconciliation_items'),"
            "('legacy_reconciliation_successes')) tables(table_name) GROUP BY rolname ORDER BY rolname",
        ).splitlines()
        if any(not value.endswith(":false") for value in runtime_privileges):
            raise AcceptanceFailure(
                "ordinary runtime identity obtained reconciliation mutation authority: "
                + str(runtime_privileges)
            )

        append_only = psql(
            self.pg_container, self.admin, self.database,
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN "
            "('legacy_reconciliation_runs_append_only','legacy_reconciliation_items_append_only',"
            "'legacy_reconciliation_successes_append_only')",
        )
        if append_only != "3":
            raise AcceptanceFailure("legacy evidence append-only guards are incomplete")

        repeated = self.operator("--apply")
        repeated_result = self.parse_operator(repeated)
        repeated_counts = psql(
            self.pg_container, self.admin, self.database,
            "SELECT (SELECT count(*) FROM legacy_reconciliation_runs)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_items)||':'||"
            "(SELECT count(*) FROM legacy_reconciliation_successes)",
        )
        if (
            PASS not in repeated.stdout
            or repeated_result.get("reused_run") is not True
            or repeated_result.get("reconciliation_run_id")
            != applied_result.get("reconciliation_run_id")
            or repeated_counts != "1:29:8"
            or repeated_result.get("summary") != applied_result.get("summary")
        ):
            raise AcceptanceFailure("complete repeated execution was not idempotent")

        final_snapshot = self.snapshot()
        if final_snapshot["entities"] != 16:
            raise AcceptanceFailure("an exact storage version or delete marker was lost")
        ledger = psql(
            self.pg_container, self.admin, self.database,
            "SELECT count(*)||':'||min(version)||':'||max(version) FROM "
            "smartcoat_migrations.applied_migrations",
        )
        if ledger != "9:1:9":
            raise AcceptanceFailure("migration ledger is not exactly 0001 through 0009")
        self.evidence["apply"] = {
            "classification": applied_result["classification"],
            "run_id": applied_result["reconciliation_run_id"],
            "durable_counts": database_summary,
            "classifications": classifications,
            "exact_version_readback": exact_verification,
            "operator_policy_denials": denials,
            "runtime_mutation_privileges": runtime_privileges,
            "append_only_trigger_count": int(append_only),
            "migration_ledger": ledger,
            "all_storage_entities_retained": final_snapshot,
        }
        self.evidence["idempotent_repeat"] = {
            "reused_run": repeated_result["reused_run"],
            "same_run_id": True,
            "same_summary": True,
            "durable_counts": repeated_counts,
        }
        return self.evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    args = parser.parse_args(argv)
    if not getattr(args, CONFIRM_FLAG[2:].replace("-", "_")):
        print(json.dumps({"authorized": False, "classification": BLOCKED}, sort_keys=True))
        print(BLOCKED)
        return 2
    harness = Harness()
    try:
        evidence = harness.run_live()
        harness.finalize()
        print(json.dumps(evidence, sort_keys=True))
        print(PASS)
        return 0
    except IsolationBlocked as exc:
        try:
            harness.finalize()
        except Exception:
            pass
        print(json.dumps({"classification": BLOCKED, "reason": str(exc)}, sort_keys=True))
        print(BLOCKED)
        return 2
    except Exception as exc:
        try:
            harness.finalize()
        except Exception as cleanup_exc:
            exc = cleanup_exc
        print(json.dumps({
            "classification": FAIL,
            "reason": type(exc).__name__,
            "sanitized_detail": str(exc)[:240],
        }, sort_keys=True))
        print(FAIL)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
