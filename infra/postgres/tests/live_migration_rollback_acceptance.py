#!/usr/bin/env python3
"""Opt-in M0-R01.4.2b transactional-rollback acceptance.

The live path creates three independently finalized disposable synthetic
PostgreSQL projects.  It never reads the repository's ``.env`` file and is
intentionally excluded from ``test_*.py`` discovery::

    python3 infra/postgres/tests/live_migration_rollback_acceptance.py \
        --confirm-disposable-synthetic-rollback-run
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
LIFECYCLE_HARNESS_PATH = (
    ROOT / "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
)
LOCK_HARNESS_PATH = (
    ROOT / "infra/postgres/tests/live_migration_lock_acceptance.py"
)
LIFECYCLE_HARNESS_SHA256 = (
    "4d7fbe8d33d36b6ff50161f4374cf16477667903253b790cbc37cb3e54707cfd"
)
LOCK_HARNESS_SHA256 = (
    "dca6c0c8473c72134f68a11938be24324c6bbdbffc77c9dd7d56ed4bab736b53"
)

PROTECTED_HASHES = {
    "infra/postgres/tests/live_migration_lifecycle_acceptance.py": (
        LIFECYCLE_HARNESS_SHA256
    ),
    "infra/postgres/tests/live_migration_lock_acceptance.py": (
        LOCK_HARNESS_SHA256
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
    (
        "docs/architecture/decisions/"
        "ADR-0001-master-roadmap-v2-scope-expansion-sequencing.md"
    ): "afb78304621b383c2e187698beaaf0017037fa7c450063f326a05a9f71e5eaeb",
    (
        "docs/architecture/decisions/"
        "ADR-0002-retention-semantics-and-enforcement-contract.md"
    ): "307ce9d9484b3819d16c5178a3dc61fb56e257376779e679e4923b1e7f5beb37",
    "docs/architecture/M0_CONTRACT_FREEZE_ACCEPTANCE_MATRIX.md": (
        "cece377662dcb5224fa70226e4200f14017745615a6b31024c792cdc9d33de12"
    ),
}

RESULT_PASS = "PASS_M0_R01_4_2B"
RESULT_PRODUCT_FAILURE = "FAIL_PRODUCT_CONTRACT"
RESULT_HARNESS_FAILURE = "FAIL_VERIFICATION_HARNESS"
RESULT_ISOLATION_BLOCKED = "BLOCKED_ISOLATION"
RESULT_ENVIRONMENT_BLOCKED = "BLOCKED_ENVIRONMENT"
RESULT_BOUNDARY_BLOCKED = "BLOCKED_IMPLEMENTATION_BOUNDARY"

EXPECTED_MIGRATION_EXIT = 2
EXPECTED_MIGRATION_LOCK_KEY = 5999724105712152625
EXPECTED_LOCK_PARTS = {
    "classid": 1396919625,
    "objid": 1196568625,
    "objsubid": 1,
}
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RollbackAcceptanceError(RuntimeError):
    result = RESULT_HARNESS_FAILURE


class ProductContractFailure(RollbackAcceptanceError):
    result = RESULT_PRODUCT_FAILURE


class VerificationHarnessFailure(RollbackAcceptanceError):
    result = RESULT_HARNESS_FAILURE


class IsolationBlocked(RollbackAcceptanceError):
    result = RESULT_ISOLATION_BLOCKED


class EnvironmentBlocked(RollbackAcceptanceError):
    result = RESULT_ENVIRONMENT_BLOCKED


class ImplementationBoundaryBlocked(RollbackAcceptanceError):
    result = RESULT_BOUNDARY_BLOCKED


def require(
    condition: bool,
    message: str,
    error_type: type[RollbackAcceptanceError] = VerificationHarnessFailure,
) -> None:
    if not condition:
        raise error_type(message)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def verify_protected_hashes(
    *,
    root: Path = ROOT,
    expected_hashes: dict[str, str] = PROTECTED_HASHES,
    preflight_hashes: dict[str, str] | None = None,
) -> dict[str, str]:
    observed: dict[str, str] = {}
    require(
        len(expected_hashes) == 14,
        "The protected implementation boundary is not exactly 14 paths",
        ImplementationBoundaryBlocked,
    )
    if preflight_hashes is not None:
        require(
            set(preflight_hashes) == set(expected_hashes),
            "Protected preflight evidence has an unexpected path set",
            ImplementationBoundaryBlocked,
        )
    for relative_path, accepted_hash in expected_hashes.items():
        path = root / relative_path
        require(
            path.is_file(),
            f"Protected path is unavailable: {relative_path}",
            ImplementationBoundaryBlocked,
        )
        try:
            actual_hash = sha256_path(path)
        except OSError as exc:
            raise ImplementationBoundaryBlocked(
                f"Protected path is unreadable: {relative_path}: {type(exc).__name__}"
            ) from exc
        require(
            actual_hash == accepted_hash,
            f"Protected path differs from its accepted hash: {relative_path}",
            ImplementationBoundaryBlocked,
        )
        if preflight_hashes is not None:
            require(
                preflight_hashes.get(relative_path) == accepted_hash
                and actual_hash == preflight_hashes[relative_path],
                f"Protected path differs from preflight: {relative_path}",
                ImplementationBoundaryBlocked,
            )
        observed[relative_path] = actual_hash
    return observed


def authenticated_import(
    path: Path,
    accepted_hash: str,
    module_name: str,
    required_symbols: set[str],
) -> ModuleType:
    try:
        before = sha256_path(path)
    except OSError as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted harness is unavailable: {path.name}: {type(exc).__name__}"
        ) from exc
    require(
        before == accepted_hash,
        f"Accepted harness hash changed: {path.name}",
        ImplementationBoundaryBlocked,
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    require(
        spec is not None and spec.loader is not None,
        f"Accepted harness cannot be imported: {path.name}",
        ImplementationBoundaryBlocked,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted harness import failed: {path.name}: {type(exc).__name__}"
        ) from exc
    try:
        after = sha256_path(path)
    except OSError as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted harness became unreadable while importing: "
            f"{path.name}: {type(exc).__name__}"
        ) from exc
    require(
        after == accepted_hash,
        f"Accepted harness changed while importing: {path.name}",
        ImplementationBoundaryBlocked,
    )
    missing = sorted(symbol for symbol in required_symbols if not hasattr(module, symbol))
    require(
        not missing,
        f"Accepted harness lacks required symbols: {path.name}: {missing}",
        ImplementationBoundaryBlocked,
    )
    return module


def load_accepted_harnesses() -> tuple[ModuleType, ModuleType]:
    lock = authenticated_import(
        LOCK_HARNESS_PATH,
        LOCK_HARNESS_SHA256,
        "m0_r01_4_2a_accepted_lock_harness_for_rollback",
        {
            "EXPECTED_MIGRATION_LOCK_KEY",
            "advisory_lock_parts",
            "load_accepted_harness",
            "read_and_verify_migration_lock_key",
            "repository_evidence",
        },
    )
    try:
        accepted = lock.load_accepted_harness()
        lock_evidence = lock.read_and_verify_migration_lock_key()
    except Exception as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted harness dependency authentication failed: {type(exc).__name__}"
        ) from exc
    try:
        lifecycle_after = sha256_path(LIFECYCLE_HARNESS_PATH)
    except OSError as exc:
        raise ImplementationBoundaryBlocked(
            "Accepted lifecycle harness became unreadable during dependency loading: "
            f"{type(exc).__name__}"
        ) from exc
    require(
        lifecycle_after == LIFECYCLE_HARNESS_SHA256,
        "Accepted lifecycle harness changed during dependency loading",
        ImplementationBoundaryBlocked,
    )
    require(
        lock.EXPECTED_MIGRATION_LOCK_KEY == EXPECTED_MIGRATION_LOCK_KEY
        and lock_evidence.get("observed") == EXPECTED_MIGRATION_LOCK_KEY
        and lock.advisory_lock_parts(EXPECTED_MIGRATION_LOCK_KEY)
        == EXPECTED_LOCK_PARTS,
        "Accepted advisory-lock identity differs from the rollback contract",
        ImplementationBoundaryBlocked,
    )
    return accepted, lock


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def validate_identifier(value: str) -> str:
    require(
        IDENTIFIER_PATTERN.fullmatch(value) is not None,
        "A generated PostgreSQL identifier is unsafe",
    )
    return value


@dataclass(frozen=True)
class ScenarioSpec:
    key: str
    kind: str
    migration_filename: str
    migration_name: str
    probe_schema: str
    probe_table: str
    probe_id: str
    probe_value: str
    marker: str
    fault_schema: str | None = None
    fault_function: str | None = None
    fault_trigger: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.migration_name,
            self.probe_schema,
            self.probe_table,
        ):
            validate_identifier(value)
        for value in (self.fault_schema, self.fault_function, self.fault_trigger):
            if value is not None:
                validate_identifier(value)
        require(
            self.migration_filename == f"0002__{self.migration_name}.sql",
            "Scenario migration filename and name are inconsistent",
        )
        require(
            self.kind in {"sql_body", "ledger_insert", "deferred_commit"},
            "Unknown rollback scenario kind",
        )


SCENARIOS = (
    ScenarioSpec(
        key="A",
        kind="sql_body",
        migration_filename="0002__rollback_sql_body_probe.sql",
        migration_name="rollback_sql_body_probe",
        probe_schema="m0r0142b_sql_body_probe",
        probe_table="rollback_probe",
        probe_id="m0-r01-4-2b-scenario-a",
        probe_value="synthetic-sql-body-rollback-probe",
        marker="M0R0142B_SCENARIO_A_SQL_BODY_FAILURE",
    ),
    ScenarioSpec(
        key="B",
        kind="ledger_insert",
        migration_filename="0002__rollback_ledger_insert_probe.sql",
        migration_name="rollback_ledger_insert_probe",
        probe_schema="m0r0142b_ledger_insert_probe",
        probe_table="rollback_probe",
        probe_id="m0-r01-4-2b-scenario-b",
        probe_value="synthetic-ledger-insert-rollback-probe",
        marker="M0R0142B_SCENARIO_B_LEDGER_INSERT_FAILURE",
        fault_schema="m0r0142b_ledger_fault",
        fault_function="reject_expected_ledger_insert",
        fault_trigger="m0r0142b_reject_expected_ledger_insert",
    ),
    ScenarioSpec(
        key="C",
        kind="deferred_commit",
        migration_filename="0002__rollback_deferred_commit_probe.sql",
        migration_name="rollback_deferred_commit_probe",
        probe_schema="m0r0142b_deferred_commit_probe",
        probe_table="rollback_probe",
        probe_id="m0-r01-4-2b-scenario-c",
        probe_value="synthetic-deferred-commit-rollback-probe",
        marker="M0R0142B_SCENARIO_C_DEFERRED_COMMIT_FAILURE",
        fault_schema="m0r0142b_deferred_fault",
        fault_function="reject_expected_deferred_commit",
        fault_trigger="m0r0142b_reject_expected_deferred_commit",
    ),
)


def migration_content(spec: ScenarioSpec, *, fail_after_dml: bool = False) -> bytes:
    body = f"""CREATE SCHEMA {spec.probe_schema};

