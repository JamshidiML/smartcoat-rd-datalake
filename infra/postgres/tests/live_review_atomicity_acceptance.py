#!/usr/bin/env python3
"""Opt-in M0-R04 acceptance against disposable synthetic PostgreSQL only.

The default invocation fails closed without touching Docker.  The live mode
uses already-local immutable image IDs, publishes no ports, creates a distinct
internal network and owned volume for each scenario, and removes every owned
resource before returning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from urllib.parse import quote, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
POSTGRES_REF = "postgres:17.6-alpine"
API_REF = "smartcoat-rd-datalake-api:latest"
CONFIRM_FLAG = "--confirm-disposable-synthetic-review-run"
PASS = "PASS_M0_R04"
BLOCKED_ISOLATION = "BLOCKED_ISOLATION"
BLOCKED_ENVIRONMENT = "BLOCKED_ENVIRONMENT"
FAIL_PRODUCT_CONTRACT = "FAIL_PRODUCT_CONTRACT"
PROJECT_PATTERN = re.compile(r"^m0r04-(fresh|upgraded)-[0-9a-f]{10}$")
IMAGE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
LABEL = "smartcoat.m0r04.project"
CAPABILITY_SCHEMA = "m0r04_review_acceptance"
CAPABILITY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
INTERNAL_ENVIRONMENT_KEYS = {
    "capability": "M0R04_INTERNAL_CAPABILITY",
    "project": "M0R04_INTERNAL_PROJECT",
    "database": "M0R04_INTERNAL_DATABASE",
    "app_url": "REVIEW_DATABASE_URL",
    "admin_url": "REVIEW_ADMIN_DATABASE_URL",
}

LEGACY = {
    "ingestion_id": "00000000-0000-4000-8000-000000000401",
    "ocr_job_id": "00000000-0000-4000-8000-000000000402",
    "ocr_run_id": "00000000-0000-4000-8000-000000000403",
    "draft_id": "00000000-0000-4000-8000-000000000404",
    "decision_id": "00000000-0000-4000-8000-000000000405",
    "record_id": "00000000-0000-4000-8000-000000000406",
}


class AcceptanceError(RuntimeError):
    pass


class EnvironmentBlocked(AcceptanceError):
    pass


class IsolationBlocked(AcceptanceError):
    pass


def run(
    command: list[str],
    *,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise AcceptanceError(
            f"sanitized command failure: executable={command[0]!r}, "
            f"exit={completed.returncode}"
        )
    return completed


def docker(*arguments: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["docker", *arguments], check=check, timeout=timeout)


def image_id(reference: str) -> str:
    completed = docker("image", "inspect", reference, "--format", "{{.Id}}", check=False)
    value = completed.stdout.strip()
    if completed.returncode != 0 or not IMAGE_PATTERN.fullmatch(value):
        raise EnvironmentBlocked(f"required already-local image unavailable: {reference}")
    return value


def inventory() -> dict[str, list[str]]:
    return {
        "containers": sorted(filter(None, docker("ps", "-aq").stdout.splitlines())),
        "networks": sorted(filter(None, docker("network", "ls", "-q").stdout.splitlines())),
        "volumes": sorted(filter(None, docker("volume", "ls", "-q").stdout.splitlines())),
    }


def inventory_evidence(value: dict[str, list[str]]) -> dict[str, Any]:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "counts": {key: len(items) for key, items in value.items()},
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def project_resources(project: str) -> dict[str, list[str]]:
    return {
        "containers": sorted(
            filter(None, docker("ps", "-aq", "--filter", f"label={LABEL}={project}").stdout.splitlines())
        ),
        "networks": sorted(
            filter(None, docker("network", "ls", "-q", "--filter", f"label={LABEL}={project}").stdout.splitlines())
        ),
        "volumes": sorted(
            filter(None, docker("volume", "ls", "-q", "--filter", f"label={LABEL}={project}").stdout.splitlines())
        ),
    }


def assert_owned(kind: str, name: str, project: str) -> None:
    template = "{{ index .Config.Labels \"" + LABEL + "\" }}"
    if kind in {"network", "volume"}:
        template = "{{ index .Labels \"" + LABEL + "\" }}"
    observed = docker(kind, "inspect", name, "--format", template).stdout.strip()
    if observed != project:
        raise IsolationBlocked(f"refusing to finalize unowned {kind} resource")


def _resource_exists(kind: str, name: str) -> bool:
    return docker(kind, "inspect", name, check=False).returncode == 0


def _remove_resource(kind: str, name: str) -> None:
    if kind == "container":
        docker("rm", "-f", name)
    else:
        docker(kind, "rm", name)


def _remove_owned_resource(kind: str, name: str, project: str) -> None:
    """Remove one owned target while tolerating Docker auto-remove races."""

    for attempt in range(4):
        if not _resource_exists(kind, name):
            return
        try:
            assert_owned(kind, name, project)
        except Exception:
            if not _resource_exists(kind, name):
                return
            raise
        try:
            _remove_resource(kind, name)
        except Exception:
            if not _resource_exists(kind, name):
                return
            if attempt == 3:
                raise
            time.sleep(0.25)
            continue
        if not _resource_exists(kind, name):
            return
        if attempt != 3:
            time.sleep(0.25)
    raise IsolationBlocked(f"owned {kind} resource survived cleanup")


def finalize(project: str, container: str, network: str, volume: str) -> None:
    """Best-effort sweep of all owned resources, including timed-out one-shots."""

    failures: list[str] = []
    try:
        observed = project_resources(project)
    except Exception:
        observed = {"containers": [], "networks": [], "volumes": []}
        failures.append("owned resource enumeration failed")

    targets = {
        "container": list(observed["containers"]),
        "network": list(observed["networks"]),
        "volume": list(observed["volumes"]),
    }
    for kind, explicit in (("container", container), ("network", network), ("volume", volume)):
        try:
            if _resource_exists(kind, explicit) and explicit not in targets[kind]:
                targets[kind].append(explicit)
        except Exception:
            failures.append(f"{kind} existence check failed")

    # Containers are removed first so a timed-out labeled one-shot cannot keep
    # the owned internal network or volume busy.  Every later target is still
    # attempted if inspection or removal of an earlier target fails.
    for kind in ("container", "network", "volume"):
        for name in targets[kind]:
            try:
                _remove_owned_resource(kind, name, project)
            except Exception:
                failures.append(f"owned {kind} cleanup failed")

    try:
        remaining = project_resources(project)
    except Exception:
        remaining = {"containers": ["unverified"], "networks": [], "volumes": []}
        failures.append("post-cleanup resource enumeration failed")
    if any(remaining.values()):
        failures.append("owned disposable resources remain after finalization")
    if failures:
        raise IsolationBlocked("; ".join(failures))


def database_url(user: str, password: str, database: str) -> str:
    return f"postgresql://{quote(user)}:{quote(password)}@postgres/{quote(database)}"


def expected_database_name(project: str) -> str:
    if not PROJECT_PATTERN.fullmatch(project):
        raise IsolationBlocked("internal project identity is invalid")
    return project.replace("-", "_")


def validate_internal_url(value: str, *, user: str, database: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise IsolationBlocked(
            "internal database URL escaped the owned scenario boundary"
        ) from exc
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != "postgres"
        or port not in (None, 5432)
        or unquote(parsed.username or "") != user
        or not parsed.password
        or unquote(parsed.path.removeprefix("/")) != database
        or parsed.query
        or parsed.fragment
    ):
        raise IsolationBlocked("internal database URL escaped the owned scenario boundary")


def internal_identity(mode: str, environment: dict[str, str]) -> dict[str, str]:
    try:
        identity = {
            name: environment[key]
            for name, key in INTERNAL_ENVIRONMENT_KEYS.items()
        }
    except KeyError as exc:
        raise IsolationBlocked("internal capability environment is incomplete") from exc
    if not CAPABILITY_PATTERN.fullmatch(identity["capability"]):
        raise IsolationBlocked("internal capability is invalid")
    project = identity["project"]
    database = identity["database"]
    if database != expected_database_name(project):
        raise IsolationBlocked("internal database identity does not match the owned project")
    if mode not in {"seed-legacy", "fresh", "upgraded"}:
        raise IsolationBlocked("internal mode is invalid")
    validate_internal_url(identity["app_url"], user="smartcoat_app", database=database)
    validate_internal_url(identity["admin_url"], user="r04_admin", database=database)
    return identity


def authenticate_internal_boundary(
    mode: str,
    environment: dict[str, str],
    connector: Any,
) -> dict[str, str]:
    """Authenticate the controller capability before any acceptance mutation."""

    identity = internal_identity(mode, environment)
    capability_sha256 = hashlib.sha256(identity["capability"].encode()).hexdigest()
    try:
        with connector(identity["admin_url"]) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                f"""
                SELECT current_database(), current_user, count(*)
                FROM {CAPABILITY_SCHEMA}.internal_capabilities
                WHERE project = %s AND database_name = %s AND mode = %s
                  AND capability_sha256 = %s
                GROUP BY current_database(), current_user
                """,
                (
                    identity["project"],
                    identity["database"],
                    mode,
                    capability_sha256,
                ),
            ).fetchone()
    except Exception as exc:
        raise IsolationBlocked("internal capability could not be authenticated") from exc
    if row != (identity["database"], "r04_admin", 1):
        raise IsolationBlocked("internal capability did not authenticate the owned database")
    return identity


def one_shot(
    *,
    project: str,
    suffix: str,
    network: str,
    image: str,
    environment: dict[str, str],
    command: list[str],
    mount: str | None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    name = f"{project}-{suffix}"
    arguments = [
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--label",
        f"{LABEL}={project}",
        "--network",
        network,
    ]
    for key, value in environment.items():
        arguments.extend(["--env", f"{key}={value}"])
    if mount is not None:
        arguments.extend(["--mount", mount])
    arguments.extend([image, *command])
    return docker(*arguments, check=False, timeout=timeout)


def wait_ready(container: str, user: str, database: str) -> None:
    for _ in range(90):
        result = docker(
            "exec",
            container,
            "pg_isready",
            "-U",
            user,
            "-d",
            database,
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise EnvironmentBlocked("disposable PostgreSQL did not become ready")


def migration_command(
    project: str,
    network: str,
    api_image: str,
    url: str,
    action: str,
    database: str,
) -> None:
    command = ["python", "/opt/smartcoat-postgres/migrate.py", action]
    if action == "adopt":
        command.append(database)
    result = one_shot(
        project=project,
        suffix=f"migration-{action}",
        network=network,
        image=api_image,
        environment={"MIGRATION_DATABASE_URL": url},
        command=command,
        mount=(
            f"type=bind,src={ROOT / 'infra/postgres'},"
            "dst=/opt/smartcoat-postgres,readonly"
        ),
        timeout=180,
    )
    if result.returncode != 0:
        raise AcceptanceError(f"migration {action} failed with exit {result.returncode}")


def assert_scenario_boundary(project: str, container: str, network: str) -> None:
    assert_owned("container", container, project)
    assert_owned("network", network, project)
    container_networks = json.loads(
        docker(
            "container",
            "inspect",
            container,
            "--format",
            "{{json .NetworkSettings.Networks}}",
        ).stdout
    )
    network_internal = docker(
        "network", "inspect", network, "--format", "{{.Internal}}"
    ).stdout.strip()
    if set(container_networks) != {network} or network_internal != "true":
        raise IsolationBlocked("owned PostgreSQL escaped the exact internal network boundary")


def install_internal_capabilities(
    *,
    project: str,
    container: str,
    network: str,
    api_image: str,
    admin_url: str,
    database: str,
    capability: str,
    modes: tuple[str, ...],
) -> None:
    assert_scenario_boundary(project, container, network)
    capability_sha256 = hashlib.sha256(capability.encode()).hexdigest()
    script = """
