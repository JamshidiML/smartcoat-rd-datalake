#!/usr/bin/env python3
"""Opt-in M0-R01.4.2c.2 checksum-only and name-only drift acceptance.

The default and focused modes remain offline.  The explicit live flag creates and
fully finalizes only two isolated disposable synthetic PostgreSQL projects; later
missing-history and non-prefix scenarios remain deferred to M0-R01.4.2c.3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, NoReturn


RESULT_ISOLATION_BLOCKED = "BLOCKED_ISOLATION"
RESULT_BOUNDARY_BLOCKED = "BLOCKED_IMPLEMENTATION_BOUNDARY"
RESULT_ENVIRONMENT_BLOCKED = "BLOCKED_ENVIRONMENT"
RESULT_PRODUCT_FAILURE = "FAIL_PRODUCT_CONTRACT"
RESULT_HARNESS_FAILURE = "FAIL_VERIFICATION_HARNESS"
RESULT_LIVE_PASS = "PASS_M0_R01_4_2C_2"
FOCUSED_PASS = "FOCUSED_REGRESSION_CHECKS_PASS"

EXPECTED_MIGRATION_EXIT = 2
EXPECTED_MIGRATION_LOCK_KEY = 5999724105712152625
ROLLBACK_HARNESS_RELATIVE_PATH = (
    "infra/postgres/tests/live_migration_rollback_acceptance.py"
)
DRIFT_HARNESS_RELATIVE_PATH = (
    "infra/postgres/tests/live_migration_drift_acceptance.py"
)
ROLLBACK_HARNESS_SHA256 = (
    "dd549b3f9d51e9843c5db6c2127479eb6a6e0e8cef104d08ddef612d19b4ac16"
)

CHECKSUM_OR_NAME_DRIFT_ERROR = (
    "Migration error: Applied migration 0002 no longer matches its recorded "
    "name and checksum"
)
MISSING_HISTORY_ERROR = (
    "Migration error: Applied migration 0002 is missing from the repository"
)
NON_PREFIX_HISTORY_ERROR = (
    "Migration error: Applied migration history is not an ordered prefix of "
    "discovered migrations; refusing an unsafe out-of-order migration"
)

VALID_MIGRATION_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_EVIDENCE_VALUE_PATTERN = re.compile(
    r"(?:"
    r"\bpostgres(?:ql)?://"
    r"|\b(?:MIGRATION_DATABASE_URL|DATABASE_URL|PGPASSWORD|POSTGRES_PASSWORD|"
    r"password|passfile)\s*="
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@"
    r")",
    re.IGNORECASE,
)
PROHIBITED_EVIDENCE_KEY_TOKENS = {
    "credential",
    "credentials",
    "dsn",
    "env",
    "environ",
    "environment",
    "output",
    "passfile",
    "password",
    "pgpassword",
    "secret",
    "stderr",
    "stdout",
    "token",
}
PROHIBITED_EVIDENCE_KEY_FRAGMENTS = {
    "connectionurl",
    "credential",
    "databaseurl",
    "environment",
    "environmentpayload",
    "envpayload",
    "migrationdatabaseurl",
    "output",
    "passfile",
    "password",
    "postgrespassword",
    "privatekey",
    "rawoutput",
    "rawstderr",
    "rawstdout",
    "secret",
    "stderr",
    "stdout",
    "subprocessoutput",
    "token",
}

PROTECTED_HASHES = {
    "infra/postgres/tests/live_migration_lifecycle_acceptance.py": (
        "4d7fbe8d33d36b6ff50161f4374cf16477667903253b790cbc37cb3e54707cfd"
    ),
    "infra/postgres/tests/live_migration_lock_acceptance.py": (
        "dca6c0c8473c72134f68a11938be24324c6bbdbffc77c9dd7d56ed4bab736b53"
    ),
    "infra/postgres/tests/live_migration_rollback_acceptance.py": (
        "dd549b3f9d51e9843c5db6c2127479eb6a6e0e8cef104d08ddef612d19b4ac16"
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


class DriftHarnessError(RuntimeError):
    """Base class for offline drift-contract failures."""

    result = RESULT_HARNESS_FAILURE


class VerificationHarnessFailure(DriftHarnessError):
    """The offline harness or its supplied evidence violated its contract."""


class ProductContractFailure(DriftHarnessError):
    """Live PostgreSQL behavior violated the accepted product contract."""

    result = RESULT_PRODUCT_FAILURE


class IsolationBlocked(DriftHarnessError):
    """The disposable-resource or inventory boundary could not be proven."""

    result = RESULT_ISOLATION_BLOCKED


class EnvironmentBlocked(DriftHarnessError):
    """The accepted local live-test environment was unavailable."""

    result = RESULT_ENVIRONMENT_BLOCKED


class ImplementationBoundaryBlocked(DriftHarnessError):
    """An accepted protected-path value is missing or changed."""

    result = RESULT_BOUNDARY_BLOCKED


class ExpectedDriftObservation(DriftHarnessError):
    """Pure fake used to exercise expected-exception finalization."""


def require(
    condition: bool,
    message: str,
    error_type: type[DriftHarnessError] = VerificationHarnessFailure,
) -> None:
    if not condition:
        raise error_type(message)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class MigrationFixture:
    version: int
    name: str
    sql: bytes
    sha256: str

    @property
    def filename(self) -> str:
        return f"{self.version:04d}__{self.name}.sql"


def migration_fixture(version: int, name: str, sql: bytes) -> MigrationFixture:
    require(version > 0, "Migration version must be positive")
    require(
        VALID_MIGRATION_NAME.fullmatch(name) is not None,
        "Migration name must be valid lower snake case",
    )
    require(bool(sql), "Migration SQL bytes must not be empty")
    return MigrationFixture(version, name, sql, sha256_bytes(sql))


@dataclass(frozen=True)
class DriftComparison:
    kind: str
    recorded: MigrationFixture
    discovered: MigrationFixture | None
    expected_error: str


@dataclass(frozen=True)
class HistoryComparison:
    kind: str
    applied_versions: tuple[int, ...]
    discovered_versions: tuple[int, ...]
    expected_error: str


@dataclass(frozen=True)
class ScenarioContract:
    key: str
    kind: str
    expected_error: str
    later_live_scope: str


SCENARIO_CONTRACTS = (
    ScenarioContract(
        "A",
        "checksum_only",
        CHECKSUM_OR_NAME_DRIFT_ERROR,
        "registered version 2 retains its name but discovered SQL bytes drift",
    ),
    ScenarioContract(
        "B",
        "name_only",
        CHECKSUM_OR_NAME_DRIFT_ERROR,
        "registered version 2 retains SQL bytes but its discovered name drifts",
    ),
    ScenarioContract(
        "C",
        "missing_registered_migration",
        MISSING_HISTORY_ERROR,
        "registered version 2 has no discovered version-2 fixture",
    ),
    ScenarioContract(
        "D",
        "non_prefix_history",
        NON_PREFIX_HISTORY_ERROR,
        "applied history 1,3 is compared with discovered history 1,2,3,4",
    ),
)


@dataclass(frozen=True)
class LiveDriftScenario:
    key: str
    kind: str
    registered_name: str
    drift_name: str
    registered_schema: str
    registered_table: str
    registered_probe_id: str
    registered_probe_value: str
    sentinel_name: str
    sentinel_schema: str
    sentinel_table: str
    sentinel_probe_id: str
    sentinel_probe_value: str

    @property
    def registered_filename(self) -> str:
        return f"0002__{self.registered_name}.sql"

    @property
    def drift_filename(self) -> str:
        return f"0002__{self.drift_name}.sql"

    @property
    def sentinel_filename(self) -> str:
        return f"0003__{self.sentinel_name}.sql"

    @property
    def probe_schema(self) -> str:
        return self.registered_schema

    @property
    def probe_table(self) -> str:
        return self.registered_table


LIVE_SCENARIOS = (
    LiveDriftScenario(
        key="A",
        kind="checksum_only",
        registered_name="drift_checksum_registered",
        drift_name="drift_checksum_registered",
        registered_schema="m0r0142c2a_registered",
        registered_table="registered_probe",
        registered_probe_id="m0-r01-4-2c-2-a-registered",
        registered_probe_value="synthetic-checksum-registered-probe",
        sentinel_name="drift_checksum_sentinel",
        sentinel_schema="m0r0142c2a_sentinel",
        sentinel_table="sentinel_probe",
        sentinel_probe_id="m0-r01-4-2c-2-a-sentinel",
        sentinel_probe_value="synthetic-checksum-sentinel-probe",
    ),
    LiveDriftScenario(
        key="B",
        kind="name_only",
        registered_name="drift_name_registered",
        drift_name="drift_name_renamed",
        registered_schema="m0r0142c2b_registered",
        registered_table="registered_probe",
        registered_probe_id="m0-r01-4-2c-2-b-registered",
        registered_probe_value="synthetic-name-registered-probe",
        sentinel_name="drift_name_sentinel",
        sentinel_schema="m0r0142c2b_sentinel",
        sentinel_table="sentinel_probe",
        sentinel_probe_id="m0-r01-4-2c-2-b-sentinel",
        sentinel_probe_value="synthetic-name-sentinel-probe",
    ),
)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def live_probe_migration_content(
    *,
    schema: str,
    table: str,
    probe_id: str,
    probe_value: str,
    observed_at_utc: str,
) -> bytes:
    return f"""CREATE SCHEMA {schema};

