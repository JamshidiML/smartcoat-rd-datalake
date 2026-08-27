#!/usr/bin/env python3
"""Opt-in M0-R01.4.2a concurrent migration-lock acceptance.

This script is deliberately excluded from ``test_*.py`` discovery. Run it only
with the explicit synthetic-live authorization flag::

    python3 infra/postgres/tests/live_migration_lock_acceptance.py \
        --confirm-disposable-synthetic-lock-run
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
ACCEPTED_HARNESS_PATH = (
    ROOT / "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
)
MIGRATION_RUNNER_PATH = ROOT / "infra/postgres/migrate.py"
ACCEPTED_HARNESS_SHA256 = (
    "4d7fbe8d33d36b6ff50161f4374cf16477667903253b790cbc37cb3e54707cfd"
)

RESULT_PASS = "PASS_M0_R01_4_2A"
RESULT_PRODUCT_FAILURE = "FAIL_PRODUCT_CONTRACT"
RESULT_HARNESS_FAILURE = "FAIL_VERIFICATION_HARNESS"
RESULT_ISOLATION_BLOCKED = "BLOCKED_ISOLATION"
RESULT_ENVIRONMENT_BLOCKED = "BLOCKED_ENVIRONMENT"
RESULT_BOUNDARY_BLOCKED = "BLOCKED_IMPLEMENTATION_BOUNDARY"

EXPECTED_MIGRATION_LOCK_KEY = int.from_bytes(
    b"SCMIGR01", byteorder="big", signed=False
)
GATE_LOCK_KEY = int.from_bytes(b"SCGATE01", byteorder="big", signed=False)
LOCK_MASK_32 = (1 << 32) - 1
LOCK_OBSERVATION_TIMEOUT_SECONDS = 20.0
COMPETING_RUNNER_TIMEOUT_SECONDS = 15
PRIMARY_COMPLETION_TIMEOUT_SECONDS = 30
CHILD_SHUTDOWN_TIMEOUT_SECONDS = 8
POLL_INTERVAL_SECONDS = 0.25

PENDING_MIGRATION_FILENAME = "0002__live_lock_acceptance_probe.sql"
PENDING_MIGRATION_NAME = "live_lock_acceptance_probe"
PROBE_SCHEMA = "m0r0142a_acceptance_probe"
PROBE_TABLE = "commit_probe"
PROBE_ID = "m0-r01-4-2a-lock-probe"
PROBE_VALUE = "synthetic-concurrent-lock-migration-applied"
PROBE_TIMESTAMP = "2026-01-02 00:00:00+00"

EXPECTED_REJECTION_LINE = (
    "Migration error: Another migration runner holds the PostgreSQL advisory lock"
)

PROTECTED_HASHES = {
    "infra/postgres/tests/live_migration_lifecycle_acceptance.py": (
        ACCEPTED_HARNESS_SHA256
    ),
    "infra/postgres/bootstrap_contract.py": (
        "755607066df9439c45d98d059e3f3786ef08dcccf33dec250aeb0cd93e1b5c69"
    ),
    "infra/postgres/migrate.py": (
        "d5564df134e092c70157edb4fb3b5d42c016ae7aa4909574dd8695feced0508f"
    ),
    "infra/postgres/tests/test_migrate.py": (
        "2fcfe8922e3120607293e65ae96e2a732826d186a870713691993136f7e765f9"
    ),
    "infra/postgres/migrations/0001__validate_bootstrap_prerequisites.sql": (
        "7f34c9aba3819a49a5bb6c83f75bceaf436009d36c1c62eb46d0ddfa425529e5"
    ),
    "infra/postgres/init.sql": (
        "9733855250e800c9a1e44f90d72711b8af49d8c940e89c6751020c1c5292e272"
    ),
    "compose.yaml": (
        "464902d8c47a82feaee43e3c2611141f800cab32c3501dda406d4763a044e5f1"
    ),
    ".env.example": (
        "4c2a8509dda86440f8cc264e997b207ec0309dedec4ebdc7989f4cca70b3a80d"
    ),
    "docs/runbooks/VPS_DEPLOYMENT.md": (
        "439104976eafbc966073e7bc05f41818ba777ff2b5b60a0daaffd211fda07d3d"
    ),
    "infra/postgres/tests/test_migration_operations.py": (
        "fc558ea2ad4a84a716c51173d1cfff8300ab196bc70afa38c54bd22c14d5ba24"
    ),
    "docs/architecture/decisions/ADR-0001-master-roadmap-v2-scope-expansion-sequencing.md": (
        "afb78304621b383c2e187698beaaf0017037fa7c450063f326a05a9f71e5eaeb"
    ),
    "docs/architecture/decisions/ADR-0002-retention-semantics-and-enforcement-contract.md": (
        "307ce9d9484b3819d16c5178a3dc61fb56e257376779e679e4923b1e7f5beb37"
    ),
    "docs/architecture/M0_CONTRACT_FREEZE_ACCEPTANCE_MATRIX.md": (
        "cece377662dcb5224fa70226e4200f14017745615a6b31024c792cdc9d33de12"
    ),
}

REQUIRED_HARNESS_SYMBOLS = {
    "LiveMigrationLifecycleAcceptance",
    "ProductContractFailure",
    "IsolationBlocked",
    "EnvironmentBlocked",
    "finalize_harness",
    "PUBLIC_CATALOG_QUERIES",
}


class LockAcceptanceError(RuntimeError):
    result = RESULT_HARNESS_FAILURE


class ProductContractFailure(LockAcceptanceError):
    result = RESULT_PRODUCT_FAILURE


class VerificationHarnessFailure(LockAcceptanceError):
    result = RESULT_HARNESS_FAILURE


class IsolationBlocked(LockAcceptanceError):
    result = RESULT_ISOLATION_BLOCKED


class EnvironmentBlocked(LockAcceptanceError):
    result = RESULT_ENVIRONMENT_BLOCKED


class ImplementationBoundaryBlocked(LockAcceptanceError):
    result = RESULT_BOUNDARY_BLOCKED


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(
    condition: bool,
    message: str,
    error_type: type[LockAcceptanceError] = VerificationHarnessFailure,
) -> None:
    if not condition:
        raise error_type(message)


def git_output(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    require(
        completed.returncode == 0,
        f"Git repository evidence failed: {' '.join(arguments)}",
        EnvironmentBlocked,
    )
    return completed.stdout


def verify_protected_hashes(
    *,
    root: Path = ROOT,
    expected_hashes: dict[str, str] = PROTECTED_HASHES,
) -> dict[str, str]:
    observed: dict[str, str] = {}
    for relative_path, expected in expected_hashes.items():
        path = root / relative_path
        require(
            path.is_file(),
            f"Protected path is unavailable: {relative_path}",
            ImplementationBoundaryBlocked,
        )
        try:
            actual = sha256_path(path)
        except OSError as exc:
            raise ImplementationBoundaryBlocked(
                f"Protected path cannot be verified: {relative_path}: "
                f"{type(exc).__name__}"
            ) from exc
        require(
            actual == expected,
            f"Protected path hash differs from the accepted boundary: {relative_path}",
            ImplementationBoundaryBlocked,
        )
        observed[relative_path] = actual
    return observed


def verify_post_execution_protected_hashes(
    preflight_hashes: dict[str, str],
    *,
    root: Path = ROOT,
    expected_hashes: dict[str, str] = PROTECTED_HASHES,
    accepted_harness_relative_path: str | None = (
        "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
    ),
    accepted_harness_sha256: str = ACCEPTED_HARNESS_SHA256,
) -> dict[str, str]:
    for relative_path, expected in expected_hashes.items():
        require(
            preflight_hashes.get(relative_path) == expected,
            f"Protected preflight evidence is missing or changed: {relative_path}",
            ImplementationBoundaryBlocked,
        )
    require(
        set(preflight_hashes) == set(expected_hashes),
        "Protected preflight evidence contains an unexpected path set",
        ImplementationBoundaryBlocked,
    )
    observed = verify_protected_hashes(
        root=root,
        expected_hashes=expected_hashes,
    )
    for relative_path, before in preflight_hashes.items():
        require(
            observed.get(relative_path) == before,
            f"Protected path changed after preflight: {relative_path}",
            ImplementationBoundaryBlocked,
        )
    if accepted_harness_relative_path is not None:
        require(
            observed.get(accepted_harness_relative_path)
            == accepted_harness_sha256,
            "Accepted M0-R01.4.1 harness changed after execution",
            ImplementationBoundaryBlocked,
        )
    return observed


def repository_evidence() -> dict[str, Any]:
    return {
        "root": str(ROOT),
        "branch": git_output("branch", "--show-current").strip(),
        "head": git_output("rev-parse", "HEAD").strip(),
        "status_short": git_output(
            "status", "--short", "--untracked-files=all"
        ).splitlines(),
    }


def load_accepted_harness() -> ModuleType:
    require(
        sha256_path(ACCEPTED_HARNESS_PATH) == ACCEPTED_HARNESS_SHA256,
        "Accepted M0-R01.4.1 harness hash changed",
        ImplementationBoundaryBlocked,
    )
    spec = importlib.util.spec_from_file_location(
        "m0_r01_4_1_accepted_harness", ACCEPTED_HARNESS_PATH
    )
    require(
        spec is not None and spec.loader is not None,
        "Accepted M0-R01.4.1 harness cannot be imported",
        ImplementationBoundaryBlocked,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    missing = sorted(
        symbol for symbol in REQUIRED_HARNESS_SYMBOLS if not hasattr(module, symbol)
    )
    require(
        not missing,
        f"Accepted harness reuse boundary lacks required symbols: {missing}",
        ImplementationBoundaryBlocked,
    )
    return module


def read_and_verify_migration_lock_key() -> dict[str, Any]:
    source = MIGRATION_RUNNER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MIGRATION_RUNNER_PATH))
    assignment: ast.AST | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "ADVISORY_LOCK_KEY"
            for target in node.targets
        ):
            assignment = node.value
            break
    require(
        isinstance(assignment, ast.Call),
        "Migration advisory-lock key assignment was not found",
        ImplementationBoundaryBlocked,
    )
    call = assignment
    require(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "int"
        and call.func.attr == "from_bytes"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == b"SCMIGR01",
        "Migration advisory-lock key no longer uses the accepted byte source",
        ImplementationBoundaryBlocked,
    )
    keywords = {item.arg: ast.literal_eval(item.value) for item in call.keywords}
    require(
        keywords == {"byteorder": "big", "signed": False},
        "Migration advisory-lock key byte-order contract changed",
        ImplementationBoundaryBlocked,
    )
    observed = int.from_bytes(
        call.args[0].value,
        byteorder=keywords["byteorder"],
        signed=keywords["signed"],
    )
    require(
        observed == EXPECTED_MIGRATION_LOCK_KEY,
        "Migration advisory-lock key differs from the independently expected value",
        ImplementationBoundaryBlocked,
    )
    require(
        GATE_LOCK_KEY != observed,
        "Synthetic gate-lock key collides with the migration lock",
        VerificationHarnessFailure,
    )
    return {
        "source_expression": (
            'int.from_bytes(b"SCMIGR01", byteorder="big", signed=False)'
        ),
        "observed": observed,
        "expected": EXPECTED_MIGRATION_LOCK_KEY,
        "synthetic_gate": GATE_LOCK_KEY,
    }


def advisory_lock_parts(key: int) -> dict[str, int]:
    require(
        0 <= key < (1 << 63),
        "Advisory-lock key is outside the accepted positive bigint range",
    )
    return {
        "classid": (key >> 32) & LOCK_MASK_32,
        "objid": key & LOCK_MASK_32,
        "objsubid": 1,
    }


def pending_migration_content() -> bytes:
    return f"""SELECT pg_advisory_xact_lock({GATE_LOCK_KEY});