import json
import os

try:
    import psycopg

    connection = psycopg.connect(os.environ["REVIEW_ADMIN_DATABASE_URL"])
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA m0r04_review_acceptance")
        cursor.execute(
            "CREATE TABLE m0r04_review_acceptance.internal_capabilities "
            "(project text NOT NULL, database_name text NOT NULL, mode text NOT NULL, "
            "capability_sha256 text NOT NULL, PRIMARY KEY (project, mode))"
        )
        cursor.executemany(
            "INSERT INTO m0r04_review_acceptance.internal_capabilities "
            "VALUES (%s,%s,%s,%s)",
            [
                (
                    os.environ["M0R04_INTERNAL_PROJECT"],
                    os.environ["M0R04_INTERNAL_DATABASE"],
                    mode,
                    os.environ["M0R04_INTERNAL_CAPABILITY_SHA256"],
                )
                for mode in os.environ["M0R04_INTERNAL_MODES"].split(",")
            ],
        )
    connection.commit()
    connection.close()
except Exception as exc:
    print(json.dumps({
        "classification": "SANITIZED_CAPABILITY_INSTALL_FAILURE",
        "exception_type": type(exc).__name__,
        "sqlstate": getattr(exc, "sqlstate", None),
    }, sort_keys=True))
    raise SystemExit(1)