CREATE TABLE {spec.probe_schema}.{spec.probe_table} (
    probe_id text PRIMARY KEY,
    probe_value text NOT NULL,
    observed_at_utc timestamptz NOT NULL
);

INSERT INTO {spec.probe_schema}.{spec.probe_table} (
    probe_id,
    probe_value,
    observed_at_utc
) VALUES (
    {sql_literal(spec.probe_id)},
    {sql_literal(spec.probe_value)},
    TIMESTAMPTZ '2026-02-01T00:00:00Z'
);
"""
    if fail_after_dml:
        body += f"""
DO $m0r0142b_failure$
BEGIN
    RAISE EXCEPTION {sql_literal(spec.marker)};
END;
$m0r0142b_failure$;
"""
    return body.encode("utf-8")


def fault_function_source(spec: ScenarioSpec, migration_sha256: str) -> str:
    require(
        spec.fault_schema is not None and spec.fault_function is not None,
        "Fault function requested for a SQL-body scenario",
    )
    require(
        SHA256_PATTERN.fullmatch(migration_sha256) is not None,
        "Fault function received an invalid migration checksum",
    )
    return f"""BEGIN
    IF NEW.version = 2
       AND NEW.name = {sql_literal(spec.migration_name)}
       AND NEW.sha256 = {sql_literal(migration_sha256)} THEN
        RAISE EXCEPTION {sql_literal(spec.marker)};
    END IF;
    RETURN NEW;
END;"""


def fault_install_sql(spec: ScenarioSpec, migration_sha256: str) -> str:
    require(
        spec.kind in {"ledger_insert", "deferred_commit"}
        and spec.fault_schema is not None
        and spec.fault_function is not None
        and spec.fault_trigger is not None,
        "Fault installation requested for an invalid scenario",
    )
    source = fault_function_source(spec, migration_sha256)
    if spec.kind == "ledger_insert":
        trigger_clause = "BEFORE INSERT"
        constraint_clause = "TRIGGER"
    else:
        trigger_clause = "AFTER INSERT"
        constraint_clause = "CONSTRAINT TRIGGER"
    deferrable = (
        "\nDEFERRABLE INITIALLY DEFERRED" if spec.kind == "deferred_commit" else ""
    )
    return f"""CREATE SCHEMA {spec.fault_schema};

CREATE FUNCTION {spec.fault_schema}.{spec.fault_function}()
RETURNS trigger
LANGUAGE plpgsql
AS $m0r0142b_fault$
{source}
$m0r0142b_fault$;

CREATE {constraint_clause} {spec.fault_trigger}
{trigger_clause} ON smartcoat_migrations.applied_migrations{deferrable}
FOR EACH ROW
WHEN (
    NEW.version = 2
    AND NEW.name = {sql_literal(spec.migration_name)}
    AND NEW.sha256 = {sql_literal(migration_sha256)}
)
EXECUTE FUNCTION {spec.fault_schema}.{spec.fault_function}();
"""


def fault_remove_sql(spec: ScenarioSpec) -> str:
    require(
        spec.fault_schema is not None
        and spec.fault_function is not None
        and spec.fault_trigger is not None,
        "Fault removal requested for an invalid scenario",
    )
    return f"""DROP TRIGGER {spec.fault_trigger}
ON smartcoat_migrations.applied_migrations;
DROP FUNCTION {spec.fault_schema}.{spec.fault_function}();
DROP SCHEMA {spec.fault_schema};
"""


USER_CATALOG_QUERIES = {
    "schemas": """
        SELECT n.nspname AS schema_name, owner.rolname AS owner_name
        FROM pg_namespace AS n
        JOIN pg_roles AS owner ON owner.oid = n.nspowner
        WHERE n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname
    """,
    "relations": """
        SELECT n.nspname AS schema_name, c.relname AS relation_name,
               c.relkind::text AS relation_kind,
               c.relpersistence::text AS persistence,
               owner.rolname AS owner_name
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_roles AS owner ON owner.oid = c.relowner
        WHERE n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname, c.relkind
    """,
    "functions": """
        SELECT n.nspname AS function_schema, p.proname AS function_name,
               pg_get_function_identity_arguments(p.oid) AS function_arguments,
               pg_get_function_result(p.oid) AS result_type,
               l.lanname AS language_name, p.prosecdef AS security_definer,
               p.provolatile AS volatility, p.prosrc AS source
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_language AS l ON l.oid = p.prolang
        WHERE n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, p.proname,
                 pg_get_function_identity_arguments(p.oid)
    """,
    "triggers": """
        SELECT n.nspname AS target_schema, c.relname AS target_table,
               t.tgname AS trigger_name, t.tgenabled AS enabled,
               t.tgisinternal AS internal,
               function_n.nspname AS function_schema,
               p.proname AS function_name,
               pg_get_triggerdef(t.oid, false) AS trigger_definition,
               t.tgdeferrable AS deferrable,
               t.tginitdeferred AS initially_deferred
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_proc AS p ON p.oid = t.tgfoid
        JOIN pg_namespace AS function_n ON function_n.oid = p.pronamespace
        WHERE n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
          AND NOT t.tgisinternal
        ORDER BY n.nspname, c.relname, t.tgname
    """,
    "constraints": """
        SELECT n.nspname AS target_schema, c.relname AS target_table,
               con.conname AS constraint_name,
               con.contype::text AS constraint_type,
               con.condeferrable AS deferrable,
               con.condeferred AS initially_deferred
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname, con.conname
    """,
}


FAULT_TRIGGER_CATALOG_QUERY = """
    SELECT target_n.nspname AS target_schema,
           target_c.relname AS target_table,
           t.tgname AS trigger_name, t.tgenabled AS enabled,
           t.tgisinternal AS internal,
           function_n.nspname AS function_schema,
           p.proname AS function_name,
           pg_get_function_identity_arguments(p.oid) AS function_arguments,
           t.tgtype::integer AS trigger_type,
           t.tgdeferrable AS trigger_deferrable,
           t.tginitdeferred AS trigger_initially_deferred,
           t.tgconstraint::bigint AS constraint_oid,
           COALESCE(con.contype::text, '') AS constraint_type,
           COALESCE(con.condeferrable, false) AS constraint_deferrable,
           COALESCE(con.condeferred, false) AS constraint_initially_deferred,
           pg_get_triggerdef(t.oid, false) AS trigger_definition
    FROM pg_trigger AS t
    JOIN pg_class AS target_c ON target_c.oid = t.tgrelid
    JOIN pg_namespace AS target_n ON target_n.oid = target_c.relnamespace
    JOIN pg_proc AS p ON p.oid = t.tgfoid
    JOIN pg_namespace AS function_n ON function_n.oid = p.pronamespace
    LEFT JOIN pg_constraint AS con ON con.oid = t.tgconstraint
    WHERE target_n.nspname = 'smartcoat_migrations'
      AND target_c.relname = 'applied_migrations'
      AND t.tgname = {trigger_name}
"""


FAULT_FUNCTION_CATALOG_QUERY = """
    SELECT n.nspname AS function_schema, p.proname AS function_name,
           pg_get_function_identity_arguments(p.oid) AS function_arguments,
           pg_get_function_result(p.oid) AS result_type,
           l.lanname AS language_name, p.prosecdef AS security_definer,
           p.provolatile AS volatility, p.prosrc AS function_source
    FROM pg_proc AS p
    JOIN pg_namespace AS n ON n.oid = p.pronamespace
    JOIN pg_language AS l ON l.oid = p.prolang
    WHERE n.nspname = {function_schema}
      AND p.proname = {function_name}
      AND pg_get_function_identity_arguments(p.oid) = ''
