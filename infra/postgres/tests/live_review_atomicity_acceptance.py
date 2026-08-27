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
from urllib.parse import quote


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


def finalize(project: str, container: str, network: str, volume: str) -> None:
    if docker("container", "inspect", container, check=False).returncode == 0:
        assert_owned("container", container, project)
        docker("rm", "-f", container)
    if docker("network", "inspect", network, check=False).returncode == 0:
        assert_owned("network", network, project)
        docker("network", "rm", network)
    if docker("volume", "inspect", volume, check=False).returncode == 0:
        assert_owned("volume", volume, project)
        docker("volume", "rm", volume)
    if any(project_resources(project).values()):
        raise IsolationBlocked("owned disposable resources remain after finalization")


def database_url(user: str, password: str, database: str) -> str:
    return f"postgresql://{quote(user)}:{quote(password)}@postgres/{quote(database)}"


def one_shot(
    *,
    project: str,
    suffix: str,
    network: str,
    image: str,
    environment: dict[str, str],
    command: list[str],
    mount: str,
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
    arguments.extend(["--mount", mount, image, *command])
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


def internal_command(
    project: str,
    network: str,
    api_image: str,
    app_url: str,
    admin_url: str,
    mode: str,
) -> dict[str, Any]:
    result = one_shot(
        project=project,
        suffix=f"verify-{mode}",
        network=network,
        image=api_image,
        environment={
            "REVIEW_DATABASE_URL": app_url,
            "REVIEW_ADMIN_DATABASE_URL": admin_url,
        },
        command=[
            "python",
            "/workspace/infra/postgres/tests/live_review_atomicity_acceptance.py",
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
    database = "smartcoat_r04"
    admin_user = "r04_admin"
    admin_password = secrets.token_hex(24)
    app_password = secrets.token_hex(24)
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
        if kind == "upgraded":
            evidence["legacy_seed"] = internal_command(
                project, network, api_image, app_url, admin_url, "seed-legacy"
            )
        migration_command(project, network, api_image, admin_url, "apply", database)
        evidence["verification"] = internal_command(
            project, network, api_image, app_url, admin_url, kind
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
        row[0]
        for row in connection.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname IN (
                'review_decisions_one_per_draft_uidx',
                'silver_verified_records_one_per_decision_uidx'
              )
            """
        ).fetchall()
    }
    expected_indexes = {
        "review_decisions_one_per_draft_uidx",
        "silver_verified_records_one_per_decision_uidx",
    }
    if indexes != expected_indexes:
        raise AcceptanceError("M0-R04 unique-index contract is incomplete")
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
    return {"unique_indexes": sorted(indexes), "constraint_validation": constraints}


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

    sys.path.insert(0, "/workspace/apps/api/src")
    import domain
    from database import PostgresRepository

    app_url = os.environ["REVIEW_DATABASE_URL"]
    admin_url = os.environ["REVIEW_ADMIN_DATABASE_URL"]
    if mode == "seed-legacy":
        return {"status": "passed", **seed_legacy(admin_url)}

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
            "catalog": catalog,
            "legacy_preserved": True,
            "legacy_unauthenticated_replay_blocked": True,
            "new_review_exact_retry": True,
        }

    if mode != "fresh":
        raise AcceptanceError("unsupported internal mode")

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
        "catalog": catalog,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    parser.add_argument("--internal", choices=("seed-legacy", "fresh", "upgraded"))
    arguments = parser.parse_args()
    if arguments.internal:
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