"""
    result = one_shot(
        project=project,
        suffix="authorize-internal",
        network=network,
        image=api_image,
        environment={
            "REVIEW_ADMIN_DATABASE_URL": admin_url,
            "M0R04_INTERNAL_PROJECT": project,
            "M0R04_INTERNAL_DATABASE": database,
            "M0R04_INTERNAL_CAPABILITY_SHA256": capability_sha256,
            "M0R04_INTERNAL_MODES": ",".join(modes),
        },
        command=["python", "-c", script],
        mount=None,
    )
    if result.returncode != 0:
        diagnostic = "unparseable_sanitized_diagnostic"
        try:
            value = json.loads(result.stdout)
            if value.get("classification") == "SANITIZED_CAPABILITY_INSTALL_FAILURE":
                diagnostic = (
                    f"{value.get('exception_type', 'unknown')}:"
                    f"{value.get('sqlstate') or 'no_sqlstate'}"
                )
        except (json.JSONDecodeError, AttributeError):
            pass
        raise AcceptanceError(
            "internal capability installation failed with "
            f"exit {result.returncode}: {diagnostic}"
        )


def internal_command(
    project: str,
    container: str,
    network: str,
    api_image: str,
    app_url: str,
    admin_url: str,
    database: str,
    capability: str,
    mode: str,
) -> dict[str, Any]:
    assert_scenario_boundary(project, container, network)
    result = one_shot(
        project=project,
        suffix=f"verify-{mode}",
        network=network,
        image=api_image,
        environment={
            "REVIEW_DATABASE_URL": app_url,
            "REVIEW_ADMIN_DATABASE_URL": admin_url,
            "M0R04_INTERNAL_CAPABILITY": capability,
            "M0R04_INTERNAL_PROJECT": project,
            "M0R04_INTERNAL_DATABASE": database,
        },
        command=[
            "python",
            "/workspace/infra/postgres/tests/live_review_atomicity_acceptance.py",
            CONFIRM_FLAG,
            "--internal",
            mode,
        ],
        mount=f"type=bind,src={ROOT},dst=/workspace,readonly",
        timeout=240,
    )
    if result.returncode != 0:
        classification = "unparseable_sanitized_evidence"
        try:
            failed_evidence = json.loads(result.stdout)
            classification = str(failed_evidence.get("classification", "unknown"))
            reason = failed_evidence.get("reason")
            if reason:
                classification = f"{classification}:{reason}"
        except (json.JSONDecodeError, AttributeError):
            pass
        raise AcceptanceError(
            f"internal {mode} verification failed with exit {result.returncode}: "
            f"{classification}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(f"internal {mode} evidence was not valid JSON") from exc
    if not isinstance(value, dict) or value.get("status") != "passed":
        raise AcceptanceError(f"internal {mode} verification did not pass")
    return value


def scenario(kind: str, postgres_image: str, api_image: str) -> dict[str, Any]:
    project = f"m0r04-{kind}-{secrets.token_hex(5)}"
    if not PROJECT_PATTERN.fullmatch(project):
        raise IsolationBlocked("generated project identifier failed validation")
    network = f"{project}-backend"
    volume = f"{project}-postgres"
    container = f"{project}-postgres"
    database = expected_database_name(project)
    admin_user = "r04_admin"
    admin_password = secrets.token_hex(24)
    app_password = secrets.token_hex(24)
    capability = secrets.token_hex(32)
    admin_url = database_url(admin_user, admin_password, database)
    app_url = database_url("smartcoat_app", app_password, database)
    constructed = False
    evidence: dict[str, Any] = {"scenario": kind, "project": project}
    try:
        docker("network", "create", "--internal", "--label", f"{LABEL}={project}", network)
        constructed = True
        docker("volume", "create", "--label", f"{LABEL}={project}", volume)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            container,
            "--label",
            f"{LABEL}={project}",
            "--network",
            network,
            "--network-alias",
            "postgres",
            "--env",
            f"POSTGRES_DB={database}",
            "--env",
            f"POSTGRES_USER={admin_user}",
            "--env",
            f"POSTGRES_PASSWORD={admin_password}",
            "--env",
            f"POSTGRES_APP_PASSWORD={app_password}",
            "--mount",
            f"type=volume,src={volume},dst=/var/lib/postgresql/data",
            "--mount",
            (
                f"type=bind,src={ROOT / 'infra/postgres/init.sql'},"
                "dst=/docker-entrypoint-initdb.d/001-init.sql,readonly"
            ),
            postgres_image,
        )
        wait_ready(container, admin_user, database)
        migration_command(project, network, api_image, admin_url, "adopt", database)
        modes = ("seed-legacy", "upgraded") if kind == "upgraded" else ("fresh",)
        install_internal_capabilities(
            project=project,
            container=container,
            network=network,
            api_image=api_image,
            admin_url=admin_url,
            database=database,
            capability=capability,
            modes=modes,
        )
        evidence["internal_boundary"] = {
            "owned_project": project,
            "database": database,
            "capability_registered": True,
        }
        if kind == "upgraded":
            evidence["legacy_seed"] = internal_command(
                project, container, network, api_image, app_url, admin_url,
                database, capability, "seed-legacy"
            )
        migration_command(project, network, api_image, admin_url, "apply", database)
        evidence["verification"] = internal_command(
            project, container, network, api_image, app_url, admin_url,
            database, capability, kind
        )
        return evidence
    finally:
        if constructed:
            finalize(project, container, network, volume)
            evidence["cleanup"] = {
                "remaining": project_resources(project),
                "finalizer_count": 1,
            }


def catalog_contract(connection: Any) -> dict[str, Any]:
    indexes = {
        row[0]: {
            "table": row[1],
            "unique": bool(row[2]),
            "valid": bool(row[3]),
            "ready": bool(row[4]),
            "key_columns": list(row[5]),
            "key_count": int(row[6]),
            "total_columns": int(row[7]),
            "predicate": row[8],
            "expressions": row[9],
        }
        for row in connection.execute(
            """
            SELECT index_class.relname, table_class.relname,
                   index_meta.indisunique, index_meta.indisvalid,
                   index_meta.indisready,
                   ARRAY(
                       SELECT pg_get_indexdef(index_meta.indexrelid, position, true)
                       FROM generate_series(1, index_meta.indnkeyatts) AS position
                       ORDER BY position
                   ),
                   index_meta.indnkeyatts, index_meta.indnatts,
                   pg_get_expr(index_meta.indpred, index_meta.indrelid),
                   pg_get_expr(index_meta.indexprs, index_meta.indrelid)
            FROM pg_index AS index_meta
            JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
            JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid
            JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
            WHERE namespace.nspname = 'public'
              AND index_class.relname IN (
                'review_decisions_one_per_draft_uidx',
                'silver_verified_records_one_per_decision_uidx'
              )
            """
        ).fetchall()
    }
    expected_indexes = {
        "review_decisions_one_per_draft_uidx": {
            "table": "review_decisions", "unique": True, "valid": True,
            "ready": True, "key_columns": ["silver_draft_id"],
            "key_count": 1, "total_columns": 1, "predicate": None,
            "expressions": None,
        },
        "silver_verified_records_one_per_decision_uidx": {
            "table": "silver_verified_records", "unique": True, "valid": True,
            "ready": True, "key_columns": ["review_decision_id"],
            "key_count": 1, "total_columns": 1, "predicate": None,
            "expressions": None,
        },
    }
    if indexes != expected_indexes:
        raise AcceptanceError("M0-R04 exact unique-index contract is incomplete")
    constraints = {
        row[0]: bool(row[1])
        for row in connection.execute(
            """
            SELECT conname, convalidated
            FROM pg_constraint
            WHERE conrelid = 'public.review_decisions'::regclass
              AND conname IN (
                'review_decisions_request_sha256_format',
                'review_decisions_request_sha256_required_for_new_rows'
              )
            """
        ).fetchall()
    }
    expected_constraints = {
        "review_decisions_request_sha256_format": True,
        "review_decisions_request_sha256_required_for_new_rows": False,
    }
    if constraints != expected_constraints:
        raise AcceptanceError("M0-R04 request-fingerprint constraint contract is incomplete")
    return {"unique_indexes": indexes, "constraint_validation": constraints}


def create_fixture(repository: Any, domain: Any, suffix: str) -> tuple[Any, dict[str, Any]]:
    class SyntheticStorage:
        def __init__(self) -> None:
            self.objects: dict[tuple[str, str], bytes] = {}

        def put_once(self, bucket: str, key: str, data: bytes, content_type: str, locked: bool) -> dict[str, str]:
            del content_type, locked
            self.objects[(bucket, key)] = bytes(data)
            return {"version_id": f"synthetic-{suffix}"}

        def get(self, bucket: str, key: str) -> bytes:
            return self.objects[(bucket, key)]

    actor = domain.Actor("usr_m0r04", "M0-R04 Synthetic Reviewer")
    repository.ensure_local_user(actor.user_id, actor.display_name, "m0r04@localhost")
    source = b"\xff\xd8\xff\xe0" + f"m0-r04-{suffix}".encode() + b"\xff\xd9"
    upload = domain.IngestionService(repository, SyntheticStorage(), 1024 * 1024).ingest(
        actor,
        f"m0-r04-{suffix}.jpg",
        source,
        "LAB_NOTE",
        f"Synthetic M0-R04 {suffix} fixture only.",
        None,
    )
    ocr = domain.OCRDomainService(repository)
    run_id = ocr.start(upload["ingestion_id"], "paddleocr", "3.7.0", {"fixture": suffix})
    draft = ocr.complete(
        upload["ingestion_id"],
        run_id,
        "Temperature 23 C",
        [],
        json.dumps({"fixture": suffix}).encode(),
        f"rd/synthetic/{run_id}.json",
    )
    return actor, draft


def review_snapshot(connection: Any, draft_id: str, ingestion_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT d.status, u.state,
          (SELECT count(*) FROM review_decisions WHERE silver_draft_id = d.silver_draft_id),
          (SELECT count(*) FROM silver_verified_records WHERE ingestion_id = u.ingestion_id),
          (SELECT count(*) FROM audit_events
             WHERE entity_type = 'SILVER_DRAFT' AND entity_id = d.silver_draft_id::text
               AND event_type = 'HUMAN_REVIEW_RECORDED'),
          (SELECT count(*) FROM audit_events
             WHERE entity_type = 'UPLOAD' AND entity_id = u.ingestion_id::text
               AND new_state IN ('VERIFIED', 'REVIEW_REJECTED'))
        FROM silver_drafts d JOIN uploads u ON u.ingestion_id = d.ingestion_id
        WHERE d.silver_draft_id = %s AND u.ingestion_id = %s
        """,
        (draft_id, ingestion_id),
    ).fetchone()
    if not row:
        raise AcceptanceError("review fixture disappeared")
    return {
        "draft_status": row[0],
        "upload_state": row[1],
        "decisions": int(row[2]),
        "verified": int(row[3]),
        "review_audits": int(row[4]),
        "final_state_audits": int(row[5]),
    }