CREATE SCHEMA {PROBE_SCHEMA};

CREATE TABLE {PROBE_SCHEMA}.{PROBE_TABLE} (
    probe_id text PRIMARY KEY,
    probe_value text NOT NULL,
    observed_at_utc timestamptz NOT NULL
);

INSERT INTO {PROBE_SCHEMA}.{PROBE_TABLE} (
    probe_id,
    probe_value,
    observed_at_utc
) VALUES (
    '{PROBE_ID}',
    '{PROBE_VALUE}',
    TIMESTAMPTZ '2026-01-02T00:00:00Z'
);
""".encode("utf-8")


class LiveMigrationLockAcceptance:
    def __init__(self, accepted: ModuleType) -> None:
        self.accepted = accepted
        self.harness = accepted.LiveMigrationLifecycleAcceptance()
        self.controller_process: subprocess.Popen[str] | None = None
        self.primary_process: subprocess.Popen[str] | None = None
        self.controller_recorded = False
        self.primary_recorded = False
        self.controller_backend_pid: int | None = None
        self.primary_backend_pid: int | None = None
        self.controller_application_name = f"{self.harness.project}-gate-controller"
        self.pending_checksum = ""
        self.observations: list[dict[str, Any]] = []

    def _record_child(
        self,
        label: str,
        process: subprocess.Popen[str],
        arguments: list[str],
        stdout: str,
        stderr: str,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.CompletedProcess(
            arguments,
            process.returncode if process.returncode is not None else -1,
            stdout,
            stderr,
        )
        self.harness._assert_no_secret_output(completed)
        self.harness._record_command(
            label,
            arguments,
            completed,
            input_from_stdin=label == "gate_controller_session",
        )
        return completed

    def _start_child(
        self, label: str, arguments: list[str], *, stdin: bool
    ) -> subprocess.Popen[str]:
        sanitized = self.harness._sanitized_arguments(arguments)
        process = subprocess.Popen(
            arguments,
            cwd=ROOT,
            env=self.harness.docker_environment,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.harness.evidence.setdefault("child_processes", {})[label] = {
            "arguments": sanitized,
            "local_pid": process.pid,
            "stdin_managed": stdin,
        }
        return process

    def _controller_arguments(self) -> list[str]:
        return self.harness._compose_arguments(
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "-X",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            self.harness.admin_user,
            "--dbname",
            self.harness.database_name,
            "--no-align",
            "--tuples-only",
        )

    def _primary_arguments(self) -> list[str]:
        return self.harness._compose_arguments(
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "postgres-migrate",
        )

    def _write_controller(self, sql: str) -> None:
        require(
            self.controller_process is not None
            and self.controller_process.poll() is None
            and self.controller_process.stdin is not None,
            "Controller child is not available for cooperative input",
            VerificationHarnessFailure,
        )
        try:
            self.controller_process.stdin.write(sql)
            self.controller_process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise VerificationHarnessFailure(
                "Controller child closed before cooperative gate handling"
            ) from exc

    def _start_controller(self) -> None:
        arguments = self._controller_arguments()
        self.controller_process = self._start_child(
            "gate_controller", arguments, stdin=True
        )
        application_name = self.controller_application_name.replace("'", "''")
        self._write_controller(
            f"SET application_name = '{application_name}';\n"
            f"SELECT pg_advisory_lock({GATE_LOCK_KEY});\n"
        )

    def _start_primary(self) -> None:
        arguments = self._primary_arguments()
        self.primary_process = self._start_child(
            "primary_migration_runner", arguments, stdin=False
        )

    def _complete_controller(self) -> subprocess.CompletedProcess[str]:
        require(
            self.controller_process is not None,
            "Controller process was never started",
        )
        self._write_controller(
            f"SELECT pg_advisory_unlock({GATE_LOCK_KEY});\n\\q\n"
        )
        try:
            self.controller_process.wait(timeout=CHILD_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise VerificationHarnessFailure(
                "Controller did not close after cooperative gate release"
            ) from exc
        stdout = (
            self.controller_process.stdout.read()
            if self.controller_process.stdout is not None
            else ""
        )
        stderr = (
            self.controller_process.stderr.read()
            if self.controller_process.stderr is not None
            else ""
        )
        completed = self._record_child(
            "gate_controller_session",
            self.controller_process,
            self._controller_arguments(),
            stdout,
            stderr,
        )
        self.controller_recorded = True
        require(
            completed.returncode == 0,
            "Controller session failed during cooperative gate release",
            VerificationHarnessFailure,
        )
        return completed

    def _complete_primary(self) -> subprocess.CompletedProcess[str]:
        require(
            self.primary_process is not None,
            "Primary migration process was never started",
        )
        try:
            stdout, stderr = self.primary_process.communicate(
                timeout=PRIMARY_COMPLETION_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            raise ProductContractFailure(
                "Primary migration runner did not complete after gate release"
            ) from exc
        completed = self._record_child(
            "primary_migration_runner",
            self.primary_process,
            self._primary_arguments(),
            stdout,
            stderr,
        )
        self.primary_recorded = True
        return completed

    def _lock_rows(self, label: str) -> list[dict[str, Any]]:
        migration = advisory_lock_parts(EXPECTED_MIGRATION_LOCK_KEY)
        gate = advisory_lock_parts(GATE_LOCK_KEY)
        return self.harness._psql_rows(
            label,
            f"""
                SELECT l.pid, l.granted, l.mode,
                       l.classid::bigint AS classid,
                       l.objid::bigint AS objid,
                       l.objsubid,
                       a.application_name, a.state,
                       COALESCE(a.wait_event_type, '') AS wait_event_type,
                       COALESCE(a.wait_event, '') AS wait_event
                FROM pg_locks AS l
                JOIN pg_stat_activity AS a ON a.pid = l.pid
                WHERE l.locktype = 'advisory'
                  AND l.database = (
                      SELECT oid FROM pg_database WHERE datname = current_database()
                  )
                  AND (
                      (l.classid = {migration['classid']}::oid
                       AND l.objid = {migration['objid']}::oid
                       AND l.objsubid = {migration['objsubid']})
                      OR
                      (l.classid = {gate['classid']}::oid
                       AND l.objid = {gate['objid']}::oid
                       AND l.objsubid = {gate['objsubid']})
                  )
                ORDER BY l.pid, l.classid, l.objid, l.granted DESC
            """,
        )

    @staticmethod
    def _matches_key(row: dict[str, Any], key: int) -> bool:
        parts = advisory_lock_parts(key)
        return (
            int(row["classid"]) == parts["classid"]
            and int(row["objid"]) == parts["objid"]
            and int(row["objsubid"]) == parts["objsubid"]
        )

    def _poll(
        self,
        label: str,
        predicate: Callable[[list[dict[str, Any]]], Any],
    ) -> tuple[Any, list[dict[str, Any]], float, int]:
        started = time.monotonic()
        deadline = started + LOCK_OBSERVATION_TIMEOUT_SECONDS
        attempts = 0
        last_rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            attempts += 1
            last_rows = self._lock_rows(f"{label}:poll:{attempts}")
            value = predicate(last_rows)
            if value:
                duration = time.monotonic() - started
                self.observations.append(
                    {
                        "label": label,
                        "duration_seconds": round(duration, 6),
                        "attempts": attempts,
                        "lock_rows": last_rows,
                    }
                )
                return value, last_rows, duration, attempts
            time.sleep(POLL_INTERVAL_SECONDS)
        raise VerificationHarnessFailure(
            f"Bounded PostgreSQL lock observation timed out: {label}; rows={last_rows}"
        )

    def _controller_lock_predicate(
        self, rows: list[dict[str, Any]]
    ) -> int | None:
        matches = [
            row
            for row in rows
            if self._matches_key(row, GATE_LOCK_KEY)
            and row["granted"] is True
            and row["application_name"] == self.controller_application_name
        ]
        if len(matches) != 1:
            return None
        return int(matches[0]["pid"])

    def _primary_blocked_predicate(
        self, rows: list[dict[str, Any]]
    ) -> int | None:
        granted_migration = [
            row
            for row in rows
            if self._matches_key(row, EXPECTED_MIGRATION_LOCK_KEY)
            and row["granted"] is True
        ]
        waiting_gate = [
            row
            for row in rows
            if self._matches_key(row, GATE_LOCK_KEY)
            and row["granted"] is False
        ]
        if len(granted_migration) != 1 or len(waiting_gate) != 1:
            return None
        migration_pid = int(granted_migration[0]["pid"])
        gate_pid = int(waiting_gate[0]["pid"])
        if (
            migration_pid != gate_pid
            or migration_pid == self.controller_backend_pid
            or waiting_gate[0]["state"] != "active"
            or waiting_gate[0]["wait_event_type"] != "Lock"
            or waiting_gate[0]["wait_event"] != "advisory"
        ):
            return None
        return migration_pid

    def _assert_exact_lock_state(
        self, rows: list[dict[str, Any]], *, label: str
    ) -> None:
        require(
            self.controller_backend_pid is not None
            and self.primary_backend_pid is not None,
            f"Backend identities were not established before {label}",
        )
        require(
            len(rows) == 3,
            f"Expected exactly three monitored advisory-lock rows during {label}",
            ProductContractFailure,
        )
        require(
            {int(row["pid"]) for row in rows}
            == {self.controller_backend_pid, self.primary_backend_pid},
            f"An unexpected PostgreSQL backend participated during {label}",
            ProductContractFailure,
        )
        controller = [
            row
            for row in rows
            if int(row["pid"]) == self.controller_backend_pid
            and self._matches_key(row, GATE_LOCK_KEY)
            and row["granted"] is True
            and row["mode"] == "ExclusiveLock"
            and row["application_name"] == self.controller_application_name
        ]
        primary_migration = [
            row
            for row in rows
            if int(row["pid"]) == self.primary_backend_pid
            and self._matches_key(row, EXPECTED_MIGRATION_LOCK_KEY)
            and row["granted"] is True
            and row["mode"] == "ExclusiveLock"
        ]
        primary_gate = [
            row
            for row in rows
            if int(row["pid"]) == self.primary_backend_pid
            and self._matches_key(row, GATE_LOCK_KEY)
            and row["granted"] is False
            and row["mode"] == "ExclusiveLock"
            and row["state"] == "active"
            and row["wait_event_type"] == "Lock"
            and row["wait_event"] == "advisory"
        ]
        require(
            len(controller) == 1
            and len(primary_migration) == 1
            and len(primary_gate) == 1,
            f"Exact controller/primary PostgreSQL lock state changed during {label}",
            ProductContractFailure,
        )

    def _probe_state(self, label: str) -> dict[str, Any]:
        existence = self.harness._psql_rows(
            f"{label}:existence",
            f"""
                SELECT to_regnamespace('{PROBE_SCHEMA}') IS NOT NULL AS schema_exists,
                       to_regclass('{PROBE_SCHEMA}.{PROBE_TABLE}') IS NOT NULL AS table_exists
            """,
        )
        require(len(existence) == 1, "Probe existence query was incomplete")
        state: dict[str, Any] = dict(existence[0])
        if state["table_exists"]:
            state["rows"] = self.harness._psql_rows(
                f"{label}:rows",
                f"""
                    SELECT probe_id, probe_value,
                           observed_at_utc::text AS observed_at_utc
                    FROM {PROBE_SCHEMA}.{PROBE_TABLE}
                    ORDER BY probe_id
                """,
            )
        else:
            state["rows"] = []
        return state

    @staticmethod
    def _require_probe_absent(state: dict[str, Any], label: str) -> None:
        require(
            state
            == {"schema_exists": False, "table_exists": False, "rows": []},
            f"Synthetic version-2 probe became visible before commit: {label}",
            ProductContractFailure,
        )

    @staticmethod
    def _require_probe_committed(state: dict[str, Any]) -> None:
        require(
            state
            == {
                "schema_exists": True,
                "table_exists": True,
                "rows": [
                    {
                        "probe_id": PROBE_ID,
                        "probe_value": PROBE_VALUE,
                        "observed_at_utc": PROBE_TIMESTAMP,
                    }
                ],
            },
            "Committed synthetic lock probe differs from the expected effect",
            ProductContractFailure,
        )

    def _write_pending_fixture(self) -> str:
        content = pending_migration_content()
        path = self.harness.migration_fixture_directory / PENDING_MIGRATION_FILENAME
        require(
            not path.exists(),
            "Synthetic lock migration unexpectedly exists before controlled creation",
        )
        path.write_bytes(content)
        path.chmod(0o400)
        self.harness.pending_migration_fixture = path
        self.harness.pending_migration_sha256 = hashlib.sha256(content).hexdigest()
        self.harness._assert_migration_fixture_host_ownership(expect_pending=True)
        require(
            path.read_bytes() == content,
            "Synthetic lock migration bytes changed after fixture creation",
        )
        self.pending_checksum = self.harness.pending_migration_sha256
        return self.pending_checksum

    def _verify_sql_lock_parts(self) -> None:
        migration = advisory_lock_parts(EXPECTED_MIGRATION_LOCK_KEY)
        gate = advisory_lock_parts(GATE_LOCK_KEY)
        rows = self.harness._psql_rows(
            "independent_lock_key_bit_layout",
            f"""
                SELECT {EXPECTED_MIGRATION_LOCK_KEY}::bigint AS migration_key,
                       ({EXPECTED_MIGRATION_LOCK_KEY}::bigint >> 32)::bigint
                           AS migration_classid,
                       ({EXPECTED_MIGRATION_LOCK_KEY}::bigint & {LOCK_MASK_32})::bigint
                           AS migration_objid,
                       {GATE_LOCK_KEY}::bigint AS gate_key,
                       ({GATE_LOCK_KEY}::bigint >> 32)::bigint AS gate_classid,
                       ({GATE_LOCK_KEY}::bigint & {LOCK_MASK_32})::bigint AS gate_objid
            """,
        )
        require(
            rows
            == [
                {
                    "migration_key": EXPECTED_MIGRATION_LOCK_KEY,
                    "migration_classid": migration["classid"],
                    "migration_objid": migration["objid"],
                    "gate_key": GATE_LOCK_KEY,
                    "gate_classid": gate["classid"],
                    "gate_objid": gate["objid"],
                }
            ],
            "PostgreSQL bigint split differs from independently derived lock parts",
            VerificationHarnessFailure,
        )
        self.harness.evidence["advisory_lock_keys"] = {
            "migration": {
                "key": EXPECTED_MIGRATION_LOCK_KEY,
                **migration,
            },
            "gate": {"key": GATE_LOCK_KEY, **gate},
            "postgres_bigint_split_verified": True,
        }

    def _assert_baseline_state(
        self,
        ledger: list[dict[str, Any]],
        adoption: list[dict[str, Any]],
        database_oid: int,
    ) -> None:
        self.harness._assert_adoption_rows(ledger, adoption, database_oid)
        require(len(ledger) == 1 and len(adoption) == 1, "Baseline state is incomplete")

    def _assert_final_ledger(
        self,
        ledger: list[dict[str, Any]],
        baseline: list[dict[str, Any]],
    ) -> None:
        require(
            len(ledger) == 2 and ledger[0] == baseline[0],
            "Final ledger did not retain exactly one unchanged baseline row",
            ProductContractFailure,
        )
        pending = ledger[1]
        require(
            pending["version"] == 2
            and pending["name"] == PENDING_MIGRATION_NAME
            and pending["sha256"] == self.pending_checksum
            and bool(pending["applied_at_utc"])
            and pending["applied_by"] == self.harness.admin_user,
            "Version-2 lock migration ledger evidence is incorrect",
            ProductContractFailure,
        )

    def preflight(self, static_evidence: dict[str, Any]) -> None:
        self.harness.evidence["repository"] = static_evidence["repository"]
        self.harness.evidence["protected_hashes_before"] = static_evidence[
            "protected_hashes"
        ]
        self.harness.evidence["migration_lock_source"] = static_evidence[
            "migration_lock_source"
        ]
        self.harness.preflight()

    def lifecycle(self) -> None:
        h = self.harness
        h._install_cleanup_handlers()
        h.state_change_attempted = True
        h._verify_migration_execution_image_boundary()
        started = h._compose(
            "start_isolated_postgres",
            "up",
            "--detach",
            "--no-deps",
            "--pull",
            "never",
            "postgres",
            timeout=180,
        )
        require(
            started.returncode == 0,
            "Isolated PostgreSQL did not start from the accepted local image",
            EnvironmentBlocked,
        )
        container_id = h._wait_for_postgres()
        h._verify_running_postgres_isolation(container_id)

        public_before = h._catalog_snapshot(
            "public_catalog_before_lock_acceptance",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        h._assert_public_bootstrap(public_before)
        public_fingerprint = self.accepted.canonical_fingerprint(public_before)
        business_before = h._business_snapshot("business_before_lock_acceptance")
        require(
            sum(len(rows) for rows in business_before.values()) == 0,
            "Fresh synthetic database unexpectedly contains application rows",
            ProductContractFailure,
        )
        require(
            h._metadata_state("metadata_before_lock_adoption") == [False] * 6,
            "Fresh synthetic database unexpectedly contains migration metadata",
            ProductContractFailure,
        )

        adoption_result = h._run_migration(
            "explicit_adoption_for_lock_acceptance",
            "adopt",
            h.database_name,
        )
        require(
            adoption_result.returncode == 0
            and "status=ADOPTED" in adoption_result.stdout
            and "evidence_inserted=true" in adoption_result.stdout,
            "Explicit adoption failed before concurrent-lock verification",
            ProductContractFailure,
        )
        identity = h._psql_rows(
            "database_identity_for_lock_acceptance",
            """
                SELECT current_database() AS database_name,
                       d.oid::bigint AS database_oid,
                       current_user AS database_user
                FROM pg_database AS d
                WHERE d.datname = current_database()
            """,
        )
        require(
            len(identity) == 1
            and identity[0]["database_name"] == h.database_name
            and identity[0]["database_user"] == h.admin_user,
            "Independent adopted database identity is incorrect",
            ProductContractFailure,
        )
        database_oid = int(identity[0]["database_oid"])
        baseline_ledger = h._ledger_rows("baseline_ledger_before_lock_test")
        baseline_adoption = h._adoption_rows("adoption_evidence_before_lock_test")
        self._assert_baseline_state(
            baseline_ledger,
            baseline_adoption,
            database_oid,
        )
        metadata_after_adoption = h._catalog_snapshot(
            "metadata_catalog_before_lock_test",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        h._assert_metadata_contract(metadata_after_adoption)
        self._verify_sql_lock_parts()
        pending_checksum = self._write_pending_fixture()

        self._start_controller()
        controller_pid, controller_rows, _, _ = self._poll(
            "controller_granted_gate_lock",
            self._controller_lock_predicate,
        )
        self.controller_backend_pid = int(controller_pid)
        require(
            self.controller_process is not None
            and self.controller_process.poll() is None,
            "Controller child exited after acquiring the synthetic gate",
            VerificationHarnessFailure,
        )

        self._start_primary()
        primary_pid, primary_rows, _, _ = self._poll(
            "primary_holds_migration_and_waits_gate",
            self._primary_blocked_predicate,
        )
        self.primary_backend_pid = int(primary_pid)
        require(
            self.primary_process is not None and self.primary_process.poll() is None,
            "Primary migration child exited before lock contention",
            ProductContractFailure,
        )
        self._assert_exact_lock_state(primary_rows, label="pre-competition")

        ledger_before_competitor = h._ledger_rows("ledger_before_competing_runner")
        adoption_before_competitor = h._adoption_rows(
            "adoption_before_competing_runner"
        )
        probe_before_competitor = self._probe_state("probe_before_competing_runner")
        public_before_competitor = h._catalog_snapshot(
            "public_before_competing_runner",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        business_before_competitor = h._business_snapshot(
            "business_before_competing_runner"
        )
        require(
            ledger_before_competitor == baseline_ledger
            and adoption_before_competitor == baseline_adoption,
            "Primary runner changed baseline evidence before the gate opened",
            ProductContractFailure,
        )
        self._require_probe_absent(probe_before_competitor, "before competitor")

        competitor_started = time.monotonic()
        competing = h._compose(
            "competing_apply_while_primary_blocked",
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "postgres-migrate",
            timeout=COMPETING_RUNNER_TIMEOUT_SECONDS,
        )
        competitor_duration = time.monotonic() - competitor_started
        rejection_lines = [
            line.strip()
            for line in (competing.stdout + "\n" + competing.stderr).splitlines()
            if line.strip().startswith("Migration error:")
        ]
        require(
            competing.returncode != 0
            and rejection_lines == [EXPECTED_REJECTION_LINE]
            and competitor_duration < COMPETING_RUNNER_TIMEOUT_SECONDS,
            "Competing runner did not return the exact bounded concurrent-lock rejection",
            ProductContractFailure,
        )

        post_rows = self._lock_rows("locks_after_competing_rejection")
        self._assert_exact_lock_state(post_rows, label="post-competition")
        require(
            self.primary_process.poll() is None,
            "Primary migration child exited after competing rejection",
            ProductContractFailure,
        )
        ledger_after_rejection = h._ledger_rows("ledger_after_competing_rejection")
        adoption_after_rejection = h._adoption_rows(
            "adoption_after_competing_rejection"
        )
        probe_after_rejection = self._probe_state(
            "probe_after_competing_rejection"
        )
        public_after_rejection = h._catalog_snapshot(
            "public_after_competing_rejection",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        business_after_rejection = h._business_snapshot(
            "business_after_competing_rejection"
        )
        require(
            ledger_after_rejection == ledger_before_competitor
            and adoption_after_rejection == adoption_before_competitor
            and public_after_rejection == public_before_competitor
            and business_after_rejection == business_before_competitor,
            "Competing rejection changed database evidence or application state",
            ProductContractFailure,
        )
        self._require_probe_absent(probe_after_rejection, "after competitor")
        h.evidence["lock_preservation"] = {
            "status": "PRIMARY_LOCK_PRESERVED_AFTER_COMPETING_REJECTION",
            "controller_backend_pid": self.controller_backend_pid,
            "primary_backend_pid": self.primary_backend_pid,
            "controller_lock_rows_initial": controller_rows,
            "primary_lock_rows_initial": primary_rows,
            "lock_rows_after_rejection": post_rows,
            "competing_exit": competing.returncode,
            "competing_duration_seconds": round(competitor_duration, 6),
            "accepted_rejection": EXPECTED_REJECTION_LINE,
            "scope_note": (
                "Live evidence proves the rejected runner did not release the primary "
                "runner's lock; accepted offline control-flow tests cover absence of a "
                "spurious unlock call before acquisition."
            ),
        }

        self._complete_controller()
        primary = self._complete_primary()
        require(
            primary.returncode == 0
            and "discovered=2" in primary.stdout
            and "already_applied=1" in primary.stdout
            and "applied_now=1" in primary.stdout,
            "Primary runner did not commit exactly one migration after gate release",
            ProductContractFailure,
        )

        final_ledger = h._ledger_rows("ledger_after_primary_completion")
        final_adoption = h._adoption_rows("adoption_after_primary_completion")
        final_probe = self._probe_state("probe_after_primary_completion")
        final_public = h._catalog_snapshot(
            "public_after_primary_completion",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        final_business = h._business_snapshot("business_after_primary_completion")
        self._assert_final_ledger(final_ledger, baseline_ledger)
        self._require_probe_committed(final_probe)
        require(
            final_adoption == baseline_adoption
            and final_public == public_before
            and final_business == business_before
            and self.accepted.canonical_fingerprint(final_public)
            == public_fingerprint,
            "Primary completion changed adoption or public application state",
            ProductContractFailure,
        )
        remaining_locks = self._lock_rows("locks_after_primary_completion")
        require(
            remaining_locks == [],
            "A migration or synthetic gate advisory lock remained after completion",
            ProductContractFailure,
        )

        idempotent = h._run_migration("idempotent_apply_after_lock_acceptance")
        require(
            idempotent.returncode == 0
            and "discovered=2" in idempotent.stdout
            and "already_applied=2" in idempotent.stdout
            and "applied_now=0" in idempotent.stdout,
            "Final ordinary apply was not idempotent",
            ProductContractFailure,
        )
        require(
            h._ledger_rows("ledger_after_idempotent_lock_apply") == final_ledger
            and self._probe_state("probe_after_idempotent_lock_apply") == final_probe,
            "Idempotent apply changed the version-2 ledger or probe",
            ProductContractFailure,
        )

        h._verify_owned_resources()
        h.evidence.update(
            {
                "lock_observations": self.observations,
                "pending_migration": {
                    "filename": PENDING_MIGRATION_FILENAME,
                    "name": PENDING_MIGRATION_NAME,
                    "sha256": pending_checksum,
                    "gate_lock_key": GATE_LOCK_KEY,
                    "ledger_rows": 2,
                    "probe_rows": 1,
                },
                "public_schema_fingerprint_before": public_fingerprint,
                "public_schema_fingerprint_after": self.accepted.canonical_fingerprint(
                    final_public
                ),
                "business_rows_before": 0,
                "business_rows_after": 0,
                "adoption_rows": 1,
                "skips": [],
            }
        )

    def cleanup_children(self) -> None:
        errors: list[str] = []

        def record_error(label: str, exc: Exception) -> None:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

        def force_bounded_shutdown(
            label: str,
            process: subprocess.Popen[str],
        ) -> None:
            if process.poll() is not None:
                return
            try:
                process.terminate()
            except Exception as exc:
                record_error(f"{label} terminate failed", exc)
            if process.poll() is not None:
                return
            try:
                process.wait(timeout=CHILD_SHUTDOWN_TIMEOUT_SECONDS)
                return
            except subprocess.TimeoutExpired as exc:
                record_error(f"{label} terminate wait timed out", exc)
            except Exception as exc:
                record_error(f"{label} terminate wait failed", exc)
            if process.poll() is not None:
                return
            try:
                process.kill()
            except Exception as exc:
                record_error(f"{label} kill failed", exc)
            if process.poll() is not None:
                return
            try:
                process.wait(timeout=CHILD_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception as exc:
                record_error(f"{label} post-kill wait failed", exc)

        def record_finished_child(
            label: str,
            process: subprocess.Popen[str],
            arguments: list[str],
        ) -> bool:
            if process.poll() is None:
                errors.append(
                    f"{label}: process remains active after bounded shutdown; "
                    "output was not read"
                )
                return False
            try:
                stdout = process.stdout.read() if process.stdout is not None else ""
                stderr = process.stderr.read() if process.stderr is not None else ""
                self._record_child(
                    label,
                    process,
                    arguments,
                    stdout,
                    stderr,
                )
                return True
            except Exception as exc:
                record_error(f"{label} evidence recording failed", exc)
                return False

        try:
            if (
                self.controller_process is not None
                and self.controller_process.poll() is None
            ):
                self._write_controller(
                    f"SELECT pg_advisory_unlock({GATE_LOCK_KEY});\n\\q\n"
                )
                self.controller_process.wait(timeout=CHILD_SHUTDOWN_TIMEOUT_SECONDS)
            if self.controller_process is not None and not self.controller_recorded:
                self.controller_recorded = record_finished_child(
                    "gate_controller_session",
                    self.controller_process,
                    self._controller_arguments(),
                )
        except Exception as exc:
            record_error("controller cleanup failed", exc)
            if self.controller_process is not None:
                try:
                    force_bounded_shutdown("controller", self.controller_process)
                except Exception as shutdown_error:
                    record_error("controller bounded shutdown failed", shutdown_error)
                if not self.controller_recorded:
                    try:
                        self.controller_recorded = record_finished_child(
                            "gate_controller_session",
                            self.controller_process,
                            self._controller_arguments(),
                        )
                    except Exception as recording_error:
                        record_error(
                            "controller evidence fallback failed", recording_error
                        )

        try:
            if self.primary_process is not None and self.primary_process.poll() is None:
                force_bounded_shutdown("primary", self.primary_process)
            if self.primary_process is not None and not self.primary_recorded:
                self.primary_recorded = record_finished_child(
                    "primary_migration_runner",
                    self.primary_process,
                    self._primary_arguments(),
                )
        except Exception as exc:
            record_error("primary cleanup failed", exc)
            if self.primary_process is not None:
                try:
                    force_bounded_shutdown("primary", self.primary_process)
                except Exception as shutdown_error:
                    record_error("primary bounded shutdown failed", shutdown_error)
                if not self.primary_recorded:
                    try:
                        self.primary_recorded = record_finished_child(
                            "primary_migration_runner",
                            self.primary_process,
                            self._primary_arguments(),
                        )
                    except Exception as recording_error:
                        record_error("primary evidence fallback failed", recording_error)

        if errors:
            self.harness.evidence["child_cleanup_failures"] = list(errors)
            raise IsolationBlocked("; ".join(errors))


def cleanup_and_finalize(
    accepted: ModuleType,
    acceptance: LiveMigrationLockAcceptance,
    result: str,
    failure: str,
) -> tuple[str, str]:
    try:
        acceptance.cleanup_children()
    except Exception as child_error:
        child_failure = {
            "type": type(child_error).__name__,
            "message": str(child_error),
        }
        try:
            acceptance.harness.evidence["child_cleanup_failure"] = child_failure
        except Exception as evidence_error:
            child_failure["evidence_recording_failure"] = (
                f"{type(evidence_error).__name__}: {evidence_error}"
            )
        result = RESULT_ISOLATION_BLOCKED
        failure = (
            "Child-process cleanup failed before disposable-resource finalization: "
            f"{child_failure['type']}: {child_failure['message']}"
        )

    try:
        return accepted.finalize_harness(
            acceptance.harness,
            result,
            failure,
        )
    except Exception as finalization_error:
        finalization_failure = {
            "type": type(finalization_error).__name__,
            "message": str(finalization_error),
        }
        try:
            acceptance.harness.evidence["finalization_failure"] = (
                finalization_failure
            )
        except Exception:
            pass
        combined = failure
        if combined:
            combined += "; "
        combined += (
            "Ownership-validated disposable-resource finalization failed: "
            f"{finalization_failure['type']}: "
            f"{finalization_failure['message']}"
        )
        return RESULT_ISOLATION_BLOCKED, combined


def run_focused_regression_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    original_run = subprocess.run
    original_popen = subprocess.Popen

    def forbid_subprocess(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Focused regression checks must not invoke subprocesses")

    subprocess.run = forbid_subprocess  # type: ignore[assignment]
    subprocess.Popen = forbid_subprocess  # type: ignore[assignment,misc]
    try:
        class FakeHarness:
            def __init__(self) -> None:
                self.evidence: dict[str, Any] = {}

            def sanitized_evidence(self) -> dict[str, Any]:
                return json.loads(json.dumps(self.evidence, sort_keys=True))

        class InjectedAcceptance:
            def __init__(self, injected: Exception) -> None:
                self.harness = FakeHarness()
                self.injected = injected

            def cleanup_children(self) -> None:
                raise self.injected

        def exercise_child_failure(
            injected: Exception,
        ) -> tuple[str, str, dict[str, Any], int]:
            calls = 0
            accepted = ModuleType("focused_fake_accepted_harness")

            def finalize_once(
                _harness: FakeHarness,
                incoming_result: str,
                incoming_failure: str,
            ) -> tuple[str, str]:
                nonlocal calls
                calls += 1
                return incoming_result, incoming_failure

            accepted.finalize_harness = finalize_once  # type: ignore[attr-defined]
            injected_acceptance = InjectedAcceptance(injected)
            focused_result, focused_failure = cleanup_and_finalize(
                accepted,
                injected_acceptance,  # type: ignore[arg-type]
                RESULT_PASS,
                "",
            )
            return (
                focused_result,
                focused_failure,
                injected_acceptance.harness.sanitized_evidence(),
                calls,
            )

        timeout = subprocess.TimeoutExpired("controller-psql", 8)
        timeout_result, timeout_failure, timeout_evidence, timeout_calls = (
            exercise_child_failure(timeout)
        )
        if (
            timeout_result != RESULT_ISOLATION_BLOCKED
            or timeout_calls != 1
            or "TimeoutExpired" not in timeout_failure
            or timeout_evidence.get("child_cleanup_failure", {}).get("type")
            != "TimeoutExpired"
        ):
            raise AssertionError(
                "Controller TimeoutExpired did not preserve evidence and finalize once"
            )
        checks["controller_timeout_finalizes_exactly_once"] = True
        checks["child_cleanup_failure_returns_blocked_isolation"] = True
        checks["original_child_failure_remains_in_sanitized_evidence"] = True

        primary_error = OSError("synthetic primary cleanup failure")
        primary_result, primary_failure, primary_evidence, primary_calls = (
            exercise_child_failure(primary_error)
        )
        if (
            primary_result != RESULT_ISOLATION_BLOCKED
            or primary_calls != 1
            or "synthetic primary cleanup failure" not in primary_failure
            or primary_evidence.get("child_cleanup_failure", {}).get("type")
            != "OSError"
        ):
            raise AssertionError(
                "Primary OSError did not preserve evidence and finalize once"
            )
        checks["primary_oserror_finalizes_exactly_once"] = True

        with tempfile.TemporaryDirectory(prefix="m0r0142a-focused-hashes-") as tmp:
            root = Path(tmp)
            protected_path = root / "protected.txt"
            protected_path.write_bytes(b"accepted\n")
            expected = {
                "protected.txt": hashlib.sha256(b"accepted\n").hexdigest()
            }
            before = verify_protected_hashes(root=root, expected_hashes=expected)
            if (
                verify_post_execution_protected_hashes(
                    before,
                    root=root,
                    expected_hashes=expected,
                    accepted_harness_relative_path=None,
                )
                != before
            ):
                raise AssertionError("Unchanged protected-file evidence did not pass")
            protected_path.write_bytes(b"changed\n")
            try:
                verify_post_execution_protected_hashes(
                    before,
                    root=root,
                    expected_hashes=expected,
                    accepted_harness_relative_path=None,
                )
            except ImplementationBoundaryBlocked:
                checks["changed_protected_file_blocks_implementation_boundary"] = True
            else:
                raise AssertionError("Changed protected file did not fail closed")
            protected_path.unlink()
            try:
                verify_post_execution_protected_hashes(
                    before,
                    root=root,
                    expected_hashes=expected,
                    accepted_harness_relative_path=None,
                )
            except ImplementationBoundaryBlocked:
                checks["missing_protected_file_blocks_implementation_boundary"] = True
            else:
                raise AssertionError("Missing protected file did not fail closed")

        subject = LiveMigrationLockAcceptance.__new__(
            LiveMigrationLockAcceptance
        )
        subject.controller_backend_pid = 101
        subject.primary_backend_pid = 202
        subject.controller_application_name = "focused-gate-controller"
        migration = advisory_lock_parts(EXPECTED_MIGRATION_LOCK_KEY)
        gate = advisory_lock_parts(GATE_LOCK_KEY)

        def lock_row(
            *,
            pid: int,
            parts: dict[str, int],
            granted: bool,
            application_name: str = "",
            state: str = "active",
            wait_event_type: str = "Lock",
            wait_event: str = "advisory",
        ) -> dict[str, Any]:
            return {
                "pid": pid,
                "granted": granted,
                "mode": "ExclusiveLock",
                "classid": parts["classid"],
                "objid": parts["objid"],
                "objsubid": parts["objsubid"],
                "application_name": application_name,
                "state": state,
                "wait_event_type": wait_event_type,
                "wait_event": wait_event,
            }

        expected_rows = [
            lock_row(
                pid=101,
                parts=gate,
                granted=True,
                application_name="focused-gate-controller",
                state="idle",
                wait_event_type="Client",
                wait_event="ClientRead",
            ),
            lock_row(pid=202, parts=migration, granted=True),
            lock_row(pid=202, parts=gate, granted=False),
        ]
        subject._assert_exact_lock_state(expected_rows, label="focused-exact-pass")
        checks["exact_three_row_lock_state_passes"] = True

        fourth = lock_row(pid=303, parts=gate, granted=False)
        try:
            subject._assert_exact_lock_state(
                [*expected_rows, fourth],
                label="focused-fourth-row",
            )
        except ProductContractFailure:
            checks["fourth_advisory_lock_row_fails"] = True
        else:
            raise AssertionError("A fourth monitored advisory-lock row was accepted")

        unexpected_pid_rows = [dict(row) for row in expected_rows]
        unexpected_pid_rows[0]["pid"] = 303
        try:
            subject._assert_exact_lock_state(
                unexpected_pid_rows,
                label="focused-unexpected-pid",
            )
        except ProductContractFailure:
            checks["unexpected_backend_pid_fails"] = True
        else:
            raise AssertionError("An unexpected advisory-lock backend was accepted")

        checks["focused_checks_use_no_subprocess_or_docker"] = True
        return checks
    finally:
        subprocess.run = original_run  # type: ignore[assignment]
        subprocess.Popen = original_popen  # type: ignore[assignment,misc]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run M0-R01.4.2a only in a generated disposable synthetic Compose project."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--confirm-disposable-synthetic-lock-run",
        action="store_true",
        help="required explicit authorization for the disposable concurrent-lock run",
    )
    mode.add_argument(
        "--run-focused-regression-checks",
        action="store_true",
        help="run no-Docker regression checks for lock and cleanup safeguards",
    )
    return parser


def classify_accepted_error(accepted: ModuleType, exc: Exception) -> str:
    if isinstance(exc, accepted.EnvironmentBlocked):
        return RESULT_ENVIRONMENT_BLOCKED
    if isinstance(exc, accepted.IsolationBlocked):
        return RESULT_ISOLATION_BLOCKED
    if isinstance(exc, accepted.ProductContractFailure):
        return RESULT_PRODUCT_FAILURE
    return RESULT_HARNESS_FAILURE


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_focused_regression_checks:
        print(json.dumps(run_focused_regression_checks(), sort_keys=True, indent=2))
        print("FOCUSED_REGRESSION_CHECKS_PASS")
        return 0
    if not args.confirm_disposable_synthetic_lock_run:
        print(
            "Explicit --confirm-disposable-synthetic-lock-run is required; no action taken.",
            file=sys.stderr,
        )
        print(RESULT_ISOLATION_BLOCKED)
        return 2

    accepted: ModuleType | None = None
    acceptance: LiveMigrationLockAcceptance | None = None
    static_evidence: dict[str, Any] | None = None
    result = RESULT_PASS
    failure = ""
    try:
        static_evidence = {
            "protected_hashes": verify_protected_hashes(),
            "repository": repository_evidence(),
            "migration_lock_source": read_and_verify_migration_lock_key(),
        }
        accepted = load_accepted_harness()
        acceptance = LiveMigrationLockAcceptance(accepted)
        acceptance.preflight(static_evidence)
        acceptance.lifecycle()
    except LockAcceptanceError as exc:
        result = exc.result
        failure = str(exc)
    except Exception as exc:
        if accepted is not None and isinstance(
            exc,
            (
                accepted.EnvironmentBlocked,
                accepted.IsolationBlocked,
                accepted.ProductContractFailure,
            ),
        ):
            result = classify_accepted_error(accepted, exc)
            failure = str(exc)
        else:
            result = RESULT_HARNESS_FAILURE
            failure = f"Unclassified verification failure: {type(exc).__name__}: {exc}"
    finally:
        if acceptance is not None and accepted is not None:
            result, failure = cleanup_and_finalize(
                accepted,
                acceptance,
                result,
                failure,
            )

    if acceptance is None:
        evidence: dict[str, Any] = {"failure": failure}
    else:
        if static_evidence is not None:
            try:
                protected_hashes_after = verify_post_execution_protected_hashes(
                    static_evidence["protected_hashes"]
                )
                acceptance.harness.evidence["protected_hashes_after"] = (
                    protected_hashes_after
                )
                acceptance.harness.evidence["accepted_harness_hash_after"] = (
                    protected_hashes_after[
                        "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
                    ]
                )
                acceptance.harness.evidence["post_execution_hash_verification"] = {
                    "accepted_values_verified": True,
                    "preflight_values_verified": True,
                    "paths_verified": len(protected_hashes_after),
                }
            except Exception as hash_error:
                prior_result = result
                prior_failure = failure
                result = RESULT_BOUNDARY_BLOCKED
                failure = (
                    "Post-execution protected-boundary verification failed: "
                    f"{type(hash_error).__name__}: {hash_error}"
                )
                acceptance.harness.evidence["post_execution_hash_verification"] = {
                    "accepted_values_verified": False,
                    "preflight_values_verified": False,
                    "failure": failure,
                    "prior_result": prior_result,
                    "prior_failure": prior_failure,
                }
        try:
            evidence = acceptance.harness.sanitized_evidence()
        except Exception as exc:
            if result != RESULT_BOUNDARY_BLOCKED:
                result = RESULT_HARNESS_FAILURE
            failure = f"Structured evidence rejected: {type(exc).__name__}: {exc}"
            evidence = {
                "project": acceptance.harness.project,
                "structured_evidence_rejected": True,
            }
    if failure:
        evidence["failure"] = failure
    print(json.dumps(evidence, sort_keys=True, indent=2))
    print(result)
    return 0 if result == RESULT_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