"""


def normalized_sql(value: str) -> str:
    return " ".join(value.split())


def canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256_bytes(payload)


def catalog_delta(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    require(set(before) == set(after), "Catalog snapshot categories differ")
    delta: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for category in sorted(before):
        before_by_key = {
            json.dumps(row, sort_keys=True, separators=(",", ":")): row
            for row in before[category]
        }
        after_by_key = {
            json.dumps(row, sort_keys=True, separators=(",", ":")): row
            for row in after[category]
        }
        delta[category] = {
            "added": [after_by_key[key] for key in sorted(after_by_key.keys() - before_by_key)],
            "removed": [before_by_key[key] for key in sorted(before_by_key.keys() - after_by_key)],
        }
    return delta


def assert_fault_catalog_delta(
    spec: ScenarioSpec,
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
) -> None:
    require(
        spec.fault_schema is not None
        and spec.fault_function is not None
        and spec.fault_trigger is not None,
        "Fault delta requested for an invalid scenario",
    )
    delta = catalog_delta(before, after)
    require(
        all(not change["removed"] for change in delta.values()),
        "Fault installation removed an accepted catalog object",
        ProductContractFailure,
    )
    expected_counts = {
        "schemas": 1,
        "relations": 0,
        "functions": 1,
        "triggers": 1,
        "constraints": 1 if spec.kind == "deferred_commit" else 0,
    }
    require(
        {category: len(change["added"]) for category, change in delta.items()}
        == expected_counts,
        "Fault installation changed an unexpected user-catalog object",
        ProductContractFailure,
    )
    require(
        delta["schemas"]["added"][0]["schema_name"] == spec.fault_schema,
        "Fault installation created an unexpected schema",
        ProductContractFailure,
    )
    function = delta["functions"]["added"][0]
    trigger = delta["triggers"]["added"][0]
    require(
        function["function_schema"] == spec.fault_schema
        and function["function_name"] == spec.fault_function
        and trigger["trigger_name"] == spec.fault_trigger
        and trigger["function_schema"] == spec.fault_schema
        and trigger["function_name"] == spec.fault_function,
        "Fault installation catalog identities are incorrect",
        ProductContractFailure,
    )
    if spec.kind == "deferred_commit":
        constraint = delta["constraints"]["added"][0]
        require(
            constraint["target_schema"] == "smartcoat_migrations"
            and constraint["target_table"] == "applied_migrations"
            and constraint["constraint_name"] == spec.fault_trigger
            and constraint["constraint_type"] == "t"
            and constraint["deferrable"] is True
            and constraint["initially_deferred"] is True,
            "Deferred fault constraint catalog identity is incorrect",
            ProductContractFailure,
        )


def assert_fault_contract(
    spec: ScenarioSpec,
    row: dict[str, Any],
    migration_sha256: str,
) -> None:
    common = (
        row.get("target_schema") == "smartcoat_migrations"
        and row.get("target_table") == "applied_migrations"
        and row.get("trigger_name") == spec.fault_trigger
        and row.get("enabled") == "O"
        and row.get("internal") is False
        and row.get("function_schema") == spec.fault_schema
        and row.get("function_name") == spec.fault_function
        and row.get("function_arguments") == ""
        and row.get("result_type") == "trigger"
        and row.get("language_name") == "plpgsql"
        and row.get("security_definer") is False
        and row.get("volatility") == "v"
        and row.get("row_level") is True
        and row.get("insert_event") is True
        and row.get("delete_event") is False
        and row.get("update_event") is False
        and row.get("truncate_event") is False
        and row.get("instead_event") is False
    )
    require(common, "Fault-injection trigger has unexpected common semantics")
    when_expression = normalized_sql(str(row.get("when_expression", ""))).lower()
    trigger_definition = normalized_sql(
        str(row.get("trigger_definition", ""))
    ).lower()
    require(
        "new.version = 2" in when_expression
        and spec.migration_name in when_expression
        and migration_sha256 in when_expression,
        "Fault trigger is not restricted to the exact expected version-2 row",
    )
    require(
        str(spec.fault_trigger) in trigger_definition
        and "smartcoat_migrations.applied_migrations" in trigger_definition
        and str(spec.fault_schema) in trigger_definition
        and str(spec.fault_function) in trigger_definition
        and spec.migration_name in trigger_definition
        and migration_sha256 in trigger_definition,
        "Fault trigger definition differs from its exact qualified identities",
    )
    require(
        normalized_sql(str(row.get("function_source", "")))
        == normalized_sql(fault_function_source(spec, migration_sha256)),
        "Fault trigger function body differs from the exact scenario contract",
    )
    if spec.kind == "ledger_insert":
        require(
            row.get("before_timing") is True
            and row.get("constraint_trigger") is False
            and row.get("trigger_deferrable") is False
            and row.get("trigger_initially_deferred") is False
            and row.get("constraint_type") == ""
            and row.get("constraint_deferrable") is False
            and row.get("constraint_initially_deferred") is False,
            "Immediate ledger fault is not an ordinary BEFORE INSERT trigger",
        )
    else:
        require(
            row.get("before_timing") is False
            and row.get("constraint_trigger") is True
            and row.get("trigger_deferrable") is True
            and row.get("trigger_initially_deferred") is True
            and row.get("constraint_type") == "t"
            and row.get("constraint_deferrable") is True
            and row.get("constraint_initially_deferred") is True,
            "Deferred commit fault is not an initially-deferred constraint trigger",
        )


def assert_rollback_state(
    *,
    probe_state: dict[str, Any],
    ledger_after: list[dict[str, Any]],
    baseline_ledger: list[dict[str, Any]],
) -> None:
    require(
        probe_state
        == {"schema_exists": False, "table_exists": False, "rows": []},
        "A scenario probe effect survived the failed transaction",
        ProductContractFailure,
    )
    require(
        ledger_after == baseline_ledger
        and len(ledger_after) == 1
        and all(row.get("version") != 2 for row in ledger_after),
        "A version-2 ledger effect survived the failed transaction",
        ProductContractFailure,
    )


def postgres_error_marker_count(raw_logs: str, marker: str) -> int:
    pattern = re.compile(
        rf"(?m)^.*\bERROR:\s+(?:[A-Z0-9]{{5}}:\s+)?{re.escape(marker)}\s*$"
    )
    return len(pattern.findall(raw_logs))


def expected_failure_line(spec: ScenarioSpec) -> str:
    return (
        "Migration error: Migration "
        f"0002__{spec.migration_name} failed and was rolled back"
    )


@dataclass
class BaselineEvidence:
    database_oid: int
    public_catalog: dict[str, list[dict[str, Any]]]
    public_fingerprint: str
    business_rows: dict[str, list[dict[str, Any]]]
    ledger: list[dict[str, Any]]
    adoption: list[dict[str, Any]]
    metadata_catalog: dict[str, list[dict[str, Any]]]
    user_catalog: dict[str, list[dict[str, Any]]]


def sanitized_exception_message(
    exc: Exception,
    *,
    harness: Any | None = None,
    accepted: ModuleType | None = None,
    lock: ModuleType | None = None,
) -> str:
    allowed_types: list[type[BaseException]] = [RollbackAcceptanceError]
    if accepted is not None:
        allowed_types.extend(
            [
                accepted.EnvironmentBlocked,
                accepted.IsolationBlocked,
                accepted.ProductContractFailure,
            ]
        )
    if lock is not None:
        allowed_types.extend(
            [
                lock.ImplementationBoundaryBlocked,
                lock.EnvironmentBlocked,
                lock.IsolationBlocked,
                lock.ProductContractFailure,
                lock.VerificationHarnessFailure,
            ]
        )
    if not isinstance(exc, tuple(allowed_types)):
        return f"Unclassified verification failure: {type(exc).__name__}"
    candidate = f"{type(exc).__name__}: {exc}"
    secret_values = getattr(harness, "secret_values", set())
    if any(secret and secret in candidate for secret in secret_values):
        return f"{type(exc).__name__}: detail withheld by synthetic-secret boundary"
    if re.search(r"(?:postgres(?:ql)?://|MIGRATION_DATABASE_URL\s*=)", candidate, re.I):
        return f"{type(exc).__name__}: detail withheld by connection-string boundary"
    return candidate


def assert_sanitized_evidence(harness: Any, evidence: dict[str, Any]) -> None:
    serialized = json.dumps(evidence, sort_keys=True)
    secret_values = getattr(harness, "secret_values", set())
    require(
        not any(secret and secret in serialized for secret in secret_values),
        "Structured rollback evidence contains a synthetic credential value",
        VerificationHarnessFailure,
    )
    require(
        re.search(r"(?:postgres(?:ql)?://|MIGRATION_DATABASE_URL\s*=)", serialized, re.I)
        is None,
        "Structured rollback evidence contains a connection credential boundary",
        VerificationHarnessFailure,
    )


class FinalizerOnce:
    def __init__(self, accepted: ModuleType, harness: Any) -> None:
        self.accepted = accepted
        self.harness = harness
        self.calls = 0

    def finalize(self, result: str, failure: str) -> tuple[str, str]:
        require(
            self.calls == 0,
            "A disposable scenario finalizer was invoked more than once",
            IsolationBlocked,
        )
        self.calls += 1
        try:
            return self.accepted.finalize_harness(self.harness, result, failure)
        except Exception as exc:
            return (
                RESULT_ISOLATION_BLOCKED,
                "Ownership-validated disposable-resource finalization failed: "
                + sanitized_exception_message(
                    exc,
                    harness=self.harness,
                    accepted=self.accepted,
                ),
            )


class RollbackScenarioAcceptance:
    def __init__(
        self,
        accepted: ModuleType,
        lock: ModuleType,
        spec: ScenarioSpec,
    ) -> None:
        self.accepted = accepted
        self.lock = lock
        self.spec = spec
        self.harness = accepted.LiveMigrationLifecycleAcceptance()
        self.harness.pending_migration_fixture = (
            self.harness.migration_fixture_directory / spec.migration_filename
        )
        self.container_id = ""
        self.evidence: dict[str, Any] = {
            "scenario": spec.key,
            "scenario_type": spec.kind,
            "project": self.harness.project,
            "database": self.harness.database_name,
            "checks": [],
        }

    def _write_fixture(self, content: bytes) -> str:
        h = self.harness
        require(
            not h.pending_migration_fixture.exists(),
            "Scenario pending migration already exists",
            IsolationBlocked,
        )
        h.pending_migration_fixture.write_bytes(content)
        h.pending_migration_fixture.chmod(0o400)
        checksum = sha256_path(h.pending_migration_fixture)
        require(
            checksum == sha256_bytes(content),
            "Scenario fixture bytes changed while being written",
            IsolationBlocked,
        )
        h.pending_migration_sha256 = checksum
        h._assert_migration_fixture_host_ownership(expect_pending=True)
        return checksum

    def _replace_fixture_atomically(self, content: bytes) -> str:
        h = self.harness
        target = h.pending_migration_fixture.resolve()
        directory = h.migration_fixture_directory.resolve()
        require(
            target.parent == directory
            and target.is_file()
            and target.stat().st_uid == os.getuid()
            and stat.S_IMODE(target.stat().st_mode) == 0o400,
            "Unapplied fixture replacement target is unsafe",
            IsolationBlocked,
        )
        staging = directory / f".{target.name}.replacement"
        require(not staging.exists(), "Fixture replacement staging path already exists")
        descriptor = -1
        try:
            descriptor = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
            written = 0
            while written < len(content):
                written += os.write(descriptor, content[written:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            staging.chmod(0o400)
            require(
                staging.stat().st_uid == os.getuid()
                and stat.S_IMODE(staging.stat().st_mode) == 0o400
                and staging.read_bytes() == content,
                "Fixture replacement staging bytes or ownership are unsafe",
                IsolationBlocked,
            )
            os.replace(staging, target)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if staging.exists():
                require(
                    staging.resolve().parent == directory
                    and staging.stat().st_uid == os.getuid(),
                    "Refusing to remove an unowned fixture staging path",
                    IsolationBlocked,
                )
                staging.unlink()
        checksum = sha256_path(target)
        require(
            checksum == sha256_bytes(content)
            and target.stat().st_uid == os.getuid()
            and stat.S_IMODE(target.stat().st_mode) == 0o400,
            "Atomic fixture replacement did not preserve exact bytes or permissions",
            IsolationBlocked,
        )
        h.pending_migration_sha256 = checksum
        h._assert_migration_fixture_host_ownership(expect_pending=True)
        return checksum

    def _start_isolated_postgres(self) -> None:
        h = self.harness
        h._install_cleanup_handlers()
        h.state_change_attempted = True
        h._verify_migration_execution_image_boundary()
        started = h._compose(
            f"scenario_{self.spec.key}_start_isolated_postgres",
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
            "The isolated PostgreSQL service could not start from a local image",
            EnvironmentBlocked,
        )
        self.container_id = h._wait_for_postgres()
        h._verify_running_postgres_isolation(self.container_id)
        self.evidence["checks"].append("postgres_healthy_owned_and_isolated")

    def _insert_synthetic_business_row(self) -> None:
        h = self.harness
        h._psql_success(
            f"scenario_{self.spec.key}_insert_synthetic_business_row",
            """
                INSERT INTO public.users (
                    user_id, display_name, email, role, active, created_at_utc
                ) VALUES (
                    'usr_m0_r01_4_2b_synthetic',
                    'M0 R01 4 2b Synthetic User',
                    'm0-r01-4-2b-synthetic@example.invalid',
                    'UPLOADER', true,
                    TIMESTAMPTZ '2026-02-01T00:00:00Z'
                )
            """,
        )

    def _adopt_and_capture_baseline(self) -> BaselineEvidence:
        h = self.harness
        public_before = h._catalog_snapshot(
            f"scenario_{self.spec.key}_public_before_adoption",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        h._assert_public_bootstrap(public_before)
        require(
            h._metadata_state(
                f"scenario_{self.spec.key}_metadata_before_adoption"
            )
            == [False] * 6,
            "Fresh synthetic bootstrap unexpectedly contains migration metadata",
            ProductContractFailure,
        )
        self._insert_synthetic_business_row()
        business_before = h._business_snapshot(
            f"scenario_{self.spec.key}_business_before_adoption"
        )
        require(
            len(business_before["users"]) == 1
            and sum(len(rows) for rows in business_before.values()) == 1,
            "Synthetic business-row fixture is not isolated",
            ProductContractFailure,
        )
        adoption_result = h._run_migration(
            f"scenario_{self.spec.key}_explicit_adoption",
            "adopt",
            h.database_name,
        )
        require(
            adoption_result.returncode == 0
            and "status=ADOPTED" in adoption_result.stdout
            and "evidence_inserted=true" in adoption_result.stdout,
            "Explicit adoption did not create the accepted synthetic baseline",
            ProductContractFailure,
        )
        identity = h._psql_rows(
            f"scenario_{self.spec.key}_database_identity",
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
            "Independent synthetic database identity is incorrect",
            ProductContractFailure,
        )
        ledger = h._ledger_rows(f"scenario_{self.spec.key}_ledger_baseline")
        adoption = h._adoption_rows(f"scenario_{self.spec.key}_adoption_baseline")
        h._assert_adoption_rows(ledger, adoption, int(identity[0]["database_oid"]))
        metadata = h._catalog_snapshot(
            f"scenario_{self.spec.key}_metadata_baseline",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        h._assert_metadata_contract(metadata)
        public_after = h._catalog_snapshot(
            f"scenario_{self.spec.key}_public_baseline",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        require(
            public_after == public_before
            and h._business_snapshot(
                f"scenario_{self.spec.key}_business_baseline"
            )
            == business_before,
            "Explicit adoption changed public application state",
            ProductContractFailure,
        )
        probe = self._probe_state(f"scenario_{self.spec.key}_probe_baseline")
        require(
            probe == {"schema_exists": False, "table_exists": False, "rows": []},
            "Scenario probe exists before its migration",
            ProductContractFailure,
        )
        user_catalog = h._catalog_snapshot(
            f"scenario_{self.spec.key}_user_catalog_baseline",
            USER_CATALOG_QUERIES,
        )
        self.evidence["checks"].append("bootstrap_adopted_and_baseline_captured")
        return BaselineEvidence(
            database_oid=int(identity[0]["database_oid"]),
            public_catalog=public_after,
            public_fingerprint=self.accepted.canonical_fingerprint(public_after),
            business_rows=business_before,
            ledger=ledger,
            adoption=adoption,
            metadata_catalog=metadata,
            user_catalog=user_catalog,
        )

    def _probe_state(self, label: str) -> dict[str, Any]:
        h = self.harness
        state_rows = h._psql_rows(
            f"{label}_existence",
            f"""
                SELECT to_regnamespace({sql_literal(self.spec.probe_schema)})
                           IS NOT NULL AS schema_exists,
                       to_regclass(
                           {sql_literal(self.spec.probe_schema + '.' + self.spec.probe_table)}
                       ) IS NOT NULL AS table_exists
            """,
        )
        require(len(state_rows) == 1, "Probe existence query was incomplete")
        state = state_rows[0]
        rows: list[dict[str, Any]] = []
        if state["table_exists"]:
            rows = h._psql_rows(
                f"{label}_rows",
                f"""
                    SELECT probe_id, probe_value,
                           observed_at_utc::text AS observed_at_utc
                    FROM {self.spec.probe_schema}.{self.spec.probe_table}
                    ORDER BY probe_id
                """,
            )
        return {
            "schema_exists": state["schema_exists"],
            "table_exists": state["table_exists"],
            "rows": rows,
        }

    def _lock_rows(self, label: str) -> list[dict[str, Any]]:
        parts = self.lock.advisory_lock_parts(EXPECTED_MIGRATION_LOCK_KEY)
        require(parts == EXPECTED_LOCK_PARTS, "Migration advisory-lock parts changed")
        return self.harness._psql_rows(
            label,
            f"""
                SELECT l.pid, l.granted, l.mode,
                       l.classid::bigint AS classid,
                       l.objid::bigint AS objid,
                       l.objsubid
                FROM pg_locks AS l
                WHERE l.locktype = 'advisory'
                  AND l.database = (
                      SELECT oid FROM pg_database
                      WHERE datname = current_database()
                  )
                  AND l.classid = {parts['classid']}::oid
                  AND l.objid = {parts['objid']}::oid
                  AND l.objsubid = {parts['objsubid']}
                ORDER BY l.pid
            """,
        )

    def _owned_postgres_logs(self, label: str) -> str:
        h = self.harness
        require(bool(self.container_id), "Owned PostgreSQL container identity is missing")
        h._verify_running_postgres_isolation(self.container_id)
        h._verify_owned_resources()
        completed = h._run(
            label,
            ["docker", "container", "logs", self.container_id],
            record=False,
        )
        require(
            completed.returncode == 0,
            "Owned PostgreSQL logs are unavailable for marker verification",
            EnvironmentBlocked,
        )
        h._assert_no_secret_output(completed)
        return completed.stdout + completed.stderr

    def _verify_fault_catalog(
        self,
        migration_sha256: str,
    ) -> dict[str, Any]:
        require(
            self.spec.fault_trigger is not None
            and self.spec.fault_schema is not None
            and self.spec.fault_function is not None,
            "Fault trigger identity is missing",
        )
        trigger_rows = self.harness._psql_rows(
            f"scenario_{self.spec.key}_fault_trigger_catalog",
            FAULT_TRIGGER_CATALOG_QUERY.format(
                trigger_name=sql_literal(self.spec.fault_trigger)
            ),
        )
        function_rows = self.harness._psql_rows(
            f"scenario_{self.spec.key}_fault_function_catalog",
            FAULT_FUNCTION_CATALOG_QUERY.format(
                function_schema=sql_literal(self.spec.fault_schema),
                function_name=sql_literal(self.spec.fault_function),
            ),
        )
        require(
            len(trigger_rows) == 1 and len(function_rows) == 1,
            "Expected exactly one scenario fault trigger and function",
        )
        row = {**trigger_rows[0], **function_rows[0]}
        trigger_type = int(row["trigger_type"])
        row.update(
            {
                "row_level": bool(trigger_type & 1),
                "before_timing": bool(trigger_type & 2),
                "insert_event": bool(trigger_type & 4),
                "delete_event": bool(trigger_type & 8),
                "update_event": bool(trigger_type & 16),
                "truncate_event": bool(trigger_type & 32),
                "instead_event": bool(trigger_type & 64),
                "constraint_trigger": int(row["constraint_oid"]) != 0,
                "when_expression": row["trigger_definition"],
            }
        )
        assert_fault_contract(self.spec, row, migration_sha256)
        return row

    def _install_fault(
        self,
        migration_sha256: str,
        baseline: BaselineEvidence,
    ) -> dict[str, list[dict[str, Any]]]:
        h = self.harness
        h._psql_success(
            f"scenario_{self.spec.key}_install_fault",
            fault_install_sql(self.spec, migration_sha256),
        )
        self._verify_fault_catalog(migration_sha256)
        installed_catalog = h._catalog_snapshot(
            f"scenario_{self.spec.key}_user_catalog_with_fault",
            USER_CATALOG_QUERIES,
        )
        assert_fault_catalog_delta(self.spec, baseline.user_catalog, installed_catalog)
        metadata_with_fault = h._catalog_snapshot(
            f"scenario_{self.spec.key}_metadata_with_fault",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        require(
            len(metadata_with_fault["triggers"])
            == len(baseline.metadata_catalog["triggers"]) + 1,
            "Fault installation changed an unexpected metadata trigger",
            ProductContractFailure,
        )
        accepted_subset = dict(metadata_with_fault)
        accepted_subset["triggers"] = [
            row
            for row in metadata_with_fault["triggers"]
            if row["trigger_name"] != self.spec.fault_trigger
        ]
        if self.spec.kind == "deferred_commit":
            accepted_subset["constraints"] = [
                row
                for row in metadata_with_fault["constraints"]
                if row["constraint_name"] != self.spec.fault_trigger
            ]
        require(
            accepted_subset == baseline.metadata_catalog,
            "Fault installation changed accepted migration metadata definitions",
            ProductContractFailure,
        )
        h._assert_metadata_contract(accepted_subset)
        self.evidence["checks"].append(
            "fault_target_timing_filter_and_catalog_delta_validated"
        )
        return installed_catalog

    def _remove_fault(
        self,
        baseline: BaselineEvidence,
    ) -> None:
        h = self.harness
        h._psql_success(
            f"scenario_{self.spec.key}_remove_exact_fault_objects",
            fault_remove_sql(self.spec),
        )
        require(
            h._psql_rows(
                f"scenario_{self.spec.key}_fault_trigger_absent",
                FAULT_TRIGGER_CATALOG_QUERY.format(
                    trigger_name=sql_literal(str(self.spec.fault_trigger))
                ),
            )
            == [],
            "Scenario fault trigger remains after exact removal",
            ProductContractFailure,
        )
        require(
            h._psql_success(
                f"scenario_{self.spec.key}_fault_schema_absent",
                "SELECT (to_regnamespace("
                f"{sql_literal(str(self.spec.fault_schema))}) IS NULL)::text",
            )
            == "true",
            "Scenario fault schema remains after exact removal",
            ProductContractFailure,
        )
        restored_metadata = h._catalog_snapshot(
            f"scenario_{self.spec.key}_metadata_after_fault_removal",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        h._assert_metadata_contract(restored_metadata)
        require(
            restored_metadata == baseline.metadata_catalog
            and h._catalog_snapshot(
                f"scenario_{self.spec.key}_user_catalog_after_fault_removal",
                USER_CATALOG_QUERIES,
            )
            == baseline.user_catalog,
            "Accepted catalog did not return exactly to its pre-injection state",
            ProductContractFailure,
        )
        self.evidence["checks"].append("fault_objects_removed_and_catalog_restored")

    def _assert_expected_runner_failure(
        self,
        completed: subprocess.CompletedProcess[str],
    ) -> None:
        combined = completed.stdout + completed.stderr
        expected = expected_failure_line(self.spec)
        require(
            completed.returncode == EXPECTED_MIGRATION_EXIT
            and expected in combined,
            "Migration runner did not report the accepted rollback-classified failure",
            ProductContractFailure,
        )
        self.evidence["failed_migration"] = {
            "exit": completed.returncode,
            "classification": "MIGRATION_ERROR_ROLLED_BACK",
            "accepted_message": expected,
        }

    def _assert_post_failure(
        self,
        baseline: BaselineEvidence,
        installed_catalog: dict[str, list[dict[str, Any]]] | None,
    ) -> None:
        h = self.harness
        probe = self._probe_state(f"scenario_{self.spec.key}_probe_after_failure")
        ledger = h._ledger_rows(f"scenario_{self.spec.key}_ledger_after_failure")
        assert_rollback_state(
            probe_state=probe,
            ledger_after=ledger,
            baseline_ledger=baseline.ledger,
        )
        adoption = h._adoption_rows(
            f"scenario_{self.spec.key}_adoption_after_failure"
        )
        metadata = h._catalog_snapshot(
            f"scenario_{self.spec.key}_metadata_after_failure",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        public = h._catalog_snapshot(
            f"scenario_{self.spec.key}_public_after_failure",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        business = h._business_snapshot(
            f"scenario_{self.spec.key}_business_after_failure"
        )
        user_catalog = h._catalog_snapshot(
            f"scenario_{self.spec.key}_user_catalog_after_failure",
            USER_CATALOG_QUERIES,
        )
        if installed_catalog is None:
            h._assert_metadata_contract(metadata)
            expected_metadata = baseline.metadata_catalog
            expected_user_catalog = baseline.user_catalog
        else:
            self._verify_fault_catalog(
                str(self.harness.pending_migration_sha256)
            )
            expected_metadata = dict(metadata)
            expected_metadata["triggers"] = [
                row
                for row in metadata["triggers"]
                if row["trigger_name"] != self.spec.fault_trigger
            ]
            if self.spec.kind == "deferred_commit":
                expected_metadata["constraints"] = [
                    row
                    for row in metadata["constraints"]
                    if row["constraint_name"] != self.spec.fault_trigger
                ]
            h._assert_metadata_contract(expected_metadata)
            require(
                len(metadata["triggers"])
                == len(baseline.metadata_catalog["triggers"]) + 1
                and len(metadata["constraints"])
                == len(baseline.metadata_catalog["constraints"])
                + (1 if self.spec.kind == "deferred_commit" else 0),
                "Fault metadata trigger evidence is incomplete",
                ProductContractFailure,
            )
            expected_user_catalog = installed_catalog
        require(
            expected_metadata == baseline.metadata_catalog
            and adoption == baseline.adoption
            and public == baseline.public_catalog
            and self.accepted.canonical_fingerprint(public)
            == baseline.public_fingerprint
            and business == baseline.business_rows
            and user_catalog == expected_user_catalog,
            "Failed migration changed accepted catalog, adoption, or application state",
            ProductContractFailure,
        )
        remaining_locks = self._lock_rows(
            f"scenario_{self.spec.key}_locks_after_failure"
        )
        require(
            remaining_locks == [],
            "Migration advisory lock remained held after failed execution",
            ProductContractFailure,
        )
        self.evidence["rollback"] = {
            "probe_schema_exists": False,
            "probe_table_exists": False,
            "probe_rows": 0,
            "version_2_ledger_rows": 0,
            "baseline_ledger_fingerprint_before": canonical_fingerprint(
                baseline.ledger
            ),
            "baseline_ledger_fingerprint_after": canonical_fingerprint(ledger),
            "adoption_fingerprint_before": canonical_fingerprint(baseline.adoption),
            "adoption_fingerprint_after": canonical_fingerprint(adoption),
            "metadata_guards_unchanged": expected_metadata
            == baseline.metadata_catalog,
            "application_triggers_unchanged": public == baseline.public_catalog,
            "public_schema_fingerprint_before": baseline.public_fingerprint,
            "public_schema_fingerprint_after": self.accepted.canonical_fingerprint(
                public
            ),
            "application_rows_unchanged": business == baseline.business_rows,
            "migration_advisory_lock_rows": 0,
        }
        self.evidence["checks"].append("direct_transactional_rollback_verified")

    def _assert_success_and_idempotency(
        self,
        baseline: BaselineEvidence,
        migration_sha256: str,
    ) -> None:
        h = self.harness
        retry = h._run_migration(f"scenario_{self.spec.key}_retry_apply")
        require(
            retry.returncode == 0
            and "discovered=2" in retry.stdout
            and "already_applied=1" in retry.stdout
            and "applied_now=1" in retry.stdout,
            "Corrected or fault-free retry did not apply exactly one migration",
            ProductContractFailure,
        )
        ledger = h._ledger_rows(f"scenario_{self.spec.key}_ledger_after_retry")
        probe = self._probe_state(f"scenario_{self.spec.key}_probe_after_retry")
        require(
            len(ledger) == 2
            and ledger[0] == baseline.ledger[0]
            and ledger[1]["version"] == 2
            and ledger[1]["name"] == self.spec.migration_name
            and ledger[1]["sha256"] == migration_sha256
            and ledger[1]["applied_by"] == h.admin_user
            and bool(ledger[1]["applied_at_utc"]),
            "Retry ledger evidence differs from the exact version-2 fixture",
            ProductContractFailure,
        )
        expected_probe = {
            "schema_exists": True,
            "table_exists": True,
            "rows": [
                {
                    "probe_id": self.spec.probe_id,
                    "probe_value": self.spec.probe_value,
                    "observed_at_utc": "2026-02-01 00:00:00+00",
                }
            ],
        }
        require(
            probe == expected_probe,
            "Retry did not commit exactly one deterministic synthetic probe row",
            ProductContractFailure,
        )
        adoption = h._adoption_rows(f"scenario_{self.spec.key}_adoption_after_retry")
        public = h._catalog_snapshot(
            f"scenario_{self.spec.key}_public_after_retry",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        metadata = h._catalog_snapshot(
            f"scenario_{self.spec.key}_metadata_after_retry",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        h._assert_metadata_contract(metadata)
        require(
            adoption == baseline.adoption
            and public == baseline.public_catalog
            and self.accepted.canonical_fingerprint(public)
            == baseline.public_fingerprint
            and h._business_snapshot(
                f"scenario_{self.spec.key}_business_after_retry"
            )
            == baseline.business_rows
            and metadata == baseline.metadata_catalog
            and self._lock_rows(f"scenario_{self.spec.key}_locks_after_retry") == [],
            "Successful retry changed accepted application or metadata state",
            ProductContractFailure,
        )
        idempotent = h._run_migration(
            f"scenario_{self.spec.key}_idempotent_reapply"
        )
        require(
            idempotent.returncode == 0
            and "discovered=2" in idempotent.stdout
            and "already_applied=2" in idempotent.stdout
            and "applied_now=0" in idempotent.stdout,
            "Repeated ordinary apply was not idempotent",
            ProductContractFailure,
        )
        ledger_after_idempotent = h._ledger_rows(
            f"scenario_{self.spec.key}_ledger_after_idempotent"
        )
        probe_after_idempotent = self._probe_state(
            f"scenario_{self.spec.key}_probe_after_idempotent"
        )
        public_after_idempotent = h._catalog_snapshot(
            f"scenario_{self.spec.key}_public_after_idempotent",
            self.accepted.PUBLIC_CATALOG_QUERIES,
        )
        metadata_after_idempotent = h._catalog_snapshot(
            f"scenario_{self.spec.key}_metadata_after_idempotent",
            self.accepted.METADATA_CATALOG_QUERIES,
        )
        h._assert_metadata_contract(metadata_after_idempotent)
        require(
            ledger_after_idempotent == ledger
            and probe_after_idempotent == probe
            and h._adoption_rows(
                f"scenario_{self.spec.key}_adoption_after_idempotent"
            )
            == baseline.adoption
            and public_after_idempotent == baseline.public_catalog
            and self.accepted.canonical_fingerprint(public_after_idempotent)
            == baseline.public_fingerprint
            and h._business_snapshot(
                f"scenario_{self.spec.key}_business_after_idempotent"
            )
            == baseline.business_rows
            and metadata_after_idempotent == baseline.metadata_catalog
            and self._lock_rows(
                f"scenario_{self.spec.key}_locks_after_idempotent"
            )
            == [],
            "Idempotent reapplication changed accepted PostgreSQL evidence",
            ProductContractFailure,
        )
        h._verify_owned_resources()
        self.evidence["retry"] = {
            "fixture_sha256": migration_sha256,
            "apply_exit": retry.returncode,
            "version_2_ledger_rows": 1,
            "probe_rows": 1,
            "idempotent_exit": idempotent.returncode,
            "idempotent_reapplication": True,
            "ledger_fingerprint": canonical_fingerprint(ledger),
            "probe_fingerprint": canonical_fingerprint(probe),
        }
        self.evidence["checks"].append(
            "retry_committed_once_and_reapplication_idempotent"
        )

    def run(self) -> None:
        h = self.harness
        h.preflight()
        self._start_isolated_postgres()
        baseline = self._adopt_and_capture_baseline()
        valid_content = migration_content(self.spec)
        failing_content = (
            migration_content(self.spec, fail_after_dml=True)
            if self.spec.kind == "sql_body"
            else valid_content
        )
        failed_checksum = self._write_fixture(failing_content)
        valid_checksum = sha256_bytes(valid_content)
        self.evidence["fixture"] = {
            "version": 2,
            "name": self.spec.migration_name,
            "failed_attempt_sha256": failed_checksum,
            "failed_attempt_bytes_authenticated": True,
        }
        installed_catalog: dict[str, list[dict[str, Any]]] | None = None
        if self.spec.kind != "sql_body":
            try:
                installed_catalog = self._install_fault(failed_checksum, baseline)
            except (
                ProductContractFailure,
                self.accepted.ProductContractFailure,
            ) as exc:
                raise VerificationHarnessFailure(
                    "Disposable fault-injection setup or catalog validation failed"
                ) from exc

        logs_before = self._owned_postgres_logs(
            f"scenario_{self.spec.key}_logs_before_failure"
        )
        require(
            postgres_error_marker_count(logs_before, self.spec.marker) == 0,
            "Scenario marker already exists in owned PostgreSQL error logs",
            EnvironmentBlocked,
        )
        failed = h._run_migration(f"scenario_{self.spec.key}_expected_failure")
        self._assert_expected_runner_failure(failed)
        logs_after = self._owned_postgres_logs(
            f"scenario_{self.spec.key}_logs_after_failure"
        )
        marker_count = postgres_error_marker_count(logs_after, self.spec.marker)
        require(
            marker_count == 1,
            "Expected PostgreSQL fault marker was not observed exactly once",
            EnvironmentBlocked,
        )
        self.evidence["failure_marker"] = {
            "scenario": self.spec.key,
            "expected_marker": self.spec.marker,
            "owned_container_validated": True,
            "marker_absent_before": True,
            "postgres_error_line_observed": True,
            "postgres_error_line_count": marker_count,
            "runner_classification": "MIGRATION_ERROR_ROLLED_BACK",
        }
        self._assert_post_failure(baseline, installed_catalog)

        if self.spec.kind == "sql_body":
            corrected_checksum = self._replace_fixture_atomically(valid_content)
            require(
                corrected_checksum == valid_checksum
                and corrected_checksum != failed_checksum,
                "Corrected unapplied fixture checksum evidence is invalid",
            )
            self.evidence["fixture_recovery"] = {
                "version": 2,
                "name": self.spec.migration_name,
                "filename_unchanged": True,
                "failed_sha256": failed_checksum,
                "corrected_sha256": corrected_checksum,
                "unapplied_replacement": True,
                "checksum_drift_test": False,
            }
        else:
            corrected_checksum = failed_checksum
            require(
                corrected_checksum == valid_checksum,
                "Fault scenario migration bytes changed before retry",
            )
            try:
                self._remove_fault(baseline)
            except (
                ProductContractFailure,
                self.accepted.ProductContractFailure,
            ) as exc:
                raise VerificationHarnessFailure(
                    "Disposable fault-object removal or restoration failed"
                ) from exc

        self.evidence["fixture"].update(
            {
                "retry_sha256": corrected_checksum,
                "retry_uses_same_bytes_as_failed_attempt": corrected_checksum
                == failed_checksum,
            }
        )

        self._assert_success_and_idempotency(baseline, corrected_checksum)
        self.evidence.update(
            {
                "fault_point": {
                    "validated": True,
                    "kind": self.spec.kind,
                    "sql_body_after_ddl_dml": self.spec.kind == "sql_body",
                    "before_insert_trigger_on_ledger": self.spec.kind
                    == "ledger_insert",
                    "deferred_after_insert_at_commit": self.spec.kind
                    == "deferred_commit",
                },
                "baseline": {
                    "application_tables": 9,
                    "application_append_only_triggers": 4,
                    "baseline_ledger_rows": 1,
                    "adoption_rows": 1,
                    "public_schema_fingerprint": baseline.public_fingerprint,
                    "application_row_count": sum(
                        len(rows) for rows in baseline.business_rows.values()
                    ),
                    "metadata_guard_fingerprint": canonical_fingerprint(
                        baseline.metadata_catalog
                    ),
                },
            }
        )


def classify_exception(
    accepted: ModuleType | None,
    lock: ModuleType | None,
    exc: Exception,
) -> str:
    if isinstance(exc, RollbackAcceptanceError):
        return exc.result
    if accepted is not None:
        if isinstance(exc, accepted.EnvironmentBlocked):
            return RESULT_ENVIRONMENT_BLOCKED
        if isinstance(exc, accepted.IsolationBlocked):
            return RESULT_ISOLATION_BLOCKED
        if isinstance(exc, accepted.ProductContractFailure):
            return RESULT_PRODUCT_FAILURE
    if lock is not None:
        if isinstance(exc, lock.ImplementationBoundaryBlocked):
            return RESULT_BOUNDARY_BLOCKED
        if isinstance(exc, lock.EnvironmentBlocked):
            return RESULT_ENVIRONMENT_BLOCKED
        if isinstance(exc, lock.IsolationBlocked):
            return RESULT_ISOLATION_BLOCKED
        if isinstance(exc, lock.ProductContractFailure):
            return RESULT_PRODUCT_FAILURE
    return RESULT_HARNESS_FAILURE


def run_scenario(
    accepted: ModuleType,
    lock: ModuleType,
    spec: ScenarioSpec,
) -> tuple[str, str, dict[str, Any]]:
    acceptance = RollbackScenarioAcceptance(accepted, lock, spec)
    return execute_scenario_acceptance(accepted, lock, acceptance)


def execute_scenario_acceptance(
    accepted: ModuleType,
    lock: ModuleType | None,
    acceptance: Any,
) -> tuple[str, str, dict[str, Any]]:
    finalizer = FinalizerOnce(accepted, acceptance.harness)
    result = RESULT_PASS
    failure = ""
    try:
        acceptance.run()
    except Exception as exc:
        result = classify_exception(accepted, lock, exc)
        failure = sanitized_exception_message(
            exc,
            harness=acceptance.harness,
            accepted=accepted,
            lock=lock,
        )
    finally:
        result, failure = finalizer.finalize(result, failure)
    require(
        finalizer.calls == 1,
        "Disposable scenario finalization was not exactly once",
        IsolationBlocked,
    )
    cleanup_complete = (
        acceptance.harness.cleanup_complete
        and acceptance.harness.temporary_files_removed
        and (
            not acceptance.harness.state_change_attempted
            or acceptance.harness.inventory_unchanged_verified
        )
    )
    if not cleanup_complete:
        result = RESULT_ISOLATION_BLOCKED
        if not failure:
            failure = "Disposable cleanup, inventory, or control-file removal is incomplete"
    sanitized = acceptance.harness.sanitized_evidence()
    acceptance.evidence["cleanup"] = sanitized.get("cleanup", {})
    if "recovery" in sanitized:
        acceptance.evidence["recovery"] = sanitized["recovery"]
    accepted_fixture = sanitized.get("migration_fixture", {})
    acceptance.evidence["isolation_boundary"] = {
        "docker_version": sanitized.get("docker_version"),
        "compose_version": sanitized.get("compose_version"),
        "postgres_image": sanitized.get("postgres_image"),
        "migration_image": sanitized.get("migration_image"),
        "migration_image_binding": sanitized.get("migration_image_binding"),
        "migration_fixture": {
            "baseline_source_sha256": accepted_fixture.get(
                "baseline_source_sha256"
            ),
            "baseline_copy_sha256": accepted_fixture.get("baseline_copy_sha256"),
            "mount_target": accepted_fixture.get("mount_target"),
            "read_only": accepted_fixture.get("read_only"),
            "owned_temporary_source": bool(accepted_fixture),
        },
        "inventory_before": sanitized.get("inventory_before"),
        "inventory_after": sanitized.get("inventory_after"),
        "isolation": sanitized.get("isolation"),
    }
    command_results = sanitized.get("command_results", [])
    acceptance.evidence["command_audit"] = {
        "commands_recorded": len(command_results),
        "nonzero_results": [
            {"label": command["label"], "exit": command["exit"]}
            for command in command_results
            if command["exit"] != 0
        ],
        "sql_input_recorded": False,
        "raw_output_recorded": False,
    }
    acceptance.evidence["result"] = result
    if failure:
        acceptance.evidence["failure"] = failure
    assert_sanitized_evidence(acceptance.harness, acceptance.evidence)
    return result, failure, acceptance.evidence


def run_focused_regression_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    original_run = subprocess.run
    original_popen = subprocess.Popen

    def forbid_subprocess(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Focused checks must not invoke subprocesses")

    subprocess.run = forbid_subprocess  # type: ignore[assignment]
    subprocess.Popen = forbid_subprocess  # type: ignore[assignment,misc]
    try:
        blocked_stdout = io.StringIO()
        blocked_stderr = io.StringIO()
        with contextlib.redirect_stdout(blocked_stdout), contextlib.redirect_stderr(
            blocked_stderr
        ):
            blocked_exit = main([])
        require(
            blocked_exit == 2
            and blocked_stdout.getvalue().strip() == RESULT_ISOLATION_BLOCKED
            and "--confirm-disposable-synthetic-rollback-run"
            in blocked_stderr.getvalue(),
            "Missing explicit authorization did not fail closed in the real CLI path",
        )
        checks["missing_explicit_authorization_fails_closed"] = True

        markers = {scenario.marker for scenario in SCENARIOS}
        require(len(markers) == 3, "Scenario markers are not distinct")
        checks["scenario_markers_are_distinct"] = True

        scenario_a = SCENARIOS[0]
        failing_sql = migration_content(scenario_a, fail_after_dml=True).decode()
        positions = [
            failing_sql.index("CREATE SCHEMA"),
            failing_sql.index("CREATE TABLE"),
            failing_sql.index("INSERT INTO"),
            failing_sql.index("RAISE EXCEPTION"),
        ]
        require(positions == sorted(positions), "SQL-body fault precedes DDL or DML")
        checks["sql_body_failure_follows_ddl_and_dml"] = True

        immediate = SCENARIOS[1]
        deferred = SCENARIOS[2]
        fake_common: dict[str, Any] = {
            "target_schema": "smartcoat_migrations",
            "target_table": "applied_migrations",
            "enabled": "O",
            "internal": False,
            "function_arguments": "",
            "result_type": "trigger",
            "language_name": "plpgsql",
            "security_definer": False,
            "volatility": "v",
            "row_level": True,
            "insert_event": True,
            "delete_event": False,
            "update_event": False,
            "truncate_event": False,
            "instead_event": False,
        }

        def fake_fault_row(spec: ScenarioSpec, checksum: str) -> dict[str, Any]:
            return {
                **fake_common,
                "trigger_name": spec.fault_trigger,
                "function_schema": spec.fault_schema,
                "function_name": spec.fault_function,
                "when_expression": (
                    f"((new.version = 2) AND (new.name = '{spec.migration_name}') "
                    f"AND (new.sha256 = '{checksum}'))"
                ),
                "function_source": fault_function_source(spec, checksum),
                "trigger_definition": (
                    f"CREATE {'CONSTRAINT ' if spec.kind == 'deferred_commit' else ''}"
                    f"TRIGGER {spec.fault_trigger} "
                    f"{'AFTER' if spec.kind == 'deferred_commit' else 'BEFORE'} INSERT "
                    "ON smartcoat_migrations.applied_migrations FOR EACH ROW "
                    f"WHEN (new.version = 2 AND new.name = '{spec.migration_name}' "
                    f"AND new.sha256 = '{checksum}') EXECUTE FUNCTION "
                    f"{spec.fault_schema}.{spec.fault_function}()"
                ),
                "before_timing": spec.kind == "ledger_insert",
                "constraint_trigger": spec.kind == "deferred_commit",
                "trigger_deferrable": spec.kind == "deferred_commit",
                "trigger_initially_deferred": spec.kind == "deferred_commit",
                "constraint_type": "t" if spec.kind == "deferred_commit" else "",
                "constraint_deferrable": spec.kind == "deferred_commit",
                "constraint_initially_deferred": spec.kind == "deferred_commit",
            }

        checksum = "a" * 64
        immediate_sql = normalized_sql(fault_install_sql(immediate, checksum))
        deferred_sql = normalized_sql(fault_install_sql(deferred, checksum))
        require(
            f"CREATE TRIGGER {immediate.fault_trigger} BEFORE INSERT" in immediate_sql
            and "CONSTRAINT TRIGGER" not in immediate_sql
            and "DEFERRABLE" not in immediate_sql
            and f"CREATE CONSTRAINT TRIGGER {deferred.fault_trigger} AFTER INSERT"
            in deferred_sql
            and "DEFERRABLE INITIALLY DEFERRED" in deferred_sql,
            "Generated immediate and deferred fault definitions are ambiguous",
        )
        assert_fault_contract(immediate, fake_fault_row(immediate, checksum), checksum)
        assert_fault_contract(deferred, fake_fault_row(deferred, checksum), checksum)
        confused = fake_fault_row(immediate, checksum)
        confused.update(
            {
                "before_timing": False,
                "constraint_trigger": True,
                "trigger_deferrable": True,
                "trigger_initially_deferred": True,
                "constraint_type": "t",
                "constraint_deferrable": True,
                "constraint_initially_deferred": True,
            }
        )
        try:
            assert_fault_contract(immediate, confused, checksum)
        except VerificationHarnessFailure:
            checks["immediate_and_deferred_faults_cannot_be_confused"] = True
        else:
            raise AssertionError("Deferred trigger semantics passed as immediate")
        reverse_confusion = fake_fault_row(deferred, checksum)
        reverse_confusion.update(
            {
                "before_timing": True,
                "constraint_trigger": False,
                "trigger_deferrable": False,
                "trigger_initially_deferred": False,
                "constraint_type": "",
                "constraint_deferrable": False,
                "constraint_initially_deferred": False,
            }
        )
        try:
            assert_fault_contract(deferred, reverse_confusion, checksum)
        except VerificationHarnessFailure:
            pass
        else:
            raise AssertionError("Immediate trigger semantics passed as deferred")

        baseline = [{"version": 1, "sha256": "b" * 64}]
        clean_probe = {"schema_exists": False, "table_exists": False, "rows": []}
        assert_rollback_state(
            probe_state=clean_probe,
            ledger_after=baseline,
            baseline_ledger=baseline,
        )
        surviving_states = [
            {"schema_exists": True, "table_exists": False, "rows": []},
            {"schema_exists": True, "table_exists": True, "rows": []},
            {
                "schema_exists": True,
                "table_exists": True,
                "rows": [{"probe_id": "survivor"}],
            },
        ]
        for survivor in surviving_states:
            try:
                assert_rollback_state(
                    probe_state=survivor,
                    ledger_after=baseline,
                    baseline_ledger=baseline,
                )
            except ProductContractFailure:
                continue
            raise AssertionError("A surviving probe effect passed rollback assertions")
        try:
            assert_rollback_state(
                probe_state=clean_probe,
                ledger_after=[*baseline, {"version": 2, "sha256": "c" * 64}],
                baseline_ledger=baseline,
            )
        except ProductContractFailure:
            checks["surviving_probe_or_ledger_effects_fail"] = True
        else:
            raise AssertionError("A surviving version-2 ledger row passed")

        with tempfile.TemporaryDirectory(prefix="m0r0142b-focused-hashes-") as tmp:
            root = Path(tmp)
            paths = {
                f"protected-{index}.txt": sha256_bytes(b"accepted\n")
                for index in range(14)
            }
            for relative_path in paths:
                (root / relative_path).write_bytes(b"accepted\n")
            before = verify_protected_hashes(root=root, expected_hashes=paths)
            require(
                verify_protected_hashes(
                    root=root,
                    expected_hashes=paths,
                    preflight_hashes=before,
                )
                == before,
                "Unchanged focused protected boundary did not pass",
            )
            changed = root / "protected-0.txt"
            changed.write_bytes(b"changed\n")
            try:
                verify_protected_hashes(
                    root=root,
                    expected_hashes=paths,
                    preflight_hashes=before,
                )
            except ImplementationBoundaryBlocked:
                checks["changed_protected_path_blocks_implementation_boundary"] = True
            else:
                raise AssertionError("Changed protected path did not block")
            changed.write_bytes(b"accepted\n")
            changed.unlink()
            try:
                verify_protected_hashes(root=root, expected_hashes=paths)
            except ImplementationBoundaryBlocked:
                checks["missing_protected_path_blocks_implementation_boundary"] = True
            else:
                raise AssertionError("Missing protected path did not block")

        calls = 0

        class FakeHarness:
            def __init__(self) -> None:
                self.cleanup_complete = False
                self.inventory_unchanged_verified = False
                self.temporary_files_removed = False
                self.state_change_attempted = True
                self.secret_values: set[str] = set()

            @staticmethod
            def sanitized_evidence() -> dict[str, Any]:
                return {
                    "cleanup": {
                        "ownership_validated_before_removal": True,
                        "project_resources_remaining": 0,
                        "preexisting_inventory_unchanged": True,
                    },
                    "command_results": [],
                }

        class FakeAccepted:
            EnvironmentBlocked = type("FocusedEnvironmentBlocked", (Exception,), {})
            IsolationBlocked = type("FocusedIsolationBlocked", (Exception,), {})
            ProductContractFailure = type(
                "FocusedProductContractFailure", (Exception,), {}
            )

            @staticmethod
            def finalize_harness(
                harness: FakeHarness, result: str, failure: str
            ) -> tuple[str, str]:
                nonlocal calls
                calls += 1
                harness.cleanup_complete = True
                harness.inventory_unchanged_verified = True
                harness.temporary_files_removed = True
                return result, failure

        class InjectedAcceptance:
            def __init__(self) -> None:
                self.harness = FakeHarness()
                self.evidence: dict[str, Any] = {}

            @staticmethod
            def run() -> None:
                raise RuntimeError("synthetic ordinary exception")

        injected = InjectedAcceptance()
        injected_result, injected_failure, injected_evidence = (
            execute_scenario_acceptance(
                FakeAccepted(),  # type: ignore[arg-type]
                None,
                injected,
            )
        )
        require(
            calls == 1
            and injected_result == RESULT_HARNESS_FAILURE
            and injected_failure
            == "Unclassified verification failure: RuntimeError"
            and injected_evidence["cleanup"]["ownership_validated_before_removal"]
            is True,
            "Ordinary exception did not use exactly-once ownership finalization",
        )
        checks["ordinary_exception_finalizes_exactly_once"] = True
        checks["focused_checks_use_no_subprocess_docker_database_or_env"] = True
        return checks
    finally:
        subprocess.run = original_run  # type: ignore[assignment]
        subprocess.Popen = original_popen  # type: ignore[assignment,misc]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run M0-R01.4.2b only against three generated disposable synthetic "
            "PostgreSQL projects."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--confirm-disposable-synthetic-rollback-run",
        action="store_true",
        help="required explicit authorization for all three disposable rollback scenarios",
    )
    mode.add_argument(
        "--run-focused-regression-checks",
        action="store_true",
        help="run no-Docker safety-predicate regression checks",
    )
    return parser


def live_authorized(args: argparse.Namespace) -> bool:
    return bool(args.confirm_disposable_synthetic_rollback_run)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_focused_regression_checks:
        print(json.dumps(run_focused_regression_checks(), sort_keys=True, indent=2))
        print("FOCUSED_REGRESSION_CHECKS_PASS")
        return 0
    if not live_authorized(args):
        print(
            "Explicit --confirm-disposable-synthetic-rollback-run is required; "
            "no action taken.",
            file=sys.stderr,
        )
        print(RESULT_ISOLATION_BLOCKED)
        return 2

    result = RESULT_PASS
    failure = ""
    accepted: ModuleType | None = None
    lock: ModuleType | None = None
    preflight_hashes: dict[str, str] | None = None
    static_evidence: dict[str, Any] = {}
    scenario_evidence: list[dict[str, Any]] = []
    try:
        preflight_hashes = verify_protected_hashes()
        accepted, lock = load_accepted_harnesses()
        static_evidence = {
            "protected_hashes_before": preflight_hashes,
            "repository": lock.repository_evidence(),
            "authenticated_harnesses": {
                "lifecycle": LIFECYCLE_HARNESS_SHA256,
                "lock": LOCK_HARNESS_SHA256,
            },
            "migration_advisory_lock": {
                "key": EXPECTED_MIGRATION_LOCK_KEY,
                **EXPECTED_LOCK_PARTS,
            },
        }
        for spec in SCENARIOS:
            verify_protected_hashes(preflight_hashes=preflight_hashes)
            scenario_result, scenario_failure, evidence = run_scenario(
                accepted, lock, spec
            )
            scenario_evidence.append(evidence)
            try:
                after_scenario = verify_protected_hashes(
                    preflight_hashes=preflight_hashes
                )
                evidence["protected_hashes_after"] = after_scenario
                evidence["protected_boundary"] = {
                    "accepted_values_verified": True,
                    "preflight_values_verified": True,
                    "paths_verified": len(after_scenario),
                }
            except Exception as hash_error:
                result = RESULT_BOUNDARY_BLOCKED
                failure = (
                    "Post-scenario protected-boundary verification failed: "
                    f"{type(hash_error).__name__}: {hash_error}"
                )
                evidence["result"] = result
                evidence["failure"] = failure
                break
            if scenario_result != RESULT_PASS:
                result = scenario_result
                failure = scenario_failure
                break
        if result == RESULT_PASS and len(scenario_evidence) != len(SCENARIOS):
            raise VerificationHarnessFailure("Not all rollback scenarios were executed")
    except Exception as exc:
        result = classify_exception(accepted, lock, exc)
        failure = sanitized_exception_message(
            exc,
            accepted=accepted,
            lock=lock,
        )
    finally:
        if preflight_hashes is not None:
            try:
                static_evidence["protected_hashes_final"] = verify_protected_hashes(
                    preflight_hashes=preflight_hashes
                )
            except Exception as hash_error:
                result = RESULT_BOUNDARY_BLOCKED
                failure = (
                    "Final protected-boundary verification failed: "
                    f"{type(hash_error).__name__}: {hash_error}"
                )

    output = {
        "ticket": "M0-R01.4.2b",
        "result": result,
        "failure": failure or None,
        "static_evidence": static_evidence,
        "scenarios": scenario_evidence,
        "scenarios_completed": len(scenario_evidence),
        "scenarios_expected": len(SCENARIOS),
    }
    print(json.dumps(output, sort_keys=True, indent=2))
    print(result)
    return 0 if result == RESULT_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