def install_fault(connection: Any, checkpoint: str, draft_id: str, ingestion_id: str) -> tuple[str, str]:
    from psycopg import sql

    specifications = {
        "decision": ("review_decisions", "INSERT", sql.SQL("NEW.silver_draft_id = {}::uuid").format(sql.Literal(draft_id))),
        "draft": ("silver_drafts", "UPDATE", sql.SQL("NEW.silver_draft_id = {}::uuid").format(sql.Literal(draft_id))),
        "verified": ("silver_verified_records", "INSERT", sql.SQL("NEW.ingestion_id = {}::uuid").format(sql.Literal(ingestion_id))),
        "review_audit": (
            "audit_events",
            "INSERT",
            sql.SQL("NEW.entity_id = {} AND NEW.event_type = 'HUMAN_REVIEW_RECORDED'").format(sql.Literal(draft_id)),
        ),
        "enter_review": (
            "uploads",
            "UPDATE",
            sql.SQL("NEW.ingestion_id = {}::uuid AND NEW.state = 'UNDER_HUMAN_REVIEW'").format(sql.Literal(ingestion_id)),
        ),
        "final_state": (
            "uploads",
            "UPDATE",
            sql.SQL("NEW.ingestion_id = {}::uuid AND NEW.state = 'VERIFIED'").format(sql.Literal(ingestion_id)),
        ),
    }
    table, operation, predicate = specifications[checkpoint]
    function = f"reject_{checkpoint}"
    trigger = f"m0r04_fault_{checkpoint}"
    marker = f"M0R04_FAULT_{checkpoint.upper()}"
    connection.execute("CREATE SCHEMA IF NOT EXISTS m0r04_faults")
    connection.execute(
        sql.SQL(
            "CREATE OR REPLACE FUNCTION m0r04_faults.{}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF {} THEN RAISE EXCEPTION '{}'; END IF; RETURN NEW; END; $$"
        ).format(sql.Identifier(function), predicate, sql.SQL(marker))
    )
    connection.execute(
        sql.SQL("CREATE TRIGGER {} BEFORE {} ON public.{} FOR EACH ROW EXECUTE FUNCTION m0r04_faults.{}()")
        .format(sql.Identifier(trigger), sql.SQL(operation), sql.Identifier(table), sql.Identifier(function))
    )
    return table, marker