CREATE TABLE {schema}.{table} (
    probe_id text PRIMARY KEY,
    probe_value text NOT NULL,
    observed_at_utc timestamptz NOT NULL
);

INSERT INTO {schema}.{table} (
    probe_id,
    probe_value,
    observed_at_utc
) VALUES (
    {sql_literal(probe_id)},
    {sql_literal(probe_value)},
    TIMESTAMPTZ {sql_literal(observed_at_utc)}
);
""".encode("utf-8")


def registered_migration_content(spec: LiveDriftScenario) -> bytes:
    return live_probe_migration_content(
        schema=spec.registered_schema,
        table=spec.registered_table,
        probe_id=spec.registered_probe_id,
        probe_value=spec.registered_probe_value,
        observed_at_utc="2026-03-01T00:00:00Z",
    )


def sentinel_migration_content(spec: LiveDriftScenario) -> bytes:
    return live_probe_migration_content(
        schema=spec.sentinel_schema,
        table=spec.sentinel_table,
        probe_id=spec.sentinel_probe_id,
        probe_value=spec.sentinel_probe_value,
        observed_at_utc="2026-03-02T00:00:00Z",
    )


def drifted_registered_content(spec: LiveDriftScenario) -> bytes:
    original = registered_migration_content(spec)
    if spec.kind == "checksum_only":
        return original + b"\n-- synthetic checksum-only drift\n"
    return original


def validate_live_scenario_contracts() -> None:
    require(
        tuple(spec.key for spec in LIVE_SCENARIOS) == ("A", "B")
        and tuple(spec.kind for spec in LIVE_SCENARIOS)
        == ("checksum_only", "name_only"),
        "Live ticket scope is not exactly checksum-only A and name-only B",
        ImplementationBoundaryBlocked,
    )
    for spec in LIVE_SCENARIOS:
        identifiers = (
            spec.registered_name,
            spec.drift_name,
            spec.registered_schema,
            spec.registered_table,
            spec.sentinel_name,
            spec.sentinel_schema,
            spec.sentinel_table,
        )
        require(
            all(VALID_MIGRATION_NAME.fullmatch(value) for value in identifiers),
            f"Scenario {spec.key} contains an unsafe generated identifier",
            ImplementationBoundaryBlocked,
        )
        registered = migration_fixture(
            2,
            spec.registered_name,
            registered_migration_content(spec),
        )
        drifted = migration_fixture(
            2,
            spec.drift_name,
            drifted_registered_content(spec),
        )
        sentinel = migration_fixture(
            3,
            spec.sentinel_name,
            sentinel_migration_content(spec),
        )
        require(
            registered.filename == spec.registered_filename
            and drifted.filename == spec.drift_filename
            and sentinel.filename == spec.sentinel_filename
            and registered.version == drifted.version == 2
            and sentinel.version == 3,
            f"Scenario {spec.key} version or filename construction is invalid",
            ImplementationBoundaryBlocked,
        )
        if spec.kind == "checksum_only":
            require(
                registered.name == drifted.name
                and registered.filename == drifted.filename
                and registered.sql != drifted.sql
                and registered.sha256 != drifted.sha256,
                "Checksum-only live scenario changes the wrong dimension",
                ImplementationBoundaryBlocked,
            )
        else:
            require(
                registered.name != drifted.name
                and registered.filename != drifted.filename
                and registered.sql == drifted.sql
                and registered.sha256 == drifted.sha256,
                "Name-only live scenario changes the wrong dimension",
                ImplementationBoundaryBlocked,
            )


def _sha256_file(relative_path: str) -> str:
    current_path = str(__file__)
    require(
        current_path.endswith(DRIFT_HARNESS_RELATIVE_PATH),
        "Drift harness repository location is not recognizable",
        ImplementationBoundaryBlocked,
    )
    repository_prefix = current_path[: -len(DRIFT_HARNESS_RELATIVE_PATH)]
    resolved_path = repository_prefix + relative_path
    try:
        with open(resolved_path, "rb") as handle:
            return sha256_bytes(handle.read())
    except OSError as exc:
        raise ImplementationBoundaryBlocked(
            f"Protected path is unavailable or unreadable: {relative_path}: "
            f"{type(exc).__name__}"
        ) from exc


def verify_repository_protected_hashes(
    *, preflight: Mapping[str, str] | None = None
) -> dict[str, str]:
    require(
        len(PROTECTED_HASHES) == 15,
        "Protected implementation boundary is not exactly 15 paths",
        ImplementationBoundaryBlocked,
    )
    if preflight is not None:
        require(
            set(preflight) == set(PROTECTED_HASHES),
            "Protected preflight boundary has an unexpected path set",
            ImplementationBoundaryBlocked,
        )
    observed: dict[str, str] = {}
    for relative_path, accepted_hash in PROTECTED_HASHES.items():
        actual_hash = _sha256_file(relative_path)
        require(
            actual_hash == accepted_hash,
            f"Protected path differs from accepted hash: {relative_path}",
            ImplementationBoundaryBlocked,
        )
        if preflight is not None:
            require(
                preflight[relative_path] == actual_hash,
                f"Protected path differs from preflight: {relative_path}",
                ImplementationBoundaryBlocked,
            )
        observed[relative_path] = actual_hash
    return observed


def protected_hash_evidence(values: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {"path": path, "sha256": values[path]}
        for path in sorted(values)
    ]


def load_authenticated_live_dependencies() -> tuple[Any, Any, Any]:
    import importlib.util
    import sys

    before = _sha256_file(ROLLBACK_HARNESS_RELATIVE_PATH)
    require(
        before == ROLLBACK_HARNESS_SHA256,
        "Accepted rollback harness hash changed before import",
        ImplementationBoundaryBlocked,
    )
    module_name = "m0_r01_4_2b_accepted_harness_for_drift"
    module_spec = importlib.util.spec_from_file_location(
        module_name,
        str(__file__)[: -len(DRIFT_HARNESS_RELATIVE_PATH)]
        + ROLLBACK_HARNESS_RELATIVE_PATH,
    )
    require(
        module_spec is not None and module_spec.loader is not None,
        "Accepted rollback harness cannot be imported",
        ImplementationBoundaryBlocked,
    )
    rollback = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = rollback
    try:
        module_spec.loader.exec_module(rollback)
    except Exception as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted rollback harness import failed: {type(exc).__name__}"
        ) from exc
    after = _sha256_file(ROLLBACK_HARNESS_RELATIVE_PATH)
    require(
        after == before == ROLLBACK_HARNESS_SHA256,
        "Accepted rollback harness changed during import",
        ImplementationBoundaryBlocked,
    )
    required_symbols = {
        "BaselineEvidence",
        "FinalizerOnce",
        "RollbackScenarioAcceptance",
        "USER_CATALOG_QUERIES",
        "canonical_fingerprint",
        "load_accepted_harnesses",
    }
    require(
        all(hasattr(rollback, symbol) for symbol in required_symbols),
        "Accepted rollback harness lacks a required reuse symbol",
        ImplementationBoundaryBlocked,
    )
    try:
        accepted, lock = rollback.load_accepted_harnesses()
    except Exception as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted dependency authentication failed: {type(exc).__name__}"
        ) from exc
    return rollback, accepted, lock


def checksum_only_comparison() -> DriftComparison:
    recorded = migration_fixture(
        2,
        "registered_checksum_probe",
        b"SELECT 'synthetic-checksum-probe';\n",
    )
    discovered = migration_fixture(
        2,
        recorded.name,
        recorded.sql + b"-- synthetic checksum-only drift\n",
    )
    return DriftComparison(
        "checksum_only",
        recorded,
        discovered,
        CHECKSUM_OR_NAME_DRIFT_ERROR,
    )


def name_only_comparison() -> DriftComparison:
    recorded = migration_fixture(
        2,
        "registered_name_probe",
        b"SELECT 'synthetic-name-probe';\n",
    )
    discovered = migration_fixture(2, "renamed_name_probe", recorded.sql)
    return DriftComparison(
        "name_only",
        recorded,
        discovered,
        CHECKSUM_OR_NAME_DRIFT_ERROR,
    )


def missing_history_comparison() -> HistoryComparison:
    return HistoryComparison(
        "missing_registered_migration",
        (1, 2),
        (1, 3),
        MISSING_HISTORY_ERROR,
    )


def non_prefix_history_comparison() -> HistoryComparison:
    return HistoryComparison(
        "non_prefix_history",
        (1, 3),
        (1, 2, 3, 4),
        NON_PREFIX_HISTORY_ERROR,
    )


def validate_checksum_only(comparison: DriftComparison) -> None:
    require(comparison.kind == "checksum_only", "Wrong checksum drift kind")
    require(comparison.discovered is not None, "Checksum drift fixture is missing")
    discovered = comparison.discovered
    require(
        comparison.recorded.version == discovered.version == 2,
        "Checksum-only drift changed the migration version",
    )
    require(
        comparison.recorded.name == discovered.name,
        "Checksum-only drift changed the migration name",
    )
    require(
        comparison.recorded.filename == discovered.filename,
        "Checksum-only drift changed the migration filename",
    )
    require(
        comparison.recorded.sql != discovered.sql,
        "Checksum-only drift did not change SQL bytes",
    )
    require(
        comparison.recorded.sha256 != discovered.sha256,
        "Checksum-only drift did not change SHA-256",
    )


def validate_name_only(comparison: DriftComparison) -> None:
    require(comparison.kind == "name_only", "Wrong name drift kind")
    require(comparison.discovered is not None, "Name drift fixture is missing")
    discovered = comparison.discovered
    require(
        comparison.recorded.version == discovered.version == 2,
        "Name-only drift changed the migration version",
    )
    require(
        comparison.recorded.name != discovered.name,
        "Name-only drift did not change the migration name",
    )
    require(
        comparison.recorded.filename != discovered.filename,
        "Name-only drift did not change the migration filename",
    )
    require(
        comparison.recorded.sql == discovered.sql,
        "Name-only drift changed SQL bytes",
    )
    require(
        comparison.recorded.sha256 == discovered.sha256,
        "Name-only drift changed SHA-256",
    )


def validate_missing_history(comparison: HistoryComparison) -> None:
    require(
        comparison.kind == "missing_registered_migration",
        "Wrong missing-history kind",
    )
    require(
        2 in comparison.applied_versions and 2 not in comparison.discovered_versions,
        "Missing-history contract does not represent an absent registered version 2",
    )


def validate_non_prefix_history(comparison: HistoryComparison) -> None:
    require(comparison.kind == "non_prefix_history", "Wrong non-prefix kind")
    require(
        comparison.applied_versions == (1, 3)
        and comparison.discovered_versions == (1, 2, 3, 4),
        "Non-prefix history is not applied 1,3 versus discovered 1,2,3,4",
    )
    prefix = comparison.discovered_versions[: len(comparison.applied_versions)]
    require(
        comparison.applied_versions != prefix,
        "Non-prefix history unexpectedly forms an ordered prefix",
    )


def validate_error_contracts() -> None:
    distinct_errors = {
        CHECKSUM_OR_NAME_DRIFT_ERROR,
        MISSING_HISTORY_ERROR,
        NON_PREFIX_HISTORY_ERROR,
    }
    require(len(distinct_errors) == 3, "Drift error contracts are not distinct")
    expected_by_kind = {
        "checksum_only": CHECKSUM_OR_NAME_DRIFT_ERROR,
        "name_only": CHECKSUM_OR_NAME_DRIFT_ERROR,
        "missing_registered_migration": MISSING_HISTORY_ERROR,
        "non_prefix_history": NON_PREFIX_HISTORY_ERROR,
    }
    require(
        {scenario.kind: scenario.expected_error for scenario in SCENARIO_CONTRACTS}
        == expected_by_kind,
        "Scenario drift errors are confused or incomplete",
    )


@dataclass(frozen=True)
class FailureEvidence:
    pending_ledger_rows: int
    pending_probe_rows: int
    advisory_lock_rows: int


def validate_fail_closed_evidence(evidence: FailureEvidence) -> None:
    require(
        evidence.pending_ledger_rows == 0,
        "A pending migration ledger row survived drift rejection",
    )
    require(
        evidence.pending_probe_rows == 0,
        "A pending migration probe survived drift rejection",
    )
    require(
        evidence.advisory_lock_rows == 0,
        "A migration advisory-lock row survived drift rejection",
    )


@dataclass(frozen=True)
class RecoveryEvidence:
    recovered_ledger_rows: int
    recovered_probe_rows: int
    idempotent_applied_now: int


def validate_recovery_evidence(evidence: RecoveryEvidence) -> None:
    require(
        evidence.recovered_ledger_rows == 1,
        "Recovery did not produce exactly one ledger row",
    )
    require(
        evidence.recovered_probe_rows == 1,
        "Recovery did not produce exactly one probe row",
    )
    require(
        evidence.idempotent_applied_now == 0,
        "Recovery reapplication was not idempotent",
    )


def verify_protected_hash_values(
    observed: Mapping[str, str],
    *,
    accepted: Mapping[str, str] = PROTECTED_HASHES,
    preflight: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if len(accepted) != 15:
        raise ImplementationBoundaryBlocked(
            "Protected implementation boundary is not exactly 15 paths"
        )
    if set(observed) != set(accepted):
        raise ImplementationBoundaryBlocked(
            "Protected path set is missing or contains an unexpected path"
        )
    if preflight is not None and set(preflight) != set(accepted):
        raise ImplementationBoundaryBlocked("Protected preflight path set differs")
    verified: dict[str, str] = {}
    for path, accepted_hash in accepted.items():
        actual_hash = observed[path]
        if (
            SHA256_PATTERN.fullmatch(actual_hash) is None
            or actual_hash != accepted_hash
        ):
            raise ImplementationBoundaryBlocked(
                f"Protected path differs from accepted hash: {path}"
            )
        if preflight is not None and preflight[path] != actual_hash:
            raise ImplementationBoundaryBlocked(
                f"Protected path differs from preflight: {path}"
            )
        verified[path] = actual_hash
    return verified


def _normalized_evidence_key(key: Any) -> str:
    if key is None:
        return "null"
    if isinstance(key, bool):
        return str(key).lower()
    if isinstance(key, int):
        return str(key)
    if isinstance(key, float):
        if not math.isfinite(key):
            raise VerificationHarnessFailure("Evidence contains an unsafe key")
        return str(key)
    if not isinstance(key, str):
        raise VerificationHarnessFailure("Evidence contains an unsafe key")
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if not normalized:
        raise VerificationHarnessFailure("Evidence contains an unsafe key")
    return normalized


def _is_prohibited_evidence_key(normalized: str) -> bool:
    tokens = set(normalized.split("_"))
    collapsed = normalized.replace("_", "")
    if tokens.intersection(PROHIBITED_EVIDENCE_KEY_TOKENS):
        return True
    if any(part in collapsed for part in PROHIBITED_EVIDENCE_KEY_FRAGMENTS):
        return True
    return "url" in tokens and bool(
        tokens.intersection(
            {"connection", "database", "migration", "postgres", "postgresql"}
        )
    )


def assert_sanitized_evidence(value: Any, *, path: str = "evidence") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_evidence_key(key)
            if _is_prohibited_evidence_key(normalized):
                raise VerificationHarnessFailure(
                    f"Evidence contains prohibited field at {path}"
                )
            assert_sanitized_evidence(nested, path=f"{path}.{normalized}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            assert_sanitized_evidence(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if SENSITIVE_EVIDENCE_VALUE_PATTERN.search(value):
            raise VerificationHarnessFailure(
                f"Evidence contains a connection secret at {path}"
            )
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise VerificationHarnessFailure(
            f"Evidence contains a non-finite number at {path}"
        )
    raise VerificationHarnessFailure(
        f"Evidence contains an unsupported value type at {path}"
    )


class FinalizerOnce:
    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self.calls = 0

    def finalize(self) -> None:
        require(self.calls == 0, "Finalization was invoked more than once")
        self.calls += 1
        self._callback()


@dataclass(frozen=True)
class OrchestrationResult:
    classification: str
    finalizer_calls: int


def execute_with_finalization(
    action: Callable[[], None],
    finalizer: FinalizerOnce,
) -> OrchestrationResult:
    classification = "PASS"
    try:
        action()
    except ExpectedDriftObservation:
        classification = "EXPECTED_DRIFT"
    except Exception:
        classification = "ORDINARY_EXCEPTION"
    finally:
        finalizer.finalize()
    return OrchestrationResult(classification, finalizer.calls)


def _expect_verification_failure(action: Callable[[], None], message: str) -> None:
    try:
        action()
    except VerificationHarnessFailure:
        return
    raise VerificationHarnessFailure(message)


def _expect_boundary_block(action: Callable[[], None], message: str) -> None:
    try:
        action()
    except ImplementationBoundaryBlocked as exc:
        require(
            exc.result == RESULT_BOUNDARY_BLOCKED,
            "Protected-path failure has the wrong classification",
        )
        return
    raise VerificationHarnessFailure(message)


def _raise_expected_drift() -> NoReturn:
    raise ExpectedDriftObservation("synthetic expected drift")


def _raise_ordinary_exception() -> NoReturn:
    raise RuntimeError("synthetic ordinary exception")


def run_focused_regression_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}

    validate_live_scenario_contracts()
    require(
        {spec.key for spec in LIVE_SCENARIOS} == {"A", "B"}
        and not {"C", "D"}.intersection(spec.key for spec in LIVE_SCENARIOS),
        "Later drift scenarios entered the M0-R01.4.2c.2 live scope",
    )
    checks["live_scope_constructs_only_checksum_and_name_scenarios"] = True

    checksum = checksum_only_comparison()
    validate_checksum_only(checksum)
    checks["checksum_only_changes_bytes_and_sha_only"] = True

    name = name_only_comparison()
    validate_name_only(name)
    checks["name_only_changes_valid_name_only"] = True

    missing = missing_history_comparison()
    validate_missing_history(missing)
    checks["missing_history_has_registered_version_2_without_fixture"] = True

    non_prefix = non_prefix_history_comparison()
    validate_non_prefix_history(non_prefix)
    checks["non_prefix_is_applied_1_3_vs_discovered_1_2_3_4"] = True

    validate_error_contracts()
    checks["exact_drift_errors_are_distinct_and_mapped"] = True

    validate_fail_closed_evidence(FailureEvidence(0, 0, 0))
    for survivor, label in (
        (FailureEvidence(1, 0, 0), "ledger"),
        (FailureEvidence(0, 1, 0), "probe"),
        (FailureEvidence(0, 0, 1), "advisory_lock"),
    ):
        _expect_verification_failure(
            lambda evidence=survivor: validate_fail_closed_evidence(evidence),
            f"A surviving {label} passed fail-closed verification",
        )
    checks["ledger_probe_and_lock_survivors_fail"] = True

    validate_recovery_evidence(RecoveryEvidence(1, 1, 0))
    for invalid in (
        RecoveryEvidence(0, 1, 0),
        RecoveryEvidence(2, 1, 0),
        RecoveryEvidence(1, 0, 0),
        RecoveryEvidence(1, 2, 0),
        RecoveryEvidence(1, 1, 1),
    ):
        _expect_verification_failure(
            lambda evidence=invalid: validate_recovery_evidence(evidence),
            "Invalid recovery evidence passed",
        )
    checks["recovery_requires_one_ledger_one_probe_and_idempotency"] = True

    finalizer_observations: list[str] = []
    expected_finalizer = FinalizerOnce(
        lambda: finalizer_observations.append("expected")
    )
    expected_result = execute_with_finalization(
        _raise_expected_drift,
        expected_finalizer,
    )
    ordinary_finalizer = FinalizerOnce(
        lambda: finalizer_observations.append("ordinary")
    )
    ordinary_result = execute_with_finalization(
        _raise_ordinary_exception,
        ordinary_finalizer,
    )
    require(
        expected_result == OrchestrationResult("EXPECTED_DRIFT", 1)
        and ordinary_result == OrchestrationResult("ORDINARY_EXCEPTION", 1)
        and finalizer_observations == ["expected", "ordinary"],
        "Expected and ordinary exceptions did not finalize exactly once",
    )
    _expect_verification_failure(
        expected_finalizer.finalize,
        "A second finalizer invocation was accepted",
    )
    checks["expected_and_ordinary_exceptions_finalize_once"] = True

    safe_evidence = {
        "scenario": "A",
        "project": "m0r0142c_synthetic_project",
        "database": "m0r0142c_synthetic_database",
        "runner": {"exit": 2, "classification": "MIGRATION_DRIFT"},
        "migration": {
            "version": checksum.recorded.version,
            "name": checksum.recorded.name,
            "recorded_sha256": checksum.recorded.sha256,
            "discovered_sha256": checksum.discovered.sha256
            if checksum.discovered is not None
            else None,
        },
        "rollback": {
            "ledger_rows": 0,
            "probe_rows": 0,
            "advisory_lock_rows": 0,
        },
        "cleanup": {
            "complete": True,
            "project_resources_remaining": 0,
            "inventory_before": {
                "containers": 3,
                "networks": 2,
                "volumes": 1,
            },
            "inventory_after": {
                "containers": 3,
                "networks": 2,
                "volumes": 1,
            },
        },
        "duration_seconds": 0.125,
        "optional_note": None,
        "safe_collections": ["sanitized", 1, 1.5, False, None, (0, 0, 0)],
        "safe_scalar_keys": {
            None: "null_key",
            False: "boolean_key",
            7: "integer_key",
            0.5: "finite_float_key",
        },
    }
    assert_sanitized_evidence(safe_evidence)
    sensitive_marker = "m0r0142c-sensitive-evidence-marker"

    class UnsupportedEvidenceValue:
        pass

    prohibited_records = (
        {"stdout": sensitive_marker},
        {"stderr": sensitive_marker},
        {"output": sensitive_marker},
        {"raw output": sensitive_marker},
        {"runner": {"process-output": sensitive_marker}},
        {"environment": {"SAFE_NAME": sensitive_marker}},
        {"env_payload": {"SAFE_NAME": sensitive_marker}},
        {"dsn": sensitive_marker},
        {"database-url": sensitive_marker},
        {"migrationDatabaseUrl": sensitive_marker},
        {"password": sensitive_marker},
        {"secret": sensitive_marker},
        {"token": sensitive_marker},
        {"credential": sensitive_marker},
        {"passfile": sensitive_marker},
        {"private-key": sensitive_marker},
        {"detail": "postgresql://synthetic.invalid/database"},
        {"detail": "MIGRATION_DATABASE_URL = synthetic"},
        {"detail": "database_url=synthetic"},
        {"detail": "pgpassword = synthetic"},
        {"detail": "POSTGRES_PASSWORD=synthetic"},
        {"detail": "Password = synthetic"},
        {"detail": "passfile = synthetic"},
        {"detail": "host=localhost user=synthetic password=synthetic"},
        {"detail": "postgresql://synthetic:synthetic@localhost/database"},
        {"detail": "https://synthetic:synthetic@example.invalid/path"},
        {"detail": b"synthetic output"},
        {"detail": UnsupportedEvidenceValue()},
        {"detail": float("nan")},
        {"detail": float("inf")},
        {b"unsafe-key": sensitive_marker},
        {"": sensitive_marker},
        {UnsupportedEvidenceValue(): sensitive_marker},
    )
    for record in prohibited_records:
        try:
            assert_sanitized_evidence(record)
        except VerificationHarnessFailure as exc:
            require(
                sensitive_marker not in str(exc),
                "Sanitization failure disclosed the rejected evidence value",
            )
        else:
            raise VerificationHarnessFailure(
                "Secret-bearing, raw, or unsupported evidence was accepted"
            )
    checks["secret_raw_and_unsupported_evidence_are_rejected"] = True

    verified = verify_protected_hash_values(PROTECTED_HASHES)
    require(verified == PROTECTED_HASHES, "Accepted protected values did not pass")
    changed = dict(PROTECTED_HASHES)
    changed[next(iter(changed))] = "0" * 64
    _expect_boundary_block(
        lambda: verify_protected_hash_values(changed),
        "Changed protected value did not block",
    )
    missing_path = dict(PROTECTED_HASHES)
    missing_path.pop(next(iter(missing_path)))
    _expect_boundary_block(
        lambda: verify_protected_hash_values(missing_path),
        "Missing protected path did not block",
    )
    changed_preflight = dict(PROTECTED_HASHES)
    changed_preflight[next(iter(changed_preflight))] = "f" * 64
    _expect_boundary_block(
        lambda: verify_protected_hash_values(
            PROTECTED_HASHES,
            preflight=changed_preflight,
        ),
        "Changed preflight value did not block",
    )
    checks["protected_mismatch_maps_to_implementation_boundary"] = True

    forbidden_module_names = {
        "docker",
        "os",
        "pathlib",
        "psycopg",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    }
    require(
        not forbidden_module_names.intersection(globals()),
        "Focused harness loaded an effect-capable module",
    )
    require(
        all(isinstance(item.recorded.sql, bytes) for item in (checksum, name)),
        "Focused drift fixtures are not pure in-memory bytes",
    )
    checks["focused_mode_has_no_live_or_mutable_fixture_effects"] = True

    return checks


def make_live_scenario_acceptance_class(rollback: Any) -> type[Any]:
    class LiveDriftScenarioAcceptance(rollback.RollbackScenarioAcceptance):
        def __init__(
            self,
            accepted: Any,
            lock: Any,
            spec: LiveDriftScenario,
            expected_inventory_fingerprint: str | None = None,
        ) -> None:
            self.accepted = accepted
            self.lock = lock
            self.spec = spec
            self.harness = accepted.LiveMigrationLifecycleAcceptance()
            self.version_2_fixture = (
                self.harness.migration_fixture_directory
                / self.spec.registered_filename
            )
            self.drifted_version_2_fixture = (
                self.harness.migration_fixture_directory / self.spec.drift_filename
            )
            self.version_3_fixture = (
                self.harness.migration_fixture_directory / self.spec.sentinel_filename
            )
            self.harness.pending_migration_fixture = self.version_2_fixture
            self.container_id = ""
            self.inventory_fingerprint_before = ""
            self.expected_inventory_fingerprint = expected_inventory_fingerprint
            self.evidence: dict[str, Any] = {
                "scenario": spec.key,
                "scenario_type": spec.kind,
                "project": self.harness.project,
                "database": self.harness.database_name,
                "checks": [],
            }

        def _assert_fixture_set(self, current_version_2: Any) -> None:
            harness = self.harness
            directory = harness.migration_fixture_directory.resolve()
            require(
                directory.parent == harness.temporary_directory.resolve()
                and directory.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(directory.stat().st_mode) == 0o700,
                "Temporary migration directory ownership or mode is unsafe",
                IsolationBlocked,
            )
            expected = {
                harness.baseline_fixture.resolve(),
                current_version_2.resolve(),
            }
            if self.version_3_fixture.exists():
                expected.add(self.version_3_fixture.resolve())
            observed = {path.resolve() for path in directory.iterdir()}
            require(
                observed == expected,
                "Temporary migration directory contains an unexpected fixture",
                IsolationBlocked,
            )
            for path in expected:
                metadata = path.stat()
                require(
                    metadata.st_uid == rollback.os.getuid()
                    and rollback.stat.S_IMODE(metadata.st_mode) == 0o400,
                    "Temporary migration fixture ownership or mode is unsafe",
                    IsolationBlocked,
                )

        def _create_fixture(self, path: Any, content: bytes) -> str:
            directory = self.harness.migration_fixture_directory.resolve()
            require(
                path.parent.resolve() == directory and not path.exists(),
                "Synthetic fixture creation target is not a new owned path",
                IsolationBlocked,
            )
            descriptor = -1
            try:
                descriptor = rollback.os.open(
                    path,
                    rollback.os.O_WRONLY
                    | rollback.os.O_CREAT
                    | rollback.os.O_EXCL,
                    0o400,
                )
                written = 0
                while written < len(content):
                    written += rollback.os.write(descriptor, content[written:])
                rollback.os.fsync(descriptor)
                rollback.os.close(descriptor)
                descriptor = -1
                path.chmod(0o400)
            finally:
                if descriptor >= 0:
                    rollback.os.close(descriptor)
            checksum = sha256_bytes(path.read_bytes())
            require(
                checksum == sha256_bytes(content)
                and path.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(path.stat().st_mode) == 0o400,
                "Synthetic fixture bytes, ownership, or permissions changed",
                IsolationBlocked,
            )
            return checksum

        def _replace_fixture_bytes(self, path: Any, content: bytes) -> str:
            directory = self.harness.migration_fixture_directory.resolve()
            target = path.resolve()
            require(
                target.parent == directory
                and target.is_file()
                and target.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(target.stat().st_mode) == 0o400,
                "Fixture replacement target is not an owned read-only fixture",
                IsolationBlocked,
            )
            staging = directory / f".{target.name}.replacement"
            require(
                not staging.exists(),
                "Fixture replacement staging path already exists",
                IsolationBlocked,
            )
            descriptor = -1
            try:
                descriptor = rollback.os.open(
                    staging,
                    rollback.os.O_WRONLY
                    | rollback.os.O_CREAT
                    | rollback.os.O_EXCL,
                    0o400,
                )
                written = 0
                while written < len(content):
                    written += rollback.os.write(descriptor, content[written:])
                rollback.os.fsync(descriptor)
                rollback.os.close(descriptor)
                descriptor = -1
                staging.chmod(0o400)
                require(
                    staging.read_bytes() == content
                    and staging.stat().st_uid == rollback.os.getuid()
                    and rollback.stat.S_IMODE(staging.stat().st_mode) == 0o400,
                    "Replacement staging fixture is unsafe",
                    IsolationBlocked,
                )
                rollback.os.replace(staging, target)
            finally:
                if descriptor >= 0:
                    rollback.os.close(descriptor)
                if staging.exists():
                    require(
                        staging.resolve().parent == directory
                        and staging.stat().st_uid == rollback.os.getuid(),
                        "Refusing to remove an unowned replacement fixture",
                        IsolationBlocked,
                    )
                    staging.unlink()
            checksum = sha256_bytes(target.read_bytes())
            require(
                checksum == sha256_bytes(content)
                and target.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(target.stat().st_mode) == 0o400,
                "Atomic fixture replacement did not preserve its contract",
                IsolationBlocked,
            )
            return checksum

        def _rename_fixture(self, source: Any, target: Any) -> str:
            directory = self.harness.migration_fixture_directory.resolve()
            source_path = source.resolve()
            require(
                source_path.parent == directory
                and source_path.is_file()
                and source_path.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(source_path.stat().st_mode) == 0o400
                and target.parent.resolve() == directory
                and not target.exists(),
                "Name-drift rename boundary is unsafe",
                IsolationBlocked,
            )
            before = sha256_bytes(source_path.read_bytes())
            rollback.os.replace(source_path, target)
            require(
                not source.exists()
                and target.is_file()
                and target.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(target.stat().st_mode) == 0o400
                and sha256_bytes(target.read_bytes()) == before,
                "Name-only fixture rename changed bytes or ownership",
                IsolationBlocked,
            )
            return before

        def _probe_state_for(self, label: str, schema: str, table: str) -> dict[str, Any]:
            harness = self.harness
            state_rows = harness._psql_rows(
                f"{label}_existence",
                f"""
                    SELECT to_regnamespace({sql_literal(schema)})
                               IS NOT NULL AS schema_exists,
                           to_regclass({sql_literal(schema + '.' + table)})
                               IS NOT NULL AS table_exists
                """,
            )
            require(
                len(state_rows) == 1,
                "Probe existence query returned incomplete evidence",
                ProductContractFailure,
            )
            state = state_rows[0]
            rows: list[dict[str, Any]] = []
            if state["table_exists"]:
                rows = harness._psql_rows(
                    f"{label}_rows",
                    f"""
                        SELECT probe_id, probe_value,
                               observed_at_utc::text AS observed_at_utc
                        FROM {schema}.{table}
                        ORDER BY probe_id
                    """,
                )
            return {
                "schema_exists": state["schema_exists"],
                "table_exists": state["table_exists"],
                "rows": rows,
            }

        def _expected_registered_probe(self) -> dict[str, Any]:
            return {
                "schema_exists": True,
                "table_exists": True,
                "rows": [
                    {
                        "probe_id": self.spec.registered_probe_id,
                        "probe_value": self.spec.registered_probe_value,
                        "observed_at_utc": "2026-03-01 00:00:00+00",
                    }
                ],
            }

        def _expected_sentinel_probe(self) -> dict[str, Any]:
            return {
                "schema_exists": True,
                "table_exists": True,
                "rows": [
                    {
                        "probe_id": self.spec.sentinel_probe_id,
                        "probe_value": self.spec.sentinel_probe_value,
                        "observed_at_utc": "2026-03-02 00:00:00+00",
                    }
                ],
            }

        def _capture_established_state(
            self,
            baseline: Any,
            registered_sha256: str,
        ) -> dict[str, Any]:
            harness = self.harness
            ledger = harness._ledger_rows(
                f"scenario_{self.spec.key}_ledger_after_registered_apply"
            )
            require(
                len(ledger) == 2
                and ledger[0] == baseline.ledger[0]
                and ledger[1]["version"] == 2
                and ledger[1]["name"] == self.spec.registered_name
                and ledger[1]["sha256"] == registered_sha256
                and ledger[1]["applied_by"] == harness.admin_user
                and bool(ledger[1]["applied_at_utc"]),
                "Registered version-2 ledger evidence is incorrect",
                ProductContractFailure,
            )
            registered_probe = self._probe_state_for(
                f"scenario_{self.spec.key}_registered_probe_established",
                self.spec.registered_schema,
                self.spec.registered_table,
            )
            require(
                registered_probe == self._expected_registered_probe(),
                "Registered version-2 probe was not committed exactly once",
                ProductContractFailure,
            )
            adoption = harness._adoption_rows(
                f"scenario_{self.spec.key}_adoption_after_registered_apply"
            )
            public = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_public_after_registered_apply",
                self.accepted.PUBLIC_CATALOG_QUERIES,
            )
            business = harness._business_snapshot(
                f"scenario_{self.spec.key}_business_after_registered_apply"
            )
            metadata = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_metadata_after_registered_apply",
                self.accepted.METADATA_CATALOG_QUERIES,
            )
            harness._assert_metadata_contract(metadata)
            user_catalog = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_user_catalog_after_registered_apply",
                rollback.USER_CATALOG_QUERIES,
            )
            require(
                adoption == baseline.adoption
                and public == baseline.public_catalog
                and self.accepted.canonical_fingerprint(public)
                == baseline.public_fingerprint
                and business == baseline.business_rows
                and metadata == baseline.metadata_catalog
                and self._lock_rows(
                    f"scenario_{self.spec.key}_locks_after_registered_apply"
                )
                == [],
                "Registering version 2 changed accepted application or metadata state",
                ProductContractFailure,
            )
            return {
                "ledger": ledger,
                "adoption": adoption,
                "public": public,
                "business": business,
                "metadata": metadata,
                "user_catalog": user_catalog,
                "registered_probe": registered_probe,
                "public_fingerprint": baseline.public_fingerprint,
            }

        def _assert_failure_state(self, established: dict[str, Any]) -> None:
            harness = self.harness
            sentinel = self._probe_state_for(
                f"scenario_{self.spec.key}_sentinel_after_failure",
                self.spec.sentinel_schema,
                self.spec.sentinel_table,
            )
            ledger = harness._ledger_rows(
                f"scenario_{self.spec.key}_ledger_after_drift_failure"
            )
            adoption = harness._adoption_rows(
                f"scenario_{self.spec.key}_adoption_after_drift_failure"
            )
            public = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_public_after_drift_failure",
                self.accepted.PUBLIC_CATALOG_QUERIES,
            )
            business = harness._business_snapshot(
                f"scenario_{self.spec.key}_business_after_drift_failure"
            )
            metadata = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_metadata_after_drift_failure",
                self.accepted.METADATA_CATALOG_QUERIES,
            )
            harness._assert_metadata_contract(metadata)
            user_catalog = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_user_catalog_after_drift_failure",
                rollback.USER_CATALOG_QUERIES,
            )
            registered_probe = self._probe_state_for(
                f"scenario_{self.spec.key}_registered_probe_after_failure",
                self.spec.registered_schema,
                self.spec.registered_table,
            )
            locks = self._lock_rows(
                f"scenario_{self.spec.key}_locks_after_drift_failure"
            )
            require(
                sentinel
                == {"schema_exists": False, "table_exists": False, "rows": []}
                and ledger == established["ledger"]
                and not [row for row in ledger if row["version"] == 3]
                and adoption == established["adoption"]
                and public == established["public"]
                and self.accepted.canonical_fingerprint(public)
                == established["public_fingerprint"]
                and business == established["business"]
                and metadata == established["metadata"]
                and user_catalog == established["user_catalog"]
                and registered_probe == established["registered_probe"]
                and locks == [],
                "Drift rejection changed state or permitted the version-3 sentinel",
                ProductContractFailure,
            )
            self.evidence["fail_closed"] = {
                "sentinel_schema_exists": False,
                "sentinel_table_exists": False,
                "sentinel_probe_rows": 0,
                "version_3_ledger_rows": 0,
                "migration_advisory_lock_rows": 0,
                "version_2_ledger_fingerprint_before": rollback.canonical_fingerprint(
                    established["ledger"]
                ),
                "version_2_ledger_fingerprint_after": rollback.canonical_fingerprint(
                    ledger
                ),
                "application_state_unchanged": True,
                "catalog_state_unchanged": True,
            }
            self.evidence["checks"].append(
                "drift_failed_closed_before_pending_version_3"
            )

        def _assert_recovery_and_idempotency(
            self,
            established: dict[str, Any],
            sentinel_sha256: str,
        ) -> None:
            harness = self.harness
            retry = harness._run_migration(
                f"scenario_{self.spec.key}_recovery_apply"
            )
            require(
                retry.returncode == 0
                and "discovered=3" in retry.stdout
                and "already_applied=2" in retry.stdout
                and "applied_now=1" in retry.stdout,
                "Recovery did not apply version 3 exactly once",
                ProductContractFailure,
            )
            ledger = harness._ledger_rows(
                f"scenario_{self.spec.key}_ledger_after_recovery"
            )
            sentinel = self._probe_state_for(
                f"scenario_{self.spec.key}_sentinel_after_recovery",
                self.spec.sentinel_schema,
                self.spec.sentinel_table,
            )
            registered = self._probe_state_for(
                f"scenario_{self.spec.key}_registered_after_recovery",
                self.spec.registered_schema,
                self.spec.registered_table,
            )
            adoption = harness._adoption_rows(
                f"scenario_{self.spec.key}_adoption_after_recovery"
            )
            public = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_public_after_recovery",
                self.accepted.PUBLIC_CATALOG_QUERIES,
            )
            business = harness._business_snapshot(
                f"scenario_{self.spec.key}_business_after_recovery"
            )
            metadata = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_metadata_after_recovery",
                self.accepted.METADATA_CATALOG_QUERIES,
            )
            harness._assert_metadata_contract(metadata)
            user_catalog = harness._catalog_snapshot(
                f"scenario_{self.spec.key}_user_catalog_after_recovery",
                rollback.USER_CATALOG_QUERIES,
            )
            require(
                len(ledger) == 3
                and ledger[:2] == established["ledger"]
                and ledger[2]["version"] == 3
                and ledger[2]["name"] == self.spec.sentinel_name
                and ledger[2]["sha256"] == sentinel_sha256
                and ledger[2]["applied_by"] == harness.admin_user
                and bool(ledger[2]["applied_at_utc"])
                and sentinel == self._expected_sentinel_probe()
                and registered == established["registered_probe"]
                and adoption == established["adoption"]
                and public == established["public"]
                and business == established["business"]
                and metadata == established["metadata"]
                and self._lock_rows(
                    f"scenario_{self.spec.key}_locks_after_recovery"
                )
                == [],
                "Recovered version 3 or accepted state differs from its contract",
                ProductContractFailure,
            )
            idempotent = harness._run_migration(
                f"scenario_{self.spec.key}_idempotent_reapply"
            )
            require(
                idempotent.returncode == 0
                and "discovered=3" in idempotent.stdout
                and "already_applied=3" in idempotent.stdout
                and "applied_now=0" in idempotent.stdout,
                "Recovered migration history was not idempotent",
                ProductContractFailure,
            )
            ledger_after = harness._ledger_rows(
                f"scenario_{self.spec.key}_ledger_after_idempotent"
            )
            sentinel_after = self._probe_state_for(
                f"scenario_{self.spec.key}_sentinel_after_idempotent",
                self.spec.sentinel_schema,
                self.spec.sentinel_table,
            )
            registered_after = self._probe_state_for(
                f"scenario_{self.spec.key}_registered_after_idempotent",
                self.spec.registered_schema,
                self.spec.registered_table,
            )
            require(
                ledger_after == ledger
                and sentinel_after == sentinel
                and registered_after == registered
                and harness._adoption_rows(
                    f"scenario_{self.spec.key}_adoption_after_idempotent"
                )
                == adoption
                and harness._catalog_snapshot(
                    f"scenario_{self.spec.key}_public_after_idempotent",
                    self.accepted.PUBLIC_CATALOG_QUERIES,
                )
                == public
                and harness._business_snapshot(
                    f"scenario_{self.spec.key}_business_after_idempotent"
                )
                == business
                and harness._catalog_snapshot(
                    f"scenario_{self.spec.key}_metadata_after_idempotent",
                    self.accepted.METADATA_CATALOG_QUERIES,
                )
                == metadata
                and harness._catalog_snapshot(
                    f"scenario_{self.spec.key}_user_catalog_after_idempotent",
                    rollback.USER_CATALOG_QUERIES,
                )
                == user_catalog
                and self._lock_rows(
                    f"scenario_{self.spec.key}_locks_after_idempotent"
                )
                == [],
                "Idempotent recovery reapplication changed PostgreSQL state",
                ProductContractFailure,
            )
            self.evidence["recovery"] = {
                "apply_exit": retry.returncode,
                "version_3_ledger_rows": 1,
                "sentinel_probe_rows": 1,
                "idempotent_exit": idempotent.returncode,
                "idempotent_applied_now": 0,
                "migration_advisory_lock_rows": 0,
                "ledger_fingerprint": rollback.canonical_fingerprint(ledger),
                "sentinel_fingerprint": rollback.canonical_fingerprint(sentinel),
            }
            self.evidence["checks"].append(
                "recovery_applied_once_and_reapplication_was_idempotent"
            )

        def run(self) -> None:
            harness = self.harness
            harness.preflight()
            require(
                harness.inventory_before is not None,
                "Docker inventory was not captured before live execution",
                IsolationBlocked,
            )
            self.inventory_fingerprint_before = rollback.canonical_fingerprint(
                harness.inventory_before.__dict__
            )
            if self.expected_inventory_fingerprint is not None:
                require(
                    self.inventory_fingerprint_before
                    == self.expected_inventory_fingerprint,
                    "Docker inventory changed between disposable scenarios",
                    IsolationBlocked,
                )
            self.evidence["inventory"] = {
                "before_counts": harness.inventory_before.counts(),
                "before_fingerprint": self.inventory_fingerprint_before,
            }
            self._start_isolated_postgres()
            baseline = self._adopt_and_capture_baseline()

            original_content = registered_migration_content(self.spec)
            original_sha256 = self._create_fixture(
                self.version_2_fixture,
                original_content,
            )
            self._assert_fixture_set(self.version_2_fixture)
            registered_apply = harness._run_migration(
                f"scenario_{self.spec.key}_apply_registered_version_2"
            )
            require(
                registered_apply.returncode == 0
                and "discovered=2" in registered_apply.stdout
                and "already_applied=1" in registered_apply.stdout
                and "applied_now=1" in registered_apply.stdout,
                "Could not establish accepted migration history through version 2",
                ProductContractFailure,
            )
            established = self._capture_established_state(
                baseline,
                original_sha256,
            )
            sentinel_content = sentinel_migration_content(self.spec)
            sentinel_sha256 = self._create_fixture(
                self.version_3_fixture,
                sentinel_content,
            )
            self._assert_fixture_set(self.version_2_fixture)
            require(
                self._probe_state_for(
                    f"scenario_{self.spec.key}_sentinel_before_drift",
                    self.spec.sentinel_schema,
                    self.spec.sentinel_table,
                )
                == {"schema_exists": False, "table_exists": False, "rows": []},
                "Version-3 sentinel exists before drift execution",
                ProductContractFailure,
            )

            drift_content = drifted_registered_content(self.spec)
            drift_sha256 = sha256_bytes(drift_content)
            current_version_2 = self.version_2_fixture
            if self.spec.kind == "checksum_only":
                self._replace_fixture_bytes(self.version_2_fixture, drift_content)
            else:
                self._rename_fixture(
                    self.version_2_fixture,
                    self.drifted_version_2_fixture,
                )
                current_version_2 = self.drifted_version_2_fixture
            self._assert_fixture_set(current_version_2)
            construction = {
                "registered_version": 2,
                "discovered_version": 2,
                "registered_name": self.spec.registered_name,
                "discovered_name": self.spec.drift_name,
                "registered_filename": self.spec.registered_filename,
                "discovered_filename": current_version_2.name,
                "registered_sha256": original_sha256,
                "discovered_sha256": drift_sha256,
                "version_unchanged": True,
                "name_unchanged": self.spec.registered_name
                == self.spec.drift_name,
                "filename_unchanged": self.spec.registered_filename
                == current_version_2.name,
                "bytes_unchanged": original_content == drift_content,
                "sha256_unchanged": original_sha256 == drift_sha256,
            }
            if self.spec.kind == "checksum_only":
                require(
                    construction["name_unchanged"] is True
                    and construction["filename_unchanged"] is True
                    and construction["bytes_unchanged"] is False
                    and construction["sha256_unchanged"] is False,
                    "Live checksum drift changed more than SQL bytes and SHA-256",
                    VerificationHarnessFailure,
                )
            else:
                require(
                    construction["name_unchanged"] is False
                    and construction["filename_unchanged"] is False
                    and construction["bytes_unchanged"] is True
                    and construction["sha256_unchanged"] is True,
                    "Live name drift changed a dimension other than name and filename",
                    VerificationHarnessFailure,
                )
            self.evidence["construction"] = construction

            failed = harness._run_migration(
                f"scenario_{self.spec.key}_expected_drift_failure"
            )
            combined = failed.stdout + failed.stderr
            marker_count = combined.count(CHECKSUM_OR_NAME_DRIFT_ERROR)
            require(
                failed.returncode == EXPECTED_MIGRATION_EXIT
                and marker_count == 1,
                "Accepted migration runner did not reject drift with the exact error once",
                ProductContractFailure,
            )
            self.evidence["error_observation"] = {
                "runner_exit": failed.returncode,
                "classification": "MIGRATION_DRIFT_REJECTED",
                "accepted_error": CHECKSUM_OR_NAME_DRIFT_ERROR,
                "accepted_error_count": marker_count,
                "raw_material_recorded": False,
            }
            self._assert_failure_state(established)

            if self.spec.kind == "checksum_only":
                recovered_sha256 = self._replace_fixture_bytes(
                    self.version_2_fixture,
                    original_content,
                )
            else:
                recovered_sha256 = self._rename_fixture(
                    self.drifted_version_2_fixture,
                    self.version_2_fixture,
                )
            require(
                recovered_sha256 == original_sha256
                and self.version_2_fixture.read_bytes() == original_content
                and (
                    self.spec.kind == "checksum_only"
                    or not self.drifted_version_2_fixture.exists()
                ),
                "Recovery did not restore exact registered version-2 bytes and name",
                IsolationBlocked,
            )
            self._assert_fixture_set(self.version_2_fixture)
            self.evidence["fixture_recovery"] = {
                "registered_sha256": original_sha256,
                "drifted_sha256": drift_sha256,
                "recovered_sha256": recovered_sha256,
                "registered_name_restored": True,
                "registered_filename_restored": True,
                "exact_bytes_restored": True,
            }
            self._assert_recovery_and_idempotency(established, sentinel_sha256)
            harness._verify_owned_resources()
            self.evidence["baseline"] = {
                "application_tables": 9,
                "application_append_only_triggers": 4,
                "registered_ledger_rows": 2,
                "adoption_rows": 1,
                "public_schema_fingerprint": established["public_fingerprint"],
                "application_row_count": sum(
                    len(rows) for rows in established["business"].values()
                ),
                "metadata_guard_fingerprint": rollback.canonical_fingerprint(
                    established["metadata"]
                ),
            }

    return LiveDriftScenarioAcceptance


def classify_live_exception(
    rollback: Any | None,
    accepted: Any | None,
    lock: Any | None,
    exc: Exception,
) -> str:
    if isinstance(exc, DriftHarnessError):
        return exc.result
    if rollback is not None:
        for type_name, result in (
            ("ImplementationBoundaryBlocked", RESULT_BOUNDARY_BLOCKED),
            ("IsolationBlocked", RESULT_ISOLATION_BLOCKED),
            ("EnvironmentBlocked", RESULT_ENVIRONMENT_BLOCKED),
            ("ProductContractFailure", RESULT_PRODUCT_FAILURE),
            ("VerificationHarnessFailure", RESULT_HARNESS_FAILURE),
        ):
            error_type = getattr(rollback, type_name, None)
            if error_type is not None and isinstance(exc, error_type):
                return result
    if accepted is not None:
        for type_name, result in (
            ("IsolationBlocked", RESULT_ISOLATION_BLOCKED),
            ("EnvironmentBlocked", RESULT_ENVIRONMENT_BLOCKED),
            ("ProductContractFailure", RESULT_PRODUCT_FAILURE),
        ):
            error_type = getattr(accepted, type_name, None)
            if error_type is not None and isinstance(exc, error_type):
                return result
    if lock is not None:
        for type_name, result in (
            ("ImplementationBoundaryBlocked", RESULT_BOUNDARY_BLOCKED),
            ("IsolationBlocked", RESULT_ISOLATION_BLOCKED),
            ("EnvironmentBlocked", RESULT_ENVIRONMENT_BLOCKED),
            ("ProductContractFailure", RESULT_PRODUCT_FAILURE),
            ("VerificationHarnessFailure", RESULT_HARNESS_FAILURE),
        ):
            error_type = getattr(lock, type_name, None)
            if error_type is not None and isinstance(exc, error_type):
                return result
    return RESULT_HARNESS_FAILURE


def sanitized_live_failure(
    exc: Exception,
    *,
    harness: Any | None = None,
    rollback: Any | None = None,
    accepted: Any | None = None,
    lock: Any | None = None,
) -> str:
    allowed = isinstance(exc, DriftHarnessError)
    for module in (rollback, accepted, lock):
        if module is None:
            continue
        for type_name in (
            "ImplementationBoundaryBlocked",
            "IsolationBlocked",
            "EnvironmentBlocked",
            "ProductContractFailure",
            "VerificationHarnessFailure",
        ):
            error_type = getattr(module, type_name, None)
            if error_type is not None and isinstance(exc, error_type):
                allowed = True
    if not allowed:
        return f"Unclassified verification failure: {type(exc).__name__}"
    candidate = f"{type(exc).__name__}: {exc}"
    secrets = getattr(harness, "secret_values", set())
    if any(secret and secret in candidate for secret in secrets):
        return f"{type(exc).__name__}: detail withheld by synthetic-secret boundary"
    if SENSITIVE_EVIDENCE_VALUE_PATTERN.search(candidate):
        return f"{type(exc).__name__}: detail withheld by credential boundary"
    return candidate


def execute_live_scenario(
    rollback: Any,
    accepted: Any,
    lock: Any,
    spec: LiveDriftScenario,
    *,
    expected_inventory_fingerprint: str | None,
) -> tuple[str, str, dict[str, Any]]:
    acceptance_class = make_live_scenario_acceptance_class(rollback)
    acceptance = acceptance_class(
        accepted,
        lock,
        spec,
        expected_inventory_fingerprint,
    )
    finalizer = rollback.FinalizerOnce(accepted, acceptance.harness)
    result = RESULT_LIVE_PASS
    failure = ""
    try:
        acceptance.run()
    except Exception as exc:
        result = classify_live_exception(rollback, accepted, lock, exc)
        failure = sanitized_live_failure(
            exc,
            harness=acceptance.harness,
            rollback=rollback,
            accepted=accepted,
            lock=lock,
        )
    finally:
        result, failure = finalizer.finalize(result, failure)

    cleanup_complete = (
        finalizer.calls == 1
        and acceptance.harness.cleanup_complete
        and acceptance.harness.temporary_files_removed
        and (
            not acceptance.harness.state_change_attempted
            or acceptance.harness.inventory_unchanged_verified
        )
    )
    if not cleanup_complete:
        result = RESULT_ISOLATION_BLOCKED
        failure = "Disposable cleanup, inventory, or finalization is incomplete"

    try:
        lifecycle_evidence = acceptance.harness.sanitized_evidence()
        cleanup = lifecycle_evidence.get("cleanup", {})
        before_counts = acceptance.evidence.get("inventory", {}).get(
            "before_counts"
        )
        after_counts = lifecycle_evidence.get("inventory_after")
        acceptance.evidence["inventory"] = {
            "before_counts": before_counts,
            "after_counts": after_counts,
            "before_fingerprint": acceptance.inventory_fingerprint_before or None,
            "after_fingerprint": acceptance.inventory_fingerprint_before
            if acceptance.harness.inventory_unchanged_verified
            else None,
            "exact_restoration_verified": acceptance.harness.inventory_unchanged_verified,
        }
        acceptance.evidence["cleanup"] = {
            "finalizer_calls": finalizer.calls,
            "cleanup_complete": acceptance.harness.cleanup_complete,
            "temporary_files_removed": acceptance.harness.temporary_files_removed,
            "ownership_validated_before_removal": cleanup.get(
                "ownership_validated_before_removal"
            ),
            "project_resources_remaining": cleanup.get(
                "project_resources_remaining"
            ),
            "preexisting_inventory_unchanged": cleanup.get(
                "preexisting_inventory_unchanged"
            ),
        }
        acceptance.evidence["command_audit"] = {
            "commands_recorded": len(lifecycle_evidence.get("command_results", [])),
            "raw_material_recorded": False,
            "sensitive_values_recorded": False,
        }
        acceptance.evidence["result"] = result
        if failure:
            acceptance.evidence["failure"] = failure
        assert_sanitized_evidence(acceptance.evidence)
    except Exception:
        result = RESULT_HARNESS_FAILURE
        failure = "Structured scenario evidence failed its sanitization boundary"
        acceptance.evidence = {
            "scenario": spec.key,
            "scenario_type": spec.kind,
            "result": result,
            "failure": failure,
            "cleanup": {
                "finalizer_calls": finalizer.calls,
                "cleanup_complete": acceptance.harness.cleanup_complete,
                "temporary_files_removed": acceptance.harness.temporary_files_removed,
            },
        }
    return result, failure, acceptance.evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run offline drift checks or the explicitly authorized "
            "M0-R01.4.2c.2 live A/B acceptance"
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run-focused-regression-checks",
        action="store_true",
        help="run pure offline contract checks",
    )
    mode.add_argument(
        "--confirm-disposable-synthetic-drift-run",
        action="store_true",
        help="run only disposable checksum-only A and name-only B scenarios",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_focused_regression_checks:
        try:
            checks = run_focused_regression_checks()
        except DriftHarnessError as exc:
            print(f"Focused verification failure: {type(exc).__name__}: {exc}")
            return 2
        print(json.dumps(checks, sort_keys=True, indent=2))
        print(FOCUSED_PASS)
        return 0

    if not args.confirm_disposable_synthetic_drift_run:
        print("Explicit offline focused mode is required; no action taken.")
        print(RESULT_ISOLATION_BLOCKED)
        return 2

    result = RESULT_LIVE_PASS
    failure = ""
    rollback: Any | None = None
    accepted: Any | None = None
    lock: Any | None = None
    protected_before: dict[str, str] | None = None
    static_evidence: dict[str, Any] = {
        "live_scope": {
            "scenario_a_constructed": False,
            "scenario_b_constructed": False,
            "scenario_c_constructed": False,
            "scenario_d_constructed": False,
        }
    }
    scenario_evidence: list[dict[str, Any]] = []
    expected_inventory_fingerprint: str | None = None
    try:
        protected_before = verify_repository_protected_hashes()
        validate_live_scenario_contracts()
        rollback, accepted, lock = load_authenticated_live_dependencies()
        protected_pre_live = verify_repository_protected_hashes(
            preflight=protected_before
        )
        static_evidence.update(
            {
                "repository": lock.repository_evidence(),
                "authenticated_dependencies": {
                    "rollback_harness_sha256": ROLLBACK_HARNESS_SHA256,
                    "lifecycle_harness_sha256": PROTECTED_HASHES[
                        "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
                    ],
                    "lock_harness_sha256": PROTECTED_HASHES[
                        "infra/postgres/tests/live_migration_lock_acceptance.py"
                    ],
                },
                "protected_hashes_before": protected_hash_evidence(
                    protected_before
                ),
                "protected_hashes_pre_live": protected_hash_evidence(
                    protected_pre_live
                ),
                "migration_advisory_lock_key": EXPECTED_MIGRATION_LOCK_KEY,
            }
        )
        for spec in LIVE_SCENARIOS:
            verify_repository_protected_hashes(preflight=protected_before)
            static_evidence["live_scope"][
                f"scenario_{spec.key.lower()}_constructed"
            ] = True
            scenario_result, scenario_failure, evidence = execute_live_scenario(
                rollback,
                accepted,
                lock,
                spec,
                expected_inventory_fingerprint=expected_inventory_fingerprint,
            )
            after_scenario = verify_repository_protected_hashes(
                preflight=protected_before
            )
            static_evidence[f"protected_hashes_after_{spec.key.lower()}"] = (
                protected_hash_evidence(after_scenario)
            )
            evidence["protected_boundary"] = {
                "accepted_values_verified": True,
                "preflight_values_verified": True,
                "paths_verified": len(after_scenario),
            }
            scenario_evidence.append(evidence)
            if scenario_result != RESULT_LIVE_PASS:
                result = scenario_result
                failure = scenario_failure
                break
            inventory = evidence.get("inventory", {})
            require(
                inventory.get("exact_restoration_verified") is True
                and inventory.get("before_fingerprint")
                == inventory.get("after_fingerprint"),
                f"Scenario {spec.key} did not restore its exact Docker inventory",
                IsolationBlocked,
            )
            expected_inventory_fingerprint = str(
                inventory["before_fingerprint"]
            )
        if result == RESULT_LIVE_PASS:
            require(
                len(scenario_evidence) == 2
                and static_evidence["live_scope"]
                == {
                    "scenario_a_constructed": True,
                    "scenario_b_constructed": True,
                    "scenario_c_constructed": False,
                    "scenario_d_constructed": False,
                },
                "Live execution constructed a scenario outside A/B scope",
                ImplementationBoundaryBlocked,
            )
            first_inventory = scenario_evidence[0]["inventory"]
            final_inventory = scenario_evidence[1]["inventory"]
            require(
                first_inventory["before_fingerprint"]
                == final_inventory["after_fingerprint"],
                "Final Docker inventory differs from the initial inventory",
                IsolationBlocked,
            )
            static_evidence["docker_inventory"] = {
                "initial_counts": first_inventory["before_counts"],
                "final_counts": final_inventory["after_counts"],
                "initial_fingerprint": first_inventory["before_fingerprint"],
                "final_fingerprint": final_inventory["after_fingerprint"],
                "exactly_unchanged": True,
            }
    except Exception as exc:
        result = classify_live_exception(rollback, accepted, lock, exc)
        failure = sanitized_live_failure(
            exc,
            rollback=rollback,
            accepted=accepted,
            lock=lock,
        )
    finally:
        if protected_before is not None:
            try:
                static_evidence["protected_hashes_final"] = protected_hash_evidence(
                    verify_repository_protected_hashes(preflight=protected_before)
                )
            except Exception as hash_error:
                result = RESULT_BOUNDARY_BLOCKED
                failure = (
                    "Final protected-boundary verification failed: "
                    f"{type(hash_error).__name__}"
                )

    output = {
        "ticket": "M0-R01.4.2c.2",
        "result": result,
        "failure": failure or None,
        "static_evidence": static_evidence,
        "scenarios": scenario_evidence,
        "scenarios_completed": len(scenario_evidence),
        "scenarios_expected": 2,
    }
    try:
        assert_sanitized_evidence(output)
    except VerificationHarnessFailure:
        result = RESULT_HARNESS_FAILURE
        output = {
            "ticket": "M0-R01.4.2c.2",
            "result": result,
            "failure": "Final evidence failed its sanitization boundary",
        }
    print(json.dumps(output, sort_keys=True, indent=2))
    print(result)
    return 0 if result == RESULT_LIVE_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