def remove_fault(connection: Any, checkpoint: str, table: str) -> None:
    from psycopg import sql

    connection.execute(
        sql.SQL("DROP TRIGGER {} ON public.{}").format(
            sql.Identifier(f"m0r04_fault_{checkpoint}"), sql.Identifier(table)
        )
    )
    connection.execute(
        sql.SQL("DROP FUNCTION m0r04_faults.{}()").format(sql.Identifier(f"reject_{checkpoint}"))
    )


def assert_null_fingerprint_rejected(admin_url: str, repository: Any, domain: Any, suffix: str) -> None:
    import psycopg

    actor, draft = create_fixture(repository, domain, suffix)
    try:
        with psycopg.connect(admin_url) as connection:
            connection.execute(
                """
                INSERT INTO review_decisions (
                    review_decision_id, silver_draft_id, ingestion_id, reviewer_user_id,
                    reviewed_at_utc, decision, explicit_confirmation, correction_summary,
                    self_review_detected, solo_exception_applied,
                    administrator_exception_reason, review_request_sha256
                ) VALUES (
                    gen_random_uuid(), %s, %s, %s, now(), 'REJECTED_UNREADABLE',
                    true, 'synthetic direct SQL check', true, true,
                    'synthetic direct SQL check', NULL
                )
                """,
                (draft["silver_draft_id"], draft["ingestion_id"], actor.user_id),
            )
    except psycopg.errors.CheckViolation:
        pass
    else:
        raise AcceptanceError("new direct review row without a request fingerprint was accepted")
    with psycopg.connect(admin_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM review_decisions WHERE silver_draft_id = %s",
            (draft["silver_draft_id"],),
        ).fetchone()[0]
        if count != 0:
            raise AcceptanceError("rejected direct SQL review left partial evidence")


def assert_direct_duplicates_rejected(
    admin_url: str,
    repository: Any,
    domain: Any,
) -> dict[str, Any]:
    import psycopg

    actor, draft = create_fixture(repository, domain, "direct-duplicate-guards")
    result = domain.ReviewService(repository, True).review(
        draft["silver_draft_id"], actor, "Temperature 23 °C",
        "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
    )
    if result is None:
        raise AcceptanceError("direct duplicate fixture did not create a verified record")
    with psycopg.connect(admin_url) as connection:
        review_decision_id = connection.execute(
            "SELECT review_decision_id FROM review_decisions WHERE silver_draft_id = %s",
            (draft["silver_draft_id"],),
        ).fetchone()[0]

    expected = {
        "decision": "review_decisions_one_per_draft_uidx",
        "verified_record": "silver_verified_records_one_per_decision_uidx",
    }
    observed: dict[str, str] = {}
    try:
        with psycopg.connect(admin_url) as connection:
            connection.execute(
                """
                INSERT INTO review_decisions (
                    review_decision_id, silver_draft_id, ingestion_id, reviewer_user_id,
                    reviewed_at_utc, decision, explicit_confirmation, correction_summary,
                    self_review_detected, solo_exception_applied,
                    administrator_exception_reason, review_request_sha256
                )
                SELECT gen_random_uuid(), silver_draft_id, ingestion_id, reviewer_user_id,
                       now(), decision, explicit_confirmation, correction_summary,
                       self_review_detected, solo_exception_applied,
                       administrator_exception_reason, repeat('d', 64)
                FROM review_decisions WHERE silver_draft_id = %s
                """,
                (draft["silver_draft_id"],),
            )
    except psycopg.errors.UniqueViolation as exc:
        observed["decision"] = str(exc.diag.constraint_name)
    else:
        raise AcceptanceError("direct SQL duplicate review decision was accepted")

    try:
        with psycopg.connect(admin_url) as connection:
            connection.execute(
                """
                INSERT INTO silver_verified_records (
                    silver_record_id, silver_revision, ingestion_id, source_sha256,
                    status, verified_text, reviewer_user_id, reviewed_at_utc,
                    review_decision, correction_summary, source_object_key,
                    ocr_artifact_key, review_decision_id
                )
                SELECT gen_random_uuid(), silver_revision + 1, ingestion_id, source_sha256,
                       status, verified_text, reviewer_user_id, now(), review_decision,
                       correction_summary, source_object_key, ocr_artifact_key,
                       review_decision_id
                FROM silver_verified_records WHERE review_decision_id = %s
                """,
                (review_decision_id,),
            )
    except psycopg.errors.UniqueViolation as exc:
        observed["verified_record"] = str(exc.diag.constraint_name)
    else:
        raise AcceptanceError("direct SQL duplicate verified record was accepted")

    if observed != expected:
        raise AcceptanceError("direct SQL duplicates hit an unexpected constraint")
    with psycopg.connect(admin_url) as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM review_decisions WHERE silver_draft_id = %s),
              (SELECT count(*) FROM silver_verified_records WHERE review_decision_id = %s)
            """,
            (draft["silver_draft_id"], review_decision_id),
        ).fetchone()
    if counts != (1, 1):
        raise AcceptanceError("rejected direct SQL duplicates left partial rows")
    return {
        "constraint_names": observed,
        "decision_rows": int(counts[0]),
        "verified_record_rows": int(counts[1]),
    }


def seed_legacy(admin_url: str) -> dict[str, Any]:
    import psycopg

    sha = "4" * 64
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "INSERT INTO users VALUES ('usr_m0r04_legacy','Legacy Synthetic Reviewer','legacy-m0r04@localhost','ADMIN_REVIEWER',true,now())"
        )
        connection.execute(
            """
            INSERT INTO uploads VALUES (
              %s,'RND','usr_m0r04_legacy','Legacy Synthetic Reviewer',now(),
              'legacy-synthetic.jpg','rd/legacy/original.jpg','rd/legacy/manifest.json',
              'image/jpeg','PHOTO','LAB_NOTE','Synthetic legacy M0-R04 fixture only.',NULL,
              128,%s,NULL,'WEB_UPLOAD','VERIFIED')
            """,
            (LEGACY["ingestion_id"], sha),
        )
        connection.execute(
            "INSERT INTO ocr_jobs VALUES (%s,%s,'COMPLETED',now(),now(),now(),1,NULL)",
            (LEGACY["ocr_job_id"], LEGACY["ingestion_id"]),
        )
        connection.execute(
            """
            INSERT INTO ocr_runs VALUES (
              %s,%s,%s,'paddleocr','3.7.0','{}',%s,%s,
              'rd/legacy/ocr.json','COMPLETED',now(),now())
            """,
            (LEGACY["ocr_run_id"], LEGACY["ocr_job_id"], LEGACY["ingestion_id"], sha, sha),
        )
        connection.execute(
            """
            INSERT INTO silver_drafts VALUES (
              %s,%s,%s,%s,'REVIEWED','legacy synthetic text','[]','PHOTO',
              'LAB_NOTE','paddleocr','3.7.0',now())
            """,
            (LEGACY["draft_id"], LEGACY["ingestion_id"], sha, LEGACY["ocr_run_id"]),
        )
        connection.execute(
            """
            INSERT INTO review_decisions VALUES (
              %s,%s,%s,'usr_m0r04_legacy',now(),'APPROVED_NO_CHANGES',true,'',true,true,
              'Synthetic pre-M0-R04 solo-review evidence')
            """,
            (LEGACY["decision_id"], LEGACY["draft_id"], LEGACY["ingestion_id"]),
        )
        connection.execute(
            """
            INSERT INTO silver_verified_records VALUES (
              %s,1,%s,%s,'VERIFIED','legacy synthetic text','usr_m0r04_legacy',now(),
              'APPROVED_NO_CHANGES','','rd/legacy/original.jpg','rd/legacy/ocr.json',%s)
            """,
            (LEGACY["record_id"], LEGACY["ingestion_id"], sha, LEGACY["decision_id"]),
        )
    return {"legacy_decisions": 1, "legacy_verified_records": 1}


def internal_verify(mode: str) -> dict[str, Any]:
    import psycopg

    identity = authenticate_internal_boundary(mode, dict(os.environ), psycopg.connect)
    sys.path.insert(0, "/workspace/apps/api/src")
    import domain
    from database import PostgresRepository

    app_url = identity["app_url"]
    admin_url = identity["admin_url"]
    boundary = {
        "capability_authenticated": True,
        "owned_project": identity["project"],
        "database": identity["database"],
    }
    if mode == "seed-legacy":
        return {"status": "passed", "internal_boundary": boundary, **seed_legacy(admin_url)}

    repository = PostgresRepository(app_url)
    with psycopg.connect(admin_url) as connection:
        catalog = catalog_contract(connection)
    assert_null_fingerprint_rejected(admin_url, repository, domain, f"{mode}-null")

    if mode == "upgraded":
        with psycopg.connect(admin_url) as connection:
            legacy = connection.execute(
                """
                SELECT rd.review_request_sha256, d.status, u.state,
                       count(v.silver_record_id)
                FROM review_decisions rd
                JOIN silver_drafts d ON d.silver_draft_id = rd.silver_draft_id
                JOIN uploads u ON u.ingestion_id = rd.ingestion_id
                LEFT JOIN silver_verified_records v ON v.review_decision_id = rd.review_decision_id
                WHERE rd.review_decision_id = %s
                GROUP BY rd.review_request_sha256, d.status, u.state
                """,
                (LEGACY["decision_id"],),
            ).fetchone()
        if legacy != (None, "REVIEWED", "VERIFIED", 1):
            raise AcceptanceError("valid pre-M0-R04 review history changed during upgrade")
        actor = domain.Actor("usr_m0r04_legacy", "Legacy Synthetic Reviewer")
        try:
            domain.ReviewService(repository, True).review(
                LEGACY["draft_id"], actor, "legacy synthetic text",
                "APPROVED_NO_CHANGES", "", True
            )
        except domain.StateConflict:
            pass
        else:
            raise AcceptanceError("legacy row without an authenticated fingerprint replayed")
        actor, draft = create_fixture(repository, domain, "upgraded-production")
        service = domain.ReviewService(repository, True)
        first = service.review(
            draft["silver_draft_id"], actor, "Temperature 23 °C",
            "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
        )
        second = service.review(
            draft["silver_draft_id"], actor, "Temperature 23 °C",
            "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
        )
        if first != second:
            raise AcceptanceError("upgraded-volume exact retry was not stable")
        return {
            "status": "passed",
            "internal_boundary": boundary,
            "catalog": catalog,
            "legacy_preserved": True,
            "legacy_unauthenticated_replay_blocked": True,
            "new_review_exact_retry": True,
        }

    if mode != "fresh":
        raise AcceptanceError("unsupported internal mode")

    direct_duplicates = assert_direct_duplicates_rejected(
        admin_url, repository, domain
    )

    actor, draft = create_fixture(repository, domain, "fresh-exact-concurrency")
    service = domain.ReviewService(repository, True)
    callers = 8
    barrier = Barrier(callers)

    def exact_review(_: int) -> dict[str, Any]:
        barrier.wait()
        result = service.review(
            draft["silver_draft_id"], actor, "Temperature 23 °C",
            "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
        )
        if result is None:
            raise AcceptanceError("approval returned no verified revision")
        return result

    with ThreadPoolExecutor(max_workers=callers) as executor:
        exact_results = list(executor.map(exact_review, range(callers)))
    if not all(item == exact_results[0] for item in exact_results):
        raise AcceptanceError("concurrent exact retries did not replay one stable result")
    with psycopg.connect(admin_url) as connection:
        exact_snapshot = review_snapshot(connection, draft["silver_draft_id"], draft["ingestion_id"])
    if exact_snapshot != {
        "draft_status": "REVIEWED", "upload_state": "VERIFIED", "decisions": 1,
        "verified": 1, "review_audits": 1, "final_state_audits": 1,
    }:
        raise AcceptanceError("concurrent exact retry evidence was not exactly once")

    conflict_actor, conflict_draft = create_fixture(repository, domain, "fresh-conflict")
    conflict_service = domain.ReviewService(repository, True)
    conflict_barrier = Barrier(2)

    def approve() -> str:
        conflict_barrier.wait()
        try:
            conflict_service.review(
                conflict_draft["silver_draft_id"], conflict_actor, "Temperature 23 °C",
                "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
            )
            return "accepted"
        except domain.StateConflict:
            return "conflict"

    def reject() -> str:
        conflict_barrier.wait()
        try:
            conflict_service.review(
                conflict_draft["silver_draft_id"], conflict_actor, "",
                "REJECTED_UNREADABLE", "Synthetic unreadable decision", True
            )
            return "accepted"
        except domain.StateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [executor.submit(approve), executor.submit(reject)]
        conflict_outcomes = sorted(future.result() for future in outcomes)
    if conflict_outcomes != ["accepted", "conflict"]:
        raise AcceptanceError("concurrent conflicting decisions did not resolve exactly once")
    with psycopg.connect(admin_url) as connection:
        conflict_snapshot = review_snapshot(
            connection, conflict_draft["silver_draft_id"], conflict_draft["ingestion_id"]
        )
    if conflict_snapshot["decisions"] != 1 or conflict_snapshot["review_audits"] != 1:
        raise AcceptanceError("conflicting review left non-unique decision or audit evidence")

    fault_evidence = []
    for checkpoint in (
        "decision", "draft", "verified", "review_audit", "enter_review", "final_state"
    ):
        fault_actor, fault_draft = create_fixture(repository, domain, f"fault-{checkpoint}")
        with psycopg.connect(admin_url) as connection:
            before = review_snapshot(
                connection, fault_draft["silver_draft_id"], fault_draft["ingestion_id"]
            )
            table, marker = install_fault(
                connection, checkpoint, fault_draft["silver_draft_id"], fault_draft["ingestion_id"]
            )
        try:
            domain.ReviewService(repository, True).review(
                fault_draft["silver_draft_id"], fault_actor, "Temperature 23 °C",
                "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
            )
        except Exception as exc:
            if marker not in str(exc):
                raise AcceptanceError(f"fault marker not observed at {checkpoint}") from exc
        else:
            raise AcceptanceError(f"fault injection did not fail at {checkpoint}")
        with psycopg.connect(admin_url) as connection:
            after = review_snapshot(
                connection, fault_draft["silver_draft_id"], fault_draft["ingestion_id"]
            )
            if after != before:
                raise AcceptanceError(f"partial review evidence survived fault at {checkpoint}")
            remove_fault(connection, checkpoint, table)
        recovered = domain.ReviewService(repository, True).review(
            fault_draft["silver_draft_id"], fault_actor, "Temperature 23 °C",
            "APPROVED_WITH_CORRECTIONS", "Corrected unit symbol", True
        )
        if recovered is None:
            raise AcceptanceError(f"review did not recover after fault at {checkpoint}")
        fault_evidence.append({"checkpoint": checkpoint, "marker_observed": True, "rollback_equal": True})
    with psycopg.connect(admin_url) as connection:
        connection.execute("DROP SCHEMA IF EXISTS m0r04_faults")
    return {
        "status": "passed",
        "internal_boundary": boundary,
        "catalog": catalog,
        "direct_sql_duplicate_rejection": direct_duplicates,
        "concurrent_exact_callers": callers,
        "concurrent_exact_snapshot": exact_snapshot,
        "conflicting_outcomes": conflict_outcomes,
        "conflicting_snapshot": conflict_snapshot,
        "faults": fault_evidence,
    }


def live() -> tuple[str, dict[str, Any]]:
    initial = inventory()
    postgres = image_id(POSTGRES_REF)
    api = image_id(API_REF)
    evidence: dict[str, Any] = {
        "images": {"postgres": postgres, "api": api},
        "initial_inventory": inventory_evidence(initial),
        "scenarios": [],
    }
    for kind in ("fresh", "upgraded"):
        evidence["scenarios"].append(scenario(kind, postgres, api))
    final = inventory()
    evidence["final_inventory"] = inventory_evidence(final)
    evidence["inventory_equal"] = final == initial
    if final != initial:
        raise IsolationBlocked("pre-existing Docker inventory changed during acceptance")
    return PASS, evidence


def _expect_exception(exception_type: type[BaseException], function: Any, message: str) -> None:
    try:
        function()
    except exception_type:
        return
    raise AcceptanceError(message)


def focused_regression_checks() -> dict[str, bool]:
    """Offline checks for authorization and finalizer fail-closed predicates."""

    project = "m0r04-fresh-0123456789"
    database = expected_database_name(project)
    capability = "a" * 64
    environment = {
        "M0R04_INTERNAL_CAPABILITY": capability,
        "M0R04_INTERNAL_PROJECT": project,
        "M0R04_INTERNAL_DATABASE": database,
        "REVIEW_DATABASE_URL": database_url("smartcoat_app", "synthetic", database),
        "REVIEW_ADMIN_DATABASE_URL": database_url("r04_admin", "synthetic", database),
    }
    connector_calls: list[str] = []

    class FakeResult:
        def __init__(self, row: Any) -> None:
            self.row = row

        def fetchone(self) -> Any:
            return self.row

    class FakeConnection:
        def __init__(self, row: Any) -> None:
            self.row = row

        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def execute(self, statement: str, parameters: Any = None) -> FakeResult:
            del parameters
            if statement == "SET TRANSACTION READ ONLY":
                return FakeResult(None)
            return FakeResult(self.row)

    def connector(row: Any) -> Any:
        def connect(url: str) -> FakeConnection:
            connector_calls.append(url)
            return FakeConnection(row)

        return connect

    missing = dict(environment)
    del missing["M0R04_INTERNAL_CAPABILITY"]
    for mode in ("seed-legacy", "fresh", "upgraded"):
        _expect_exception(
            IsolationBlocked,
            lambda selected=mode: authenticate_internal_boundary(
                selected, missing, connector(None)
            ),
            f"missing internal capability did not fail closed for {mode}",
        )
    if connector_calls:
        raise AcceptanceError("malformed internal authorization reached a database connector")

    wrong_host = dict(environment)
    wrong_host["REVIEW_ADMIN_DATABASE_URL"] = wrong_host[
        "REVIEW_ADMIN_DATABASE_URL"
    ].replace("@postgres/", "@arbitrary/", 1)
    _expect_exception(
        IsolationBlocked,
        lambda: authenticate_internal_boundary("fresh", wrong_host, connector(None)),
        "arbitrary internal database host did not fail closed",
    )
    if connector_calls:
        raise AcceptanceError("out-of-bound internal URL reached a database connector")

    wrong_database = dict(environment)
    wrong_database["M0R04_INTERNAL_DATABASE"] = "arbitrary"
    _expect_exception(
        IsolationBlocked,
        lambda: authenticate_internal_boundary("fresh", wrong_database, connector(None)),
        "mismatched internal database identity did not fail closed",
    )
    if connector_calls:
        raise AcceptanceError("mismatched internal identity reached a database connector")

    for mode in ("seed-legacy", "fresh", "upgraded"):
        _expect_exception(
            IsolationBlocked,
            lambda selected=mode: authenticate_internal_boundary(
                selected, environment, connector(None)
            ),
            f"unregistered internal capability did not fail closed for {mode}",
        )
    expected_row = (database, "r04_admin", 1)
    authenticated = authenticate_internal_boundary(
        "fresh", environment, connector(expected_row)
    )
    if authenticated["project"] != project:
        raise AcceptanceError("registered internal capability lost its project identity")

    original_project_resources = globals()["project_resources"]
    original_resource_exists = globals()["_resource_exists"]
    original_assert_owned = globals()["assert_owned"]
    original_remove_resource = globals()["_remove_resource"]
    removals: list[tuple[str, str]] = []
    enumerations = 0

    def fake_project_resources(_: str) -> dict[str, list[str]]:
        nonlocal enumerations
        enumerations += 1
        if enumerations == 1:
            return {
                "containers": ["owned-main", "owned-timeout-one-shot"],
                "networks": ["owned-network"],
                "volumes": ["owned-volume"],
            }
        return {"containers": [], "networks": [], "volumes": []}

    def fake_remove(kind: str, name: str) -> None:
        removals.append((kind, name))
        if name == "owned-timeout-one-shot":
            raise AcceptanceError("synthetic cleanup failure")

    persistent_resources = {
        "owned-main", "owned-timeout-one-shot", "owned-network", "owned-volume"
    }

    def fake_resource_exists(_kind: str, name: str) -> bool:
        return name in persistent_resources

    try:
        globals()["project_resources"] = fake_project_resources
        globals()["_resource_exists"] = fake_resource_exists
        globals()["assert_owned"] = lambda *_: None
        globals()["_remove_resource"] = (
            lambda kind, name: (
                fake_remove(kind, name), persistent_resources.discard(name)
            )
        )
        _expect_exception(
            IsolationBlocked,
            lambda: finalize(project, "owned-main", "owned-network", "owned-volume"),
            "partial finalizer failure did not fail closed",
        )
    finally:
        globals()["project_resources"] = original_project_resources
        globals()["_resource_exists"] = original_resource_exists
        globals()["assert_owned"] = original_assert_owned
        globals()["_remove_resource"] = original_remove_resource
    expected_removals = {
        ("container", "owned-main"),
        ("container", "owned-timeout-one-shot"),
        ("network", "owned-network"),
        ("volume", "owned-volume"),
    }
    if set(removals) != expected_removals:
        raise AcceptanceError("finalizer did not attempt every owned labeled resource")

    vanished_checks = 0

    def vanished_exists(_kind: str, _name: str) -> bool:
        nonlocal vanished_checks
        vanished_checks += 1
        return False

    vanished_enumerations = 0

    def vanished_project_resources(_: str) -> dict[str, list[str]]:
        nonlocal vanished_enumerations
        vanished_enumerations += 1
        if vanished_enumerations == 1:
            return {
                "containers": ["already-auto-removed"],
                "networks": [],
                "volumes": [],
            }
        return {"containers": [], "networks": [], "volumes": []}

    try:
        globals()["project_resources"] = vanished_project_resources
        globals()["_resource_exists"] = vanished_exists
        globals()["assert_owned"] = lambda *_: (_ for _ in ()).throw(
            AcceptanceError("ownership inspection must not run for a vanished target")
        )
        globals()["_remove_resource"] = lambda *_: (_ for _ in ()).throw(
            AcceptanceError("removal must not run for a vanished target")
        )
        finalize(project, "already-auto-removed", "absent-network", "absent-volume")
    finally:
        globals()["project_resources"] = original_project_resources
        globals()["_resource_exists"] = original_resource_exists
        globals()["assert_owned"] = original_assert_owned
        globals()["_remove_resource"] = original_remove_resource
    if vanished_checks == 0:
        raise AcceptanceError("vanished auto-remove target was not checked")

    return {
        "missing_capability_blocked_before_connect": True,
        "all_internal_modes_require_capability": True,
        "arbitrary_url_blocked_before_connect": True,
        "database_identity_mismatch_blocked_before_connect": True,
        "unregistered_capability_blocked": True,
        "registered_capability_authenticated": True,
        "timed_out_one_shot_enumerated": True,
        "cleanup_attempted_after_prior_failure": True,
        "cleanup_failure_classified_blocked_isolation": True,
        "vanished_auto_remove_target_tolerated": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    parser.add_argument("--internal", choices=("seed-legacy", "fresh", "upgraded"))
    parser.add_argument("--run-focused-regression-checks", action="store_true")
    arguments = parser.parse_args()
    if arguments.run_focused_regression_checks:
        try:
            print(json.dumps(focused_regression_checks(), sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "classification": type(exc).__name__}, sort_keys=True))
            return 1
    if arguments.internal:
        if not getattr(arguments, "confirm_disposable_synthetic_review_run"):
            print(BLOCKED_ISOLATION)
            return 2
        try:
            print(json.dumps(internal_verify(arguments.internal), sort_keys=True))
            return 0
        except Exception as exc:
            evidence = {"status": "failed", "classification": type(exc).__name__}
            if isinstance(exc, (AcceptanceError, AttributeError, KeyError, TypeError)):
                evidence["reason"] = str(exc)
            print(json.dumps(evidence, sort_keys=True))
            return 1
    if not getattr(arguments, "confirm_disposable_synthetic_review_run"):
        print(BLOCKED_ISOLATION)
        return 2
    try:
        status, evidence = live()
    except IsolationBlocked as exc:
        print(json.dumps({"status": BLOCKED_ISOLATION, "reason": str(exc)}, sort_keys=True))
        return 2
    except EnvironmentBlocked as exc:
        print(json.dumps({"status": BLOCKED_ENVIRONMENT, "reason": str(exc)}, sort_keys=True))
        return 3
    except Exception as exc:
        print(json.dumps({"status": FAIL_PRODUCT_CONTRACT, "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(evidence, sort_keys=True))
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
