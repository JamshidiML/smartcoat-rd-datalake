#!/usr/bin/env python3
"""Explicit M0-R01.4.2c.3 live history-drift acceptance.

The default and focused modes are deliberately offline.  Live execution imports
the accepted c.2 harness only after authenticating every protected input and only
when the exact disposable-run authorization flag is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, NoReturn


RESULT_LIVE_PASS = "PASS_M0_R01_4_2C_3"
RESULT_C2_PASS = "PASS_M0_R01_4_2C_2"
RESULT_ISOLATION_BLOCKED = "BLOCKED_ISOLATION"
RESULT_ENVIRONMENT_BLOCKED = "BLOCKED_ENVIRONMENT"
RESULT_BOUNDARY_BLOCKED = "BLOCKED_IMPLEMENTATION_BOUNDARY"
RESULT_PRODUCT_FAILURE = "FAIL_PRODUCT_CONTRACT"
RESULT_HARNESS_FAILURE = "FAIL_VERIFICATION_HARNESS"
FOCUSED_PASS = "FOCUSED_REGRESSION_CHECKS_PASS"

EXPECTED_MIGRATION_EXIT = 2
EXPECTED_MIGRATION_LOCK_KEY = 5999724105712152625
C2_RELATIVE_PATH = "infra/postgres/tests/live_migration_drift_acceptance.py"
C2_SHA256 = "81b6910784c2294d68ef41b5f8afc9de369ea16bd7295ba5dec508d068c0edb7"

MISSING_HISTORY_ERROR = (
    "Migration error: Applied migration 0002 is missing from the repository"
)
NON_PREFIX_HISTORY_ERROR = (
    "Migration error: Applied migration history is not an ordered prefix of "
    "discovered migrations; refusing an unsafe out-of-order migration"
)

PROTECTED_HASHES = {
    C2_RELATIVE_PATH: C2_SHA256,
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

SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:postgres(?:ql)?://|MIGRATION_DATABASE_URL\s*=|DATABASE_URL\s*=)", re.I
)
PROHIBITED_KEY_FRAGMENTS = (
    "password",
    "secret",
    "credential",
    "connection_url",
    "database_url",
    "raw_stdout",
    "raw_stderr",
    "raw_sql",
    "raw_log",
)


class HistoryAcceptanceError(RuntimeError):
    result = RESULT_HARNESS_FAILURE


class VerificationHarnessFailure(HistoryAcceptanceError):
    pass


class ProductContractFailure(HistoryAcceptanceError):
    result = RESULT_PRODUCT_FAILURE


class IsolationBlocked(HistoryAcceptanceError):
    result = RESULT_ISOLATION_BLOCKED


class EnvironmentBlocked(HistoryAcceptanceError):
    result = RESULT_ENVIRONMENT_BLOCKED


class ImplementationBoundaryBlocked(HistoryAcceptanceError):
    result = RESULT_BOUNDARY_BLOCKED


def require(
    condition: bool,
    message: str,
    error_type: type[HistoryAcceptanceError] = VerificationHarnessFailure,
) -> None:
    if not condition:
        raise error_type(message)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ProbeFixture:
    version: int
    name: str
    schema: str
    table: str
    probe_id: str
    probe_value: str
    observed_at_utc: str

    @property
    def filename(self) -> str:
        return f"{self.version:04d}__{self.name}.sql"


@dataclass(frozen=True)
class HistoryScenario:
    key: str
    kind: str
    expected_error: str
    established_versions: tuple[int, ...]
    failing_discovered_versions: tuple[int, ...]
    recovery_versions: tuple[int, ...]
    fixtures: tuple[ProbeFixture, ...]


C_VERSION_2 = ProbeFixture(
    2,
    "history_missing_registered",
    "m0r0142c3c_registered",
    "registered_probe",
    "m0-r01-4-2c-3-c-registered",
    "synthetic-missing-history-registered-probe",
    "2026-03-03T00:00:00Z",
)
C_VERSION_3 = ProbeFixture(
    3,
    "history_missing_sentinel",
    "m0r0142c3c_sentinel",
    "sentinel_probe",
    "m0-r01-4-2c-3-c-sentinel",
    "synthetic-missing-history-sentinel-probe",
    "2026-03-04T00:00:00Z",
)
D_VERSION_2 = ProbeFixture(
    2,
    "history_nonprefix_lower",
    "m0r0142c3d_lower",
    "lower_probe",
    "m0-r01-4-2c-3-d-lower",
    "synthetic-nonprefix-lower-probe",
    "2026-03-05T00:00:00Z",
)
D_VERSION_3 = ProbeFixture(
    3,
    "history_nonprefix_registered",
    "m0r0142c3d_registered",
    "registered_probe",
    "m0-r01-4-2c-3-d-registered",
    "synthetic-nonprefix-registered-probe",
    "2026-03-06T00:00:00Z",
)
D_VERSION_4 = ProbeFixture(
    4,
    "history_nonprefix_sentinel",
    "m0r0142c3d_sentinel",
    "sentinel_probe",
    "m0-r01-4-2c-3-d-sentinel",
    "synthetic-nonprefix-sentinel-probe",
    "2026-03-07T00:00:00Z",
)

HISTORY_SCENARIOS = (
    HistoryScenario(
        "C",
        "missing_registered_migration",
        MISSING_HISTORY_ERROR,
        (1, 2),
        (1, 3),
        (1, 2, 3),
        (C_VERSION_2, C_VERSION_3),
    ),
    HistoryScenario(
        "D",
        "non_prefix_history",
        NON_PREFIX_HISTORY_ERROR,
        (1, 3),
        (1, 2, 3, 4),
        (1, 3, 4),
        (D_VERSION_2, D_VERSION_3, D_VERSION_4),
    ),
)


def fixture_sql(fixture: ProbeFixture) -> bytes:
    identifiers = (fixture.name, fixture.schema, fixture.table)
    require(
        all(re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in identifiers),
        "Synthetic fixture contains an unsafe SQL identifier",
    )

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return f"""CREATE SCHEMA {fixture.schema};

CREATE TABLE {fixture.schema}.{fixture.table} (
    probe_id text PRIMARY KEY,
    probe_value text NOT NULL,
    observed_at_utc timestamptz NOT NULL
);

INSERT INTO {fixture.schema}.{fixture.table} (
    probe_id,
    probe_value,
    observed_at_utc
) VALUES (
    {literal(fixture.probe_id)},
    {literal(fixture.probe_value)},
    TIMESTAMPTZ {literal(fixture.observed_at_utc)}
);
""".encode("utf-8")


def expected_probe(fixture: ProbeFixture) -> dict[str, Any]:
    return {
        "schema_exists": True,
        "table_exists": True,
        "rows": [
            {
                "probe_id": fixture.probe_id,
                "probe_value": fixture.probe_value,
                "observed_at_utc": fixture.observed_at_utc.replace("T", " ").replace(
                    "Z", "+00"
                ),
            }
        ],
    }


ABSENT_PROBE = {"schema_exists": False, "table_exists": False, "rows": []}


def validate_history_scenarios() -> None:
    require(
        tuple(item.key for item in HISTORY_SCENARIOS) == ("C", "D")
        and tuple(item.kind for item in HISTORY_SCENARIOS)
        == ("missing_registered_migration", "non_prefix_history"),
        "History ticket scope must contain exactly C and D",
        ImplementationBoundaryBlocked,
    )
    c_spec, d_spec = HISTORY_SCENARIOS
    require(
        c_spec.established_versions == (1, 2)
        and c_spec.failing_discovered_versions == (1, 3)
        and c_spec.recovery_versions == (1, 2, 3)
        and c_spec.expected_error == MISSING_HISTORY_ERROR,
        "Scenario C construction does not isolate missing registered history",
    )
    require(
        d_spec.established_versions == (1, 3)
        and d_spec.failing_discovered_versions == (1, 2, 3, 4)
        and d_spec.recovery_versions == (1, 3, 4)
        and d_spec.expected_error == NON_PREFIX_HISTORY_ERROR,
        "Scenario D construction does not isolate non-prefix history",
    )
    all_fixtures = tuple(fixture for spec in HISTORY_SCENARIOS for fixture in spec.fixtures)
    require(
        len({fixture.filename for fixture in all_fixtures}) == len(all_fixtures)
        and len({fixture.schema for fixture in all_fixtures}) == len(all_fixtures)
        and len({fixture.probe_id for fixture in all_fixtures}) == len(all_fixtures),
        "Scenario C/D fixtures are not uniquely attributable",
    )
    require(
        MISSING_HISTORY_ERROR != NON_PREFIX_HISTORY_ERROR,
        "History error markers must be distinct",
    )
    for fixture in all_fixtures:
        sql = fixture_sql(fixture)
        require(
            sql.index(b"CREATE SCHEMA")
            < sql.index(b"CREATE TABLE")
            < sql.index(b"INSERT INTO"),
            "Synthetic migration statement order changed",
        )


def validate_fail_closed_snapshot(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    expected_absent: tuple[str, ...],
) -> None:
    for key in (
        "ledger",
        "adoption",
        "public",
        "business",
        "metadata",
        "user_catalog",
        "established_probes",
    ):
        require(
            before.get(key) == after.get(key),
            f"Fail-closed history rejection changed {key}",
            ProductContractFailure,
        )
    require(
        after.get("locks") == [],
        "History rejection retained the migration advisory lock",
        ProductContractFailure,
    )
    probes = after.get("pending_probes", {})
    require(
        set(probes) == set(expected_absent)
        and all(probes[label] == ABSENT_PROBE for label in expected_absent),
        "A blocked history migration left probe residue",
        ProductContractFailure,
    )


def verify_protected_values(
    observed: Mapping[str, str],
    *,
    preflight: Mapping[str, str] | None = None,
) -> None:
    require(
        set(observed) == set(PROTECTED_HASHES),
        "Protected boundary path set is not exactly 16 paths",
        ImplementationBoundaryBlocked,
    )
    for path, accepted_hash in PROTECTED_HASHES.items():
        require(
            observed.get(path) == accepted_hash,
            f"Protected path differs from accepted hash: {path}",
            ImplementationBoundaryBlocked,
        )
        if preflight is not None:
            require(
                preflight.get(path) == observed[path],
                f"Protected path differs from preflight: {path}",
                ImplementationBoundaryBlocked,
            )


def assert_sanitized_evidence(value: Any, *, path: str = "evidence") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            require(
                not any(fragment in normalized for fragment in PROHIBITED_KEY_FRAGMENTS),
                f"Structured evidence contains prohibited key at {path}",
            )
            assert_sanitized_evidence(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_sanitized_evidence(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        require(
            SENSITIVE_VALUE_PATTERN.search(value) is None,
            f"Structured evidence contains a credential boundary at {path}",
        )


class FocusedFinalizerOnce:
    def __init__(self, action: Callable[[str, str], tuple[str, str]]) -> None:
        self.action = action
        self.calls = 0

    def finalize(self, result: str, failure: str) -> tuple[str, str]:
        require(self.calls == 0, "Finalizer invoked more than once", IsolationBlocked)
        self.calls += 1
        return self.action(result, failure)


def execute_focused_with_finalization(
    action: Callable[[], None],
    finalizer: FocusedFinalizerOnce,
) -> tuple[str, str, int]:
    result = RESULT_LIVE_PASS
    failure = ""
    try:
        action()
    except Exception as exc:
        result = RESULT_HARNESS_FAILURE
        failure = type(exc).__name__
    finally:
        result, failure = finalizer.finalize(result, failure)
    return result, failure, finalizer.calls


def _raise_ordinary_exception() -> NoReturn:
    raise RuntimeError("synthetic focused exception")


def _expect_failure(action: Callable[[], None], message: str) -> None:
    try:
        action()
    except HistoryAcceptanceError:
        return
    raise VerificationHarnessFailure(message)


def run_focused_regression_checks() -> dict[str, bool]:
    validate_history_scenarios()
    checks: dict[str, bool] = {}

    c_spec, d_spec = HISTORY_SCENARIOS
    checks["scenario_c_construction_is_exact"] = (
        c_spec.established_versions == (1, 2)
        and c_spec.failing_discovered_versions == (1, 3)
        and c_spec.recovery_versions == (1, 2, 3)
    )
    checks["scenario_d_construction_is_exact"] = (
        d_spec.established_versions == (1, 3)
        and d_spec.failing_discovered_versions == (1, 2, 3, 4)
        and d_spec.recovery_versions == (1, 3, 4)
    )
    checks["accepted_errors_are_exact_and_distinct"] = (
        c_spec.expected_error == MISSING_HISTORY_ERROR
        and d_spec.expected_error == NON_PREFIX_HISTORY_ERROR
        and c_spec.expected_error != d_spec.expected_error
    )

    stable = {
        "ledger": [{"version": 1}, {"version": 2}],
        "adoption": [{"decision": "ADOPTED"}],
        "public": {"tables": ["accepted"]},
        "business": {"users": [{"id": "synthetic"}]},
        "metadata": {"guards": ["accepted"]},
        "user_catalog": {"schemas": ["public", "smartcoat_migrations"]},
        "established_probes": {"registered": expected_probe(C_VERSION_2)},
    }
    after = dict(stable)
    after.update({"locks": [], "pending_probes": {"sentinel": ABSENT_PROBE}})
    validate_fail_closed_snapshot(stable, after, expected_absent=("sentinel",))
    checks["clean_fail_closed_snapshot_passes"] = True
    bad_probe = dict(after)
    bad_probe["pending_probes"] = {"sentinel": expected_probe(C_VERSION_3)}
    _expect_failure(
        lambda: validate_fail_closed_snapshot(
            stable, bad_probe, expected_absent=("sentinel",)
        ),
        "Surviving probe evidence incorrectly passed",
    )
    checks["surviving_probe_cannot_pass"] = True
    bad_ledger = dict(after)
    bad_ledger["ledger"] = [{"version": 1}, {"version": 2}, {"version": 3}]
    _expect_failure(
        lambda: validate_fail_closed_snapshot(
            stable, bad_ledger, expected_absent=("sentinel",)
        ),
        "Surviving ledger evidence incorrectly passed",
    )
    checks["surviving_ledger_cannot_pass"] = True
    bad_lock = dict(after)
    bad_lock["locks"] = [{"granted": True}]
    _expect_failure(
        lambda: validate_fail_closed_snapshot(
            stable, bad_lock, expected_absent=("sentinel",)
        ),
        "Surviving advisory lock incorrectly passed",
    )
    checks["surviving_lock_cannot_pass"] = True

    verify_protected_values(PROTECTED_HASHES, preflight=PROTECTED_HASHES)
    changed = dict(PROTECTED_HASHES)
    changed[C2_RELATIVE_PATH] = "0" * 64
    _expect_failure(
        lambda: verify_protected_values(changed, preflight=PROTECTED_HASHES),
        "Changed c.2 harness hash incorrectly passed",
    )
    missing = dict(PROTECTED_HASHES)
    missing.pop("compose.yaml")
    _expect_failure(
        lambda: verify_protected_values(missing),
        "Missing protected path incorrectly passed",
    )
    checks["protected_boundary_fails_closed"] = True

    finalizer = FocusedFinalizerOnce(lambda result, failure: (result, failure))
    result, failure, calls = execute_focused_with_finalization(
        _raise_ordinary_exception, finalizer
    )
    require(
        result == RESULT_HARNESS_FAILURE
        and failure == "RuntimeError"
        and calls == 1,
        "Ordinary exception did not finalize exactly once",
    )
    checks["ordinary_exception_finalizes_once"] = True

    cleanup_override = FocusedFinalizerOnce(
        lambda _result, _failure: (
            RESULT_ISOLATION_BLOCKED,
            "synthetic cleanup failure",
        )
    )
    result, failure, calls = execute_focused_with_finalization(
        lambda: None, cleanup_override
    )
    require(
        result == RESULT_ISOLATION_BLOCKED
        and failure == "synthetic cleanup failure"
        and calls == 1,
        "Cleanup failure did not override an otherwise successful result",
    )
    checks["cleanup_failure_overrides_pass"] = True

    sequence = ("A", "B", "C", "D")
    require(
        sequence == ("A", "B", "C", "D") and len(sequence) == 4,
        "Live scenario scope is not exactly A/B/C/D",
        ImplementationBoundaryBlocked,
    )
    checks["live_scope_is_exactly_four_scenarios"] = True

    sanitized = {
        "scenario": "C",
        "accepted_error": MISSING_HISTORY_ERROR,
        "marker_count": 1,
    }
    assert_sanitized_evidence(sanitized)
    _expect_failure(
        lambda: assert_sanitized_evidence({"database_url": "withheld"}),
        "Prohibited evidence key incorrectly passed",
    )
    _expect_failure(
        lambda: assert_sanitized_evidence({"detail": "postgresql://example"}),
        "Connection URL incorrectly passed",
    )
    checks["structured_evidence_is_sanitized"] = True

    require(all(checks.values()), "A focused regression predicate failed")
    return checks


def _repository_root() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parents[3]


def _sha256_file(relative_path: str) -> str:
    path = (_repository_root() / relative_path).resolve()
    root = _repository_root()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ImplementationBoundaryBlocked(
            "Protected path escapes the repository root"
        ) from exc
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ImplementationBoundaryBlocked(
            f"Protected path is missing or unreadable: {relative_path}"
        ) from exc
    return digest


def verify_repository_protected_hashes(
    *, preflight: Mapping[str, str] | None = None
) -> dict[str, str]:
    require(
        len(PROTECTED_HASHES) == 16,
        "Protected implementation boundary is not exactly 16 paths",
        ImplementationBoundaryBlocked,
    )
    observed = {path: _sha256_file(path) for path in PROTECTED_HASHES}
    verify_protected_values(observed, preflight=preflight)
    return observed


def protected_hash_evidence(values: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {"path": path, "sha256": values[path]}
        for path in sorted(values)
    ]


def load_authenticated_c2() -> tuple[Any, Any, Any, Any]:
    import importlib.util
    import sys

    before = _sha256_file(C2_RELATIVE_PATH)
    require(
        before == C2_SHA256,
        "Accepted c.2 harness hash changed before import",
        ImplementationBoundaryBlocked,
    )
    module_name = "m0_r01_4_2c_2_accepted_harness_for_history"
    module_spec = importlib.util.spec_from_file_location(
        module_name, _repository_root() / C2_RELATIVE_PATH
    )
    require(
        module_spec is not None and module_spec.loader is not None,
        "Accepted c.2 harness cannot be imported",
        ImplementationBoundaryBlocked,
    )
    c2 = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = c2
    try:
        module_spec.loader.exec_module(c2)
    except Exception as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted c.2 harness import failed: {type(exc).__name__}"
        ) from exc
    after = _sha256_file(C2_RELATIVE_PATH)
    require(
        after == before == C2_SHA256,
        "Accepted c.2 harness changed during import",
        ImplementationBoundaryBlocked,
    )
    required_symbols = {
        "LIVE_SCENARIOS",
        "LiveDriftScenario",
        "execute_live_scenario",
        "make_live_scenario_acceptance_class",
        "load_authenticated_live_dependencies",
        "verify_repository_protected_hashes",
        "PROTECTED_HASHES",
        "classify_live_exception",
        "sanitized_live_failure",
        "assert_sanitized_evidence",
    }
    require(
        all(hasattr(c2, symbol) for symbol in required_symbols),
        "Accepted c.2 harness lacks a required reuse symbol",
        ImplementationBoundaryBlocked,
    )
    require(
        {C2_RELATIVE_PATH: C2_SHA256, **dict(c2.PROTECTED_HASHES)}
        == PROTECTED_HASHES,
        "Accepted c.2 protected boundary differs from the c.3 contract",
        ImplementationBoundaryBlocked,
    )
    try:
        rollback, accepted, lock = c2.load_authenticated_live_dependencies()
    except Exception as exc:
        raise ImplementationBoundaryBlocked(
            f"Accepted live dependency authentication failed: {type(exc).__name__}"
        ) from exc
    require(
        _sha256_file(C2_RELATIVE_PATH) == C2_SHA256,
        "Accepted c.2 harness changed after dependency authentication",
        ImplementationBoundaryBlocked,
    )
    return c2, rollback, accepted, lock


def _proxy_spec(c2: Any, spec: HistoryScenario) -> Any:
    if spec.key == "C":
        registered, sentinel = C_VERSION_2, C_VERSION_3
    else:
        registered, sentinel = D_VERSION_3, D_VERSION_4
    return c2.LiveDriftScenario(
        key=spec.key,
        kind=spec.kind,
        registered_name=registered.name,
        drift_name=registered.name,
        registered_schema=registered.schema,
        registered_table=registered.table,
        registered_probe_id=registered.probe_id,
        registered_probe_value=registered.probe_value,
        sentinel_name=sentinel.name,
        sentinel_schema=sentinel.schema,
        sentinel_table=sentinel.table,
        sentinel_probe_id=sentinel.probe_id,
        sentinel_probe_value=sentinel.probe_value,
    )


def make_history_acceptance_class(c2: Any, rollback: Any) -> type[Any]:
    accepted_base = c2.make_live_scenario_acceptance_class(rollback)

    class HistoryScenarioAcceptance(accepted_base):
        def __init__(
            self,
            accepted: Any,
            lock: Any,
            history_spec: HistoryScenario,
            expected_inventory_fingerprint: str | None = None,
        ) -> None:
            self.history_spec = history_spec
            super().__init__(
                accepted,
                lock,
                _proxy_spec(c2, history_spec),
                expected_inventory_fingerprint,
            )
            directory = self.harness.migration_fixture_directory
            self.paths = {
                fixture.version: directory / fixture.filename
                for fixture in history_spec.fixtures
            }
            self.version_2_fixture = self.paths.get(2)
            self.version_3_fixture = self.paths.get(3)
            self.version_4_fixture = self.paths.get(4)
            self.evidence["scenario_type"] = history_spec.kind

        def _assert_exact_fixtures(self, versions: tuple[int, ...]) -> None:
            directory = self.harness.migration_fixture_directory.resolve()
            require(
                directory.parent == self.harness.temporary_directory.resolve()
                and directory.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(directory.stat().st_mode) == 0o700,
                "Temporary migration directory ownership or mode is unsafe",
                c2.IsolationBlocked,
            )
            expected = {self.harness.baseline_fixture.resolve()}
            expected.update(self.paths[version].resolve() for version in versions)
            observed = {path.resolve() for path in directory.iterdir()}
            require(
                observed == expected,
                "Temporary migration directory contains an unexpected fixture",
                c2.IsolationBlocked,
            )
            for path in expected:
                metadata = path.stat()
                require(
                    metadata.st_uid == rollback.os.getuid()
                    and rollback.stat.S_IMODE(metadata.st_mode) == 0o400,
                    "Temporary migration fixture ownership or mode is unsafe",
                    c2.IsolationBlocked,
                )

        def _remove_fixture(self, version: int) -> bytes:
            path = self.paths[version]
            directory = self.harness.migration_fixture_directory.resolve()
            require(
                path.exists()
                and path.resolve().parent == directory
                and path.stat().st_uid == rollback.os.getuid()
                and rollback.stat.S_IMODE(path.stat().st_mode) == 0o400,
                "Fixture removal target is not an owned read-only fixture",
                c2.IsolationBlocked,
            )
            content = path.read_bytes()
            path.unlink()
            require(not path.exists(), "Owned fixture removal did not complete", c2.IsolationBlocked)
            return content

        def _probe(self, label: str, fixture: ProbeFixture) -> dict[str, Any]:
            return self._probe_state_for(label, fixture.schema, fixture.table)

        def _ledger_versions(self, rows: list[dict[str, Any]]) -> tuple[int, ...]:
            return tuple(int(row["version"]) for row in rows)

        def _capture_state(
            self,
            label: str,
            fixtures: tuple[ProbeFixture, ...],
        ) -> dict[str, Any]:
            harness = self.harness
            public = harness._catalog_snapshot(
                f"{label}_public", self.accepted.PUBLIC_CATALOG_QUERIES
            )
            metadata = harness._catalog_snapshot(
                f"{label}_metadata", self.accepted.METADATA_CATALOG_QUERIES
            )
            harness._assert_metadata_contract(metadata)
            return {
                "ledger": harness._ledger_rows(f"{label}_ledger"),
                "adoption": harness._adoption_rows(f"{label}_adoption"),
                "public": public,
                "business": harness._business_snapshot(f"{label}_business"),
                "metadata": metadata,
                "user_catalog": harness._catalog_snapshot(
                    f"{label}_user_catalog", rollback.USER_CATALOG_QUERIES
                ),
                "established_probes": {
                    fixture.filename: self._probe(
                        f"{label}_{fixture.version}_probe", fixture
                    )
                    for fixture in fixtures
                },
                "locks": self._lock_rows(f"{label}_locks"),
                "public_fingerprint": self.accepted.canonical_fingerprint(public),
            }

        def _assert_accepted_state(
            self,
            baseline: Any,
            state: Mapping[str, Any],
        ) -> None:
            require(
                state["adoption"] == baseline.adoption
                and state["public"] == baseline.public_catalog
                and state["public_fingerprint"] == baseline.public_fingerprint
                and state["business"] == baseline.business_rows
                and state["metadata"] == baseline.metadata_catalog
                and state["locks"] == [],
                "History scenario changed accepted application or metadata state",
                c2.ProductContractFailure,
            )

        def _run_expected_history_failure(
            self,
            label: str,
            expected_error: str,
        ) -> None:
            failed = self.harness._run_migration(label)
            combined = failed.stdout + failed.stderr
            marker_count = combined.count(expected_error)
            require(
                failed.returncode == EXPECTED_MIGRATION_EXIT and marker_count == 1,
                "Accepted runner did not reject history drift with the exact error once",
                c2.ProductContractFailure,
            )
            self.evidence["error_observation"] = {
                "runner_exit": failed.returncode,
                "classification": "MIGRATION_HISTORY_DRIFT_REJECTED",
                "accepted_error": expected_error,
                "accepted_error_count": marker_count,
                "raw_material_recorded": False,
            }

        def _prepare(self) -> Any:
            self.harness.preflight()
            require(
                self.harness.inventory_before is not None,
                "Docker inventory was not captured before live execution",
                c2.IsolationBlocked,
            )
            self.inventory_fingerprint_before = rollback.canonical_fingerprint(
                self.harness.inventory_before.__dict__
            )
            if self.expected_inventory_fingerprint is not None:
                require(
                    self.inventory_fingerprint_before
                    == self.expected_inventory_fingerprint,
                    "Docker inventory changed between disposable scenarios",
                    c2.IsolationBlocked,
                )
            self.evidence["inventory"] = {
                "before_counts": self.harness.inventory_before.counts(),
                "before_fingerprint": self.inventory_fingerprint_before,
            }
            self._start_isolated_postgres()
            return self._adopt_and_capture_baseline()

        def _run_c(self) -> None:
            baseline = self._prepare()
            version_2_content = fixture_sql(C_VERSION_2)
            version_2_sha = self._create_fixture(self.paths[2], version_2_content)
            self._assert_exact_fixtures((2,))
            apply_v2 = self.harness._run_migration("scenario_C_apply_version_2")
            require(
                apply_v2.returncode == 0
                and "discovered=2" in apply_v2.stdout
                and "already_applied=1" in apply_v2.stdout
                and "applied_now=1" in apply_v2.stdout,
                "Scenario C could not establish registered history 1,2",
                c2.ProductContractFailure,
            )
            established = self._capture_state("scenario_C_established", (C_VERSION_2,))
            self._assert_accepted_state(baseline, established)
            require(
                self._ledger_versions(established["ledger"]) == (1, 2)
                and established["ledger"][0] == baseline.ledger[0]
                and established["ledger"][1]["name"] == C_VERSION_2.name
                and established["ledger"][1]["sha256"] == version_2_sha
                and established["established_probes"][C_VERSION_2.filename]
                == expected_probe(C_VERSION_2),
                "Scenario C registered state is not exactly versions 1,2",
                c2.ProductContractFailure,
            )

            version_3_sha = self._create_fixture(
                self.paths[3], fixture_sql(C_VERSION_3)
            )
            removed_bytes = self._remove_fixture(2)
            require(
                removed_bytes == version_2_content
                and sha256_bytes(removed_bytes) == version_2_sha,
                "Scenario C did not retain exact registered version-2 bytes",
                c2.IsolationBlocked,
            )
            self._assert_exact_fixtures((3,))
            pre_failure = self._capture_state("scenario_C_pre_failure", (C_VERSION_2,))
            require(
                self._ledger_versions(pre_failure["ledger"]) == (1, 2)
                and self._probe("scenario_C_sentinel_before_failure", C_VERSION_3)
                == ABSENT_PROBE,
                "Scenario C missing-history construction is not exact",
                c2.VerificationHarnessFailure,
            )
            self.evidence["construction"] = {
                "applied_versions": [1, 2],
                "discovered_versions": [1, 3],
                "missing_registered_version": 2,
                "sentinel_version": 3,
                "sentinel_absent_before_failure": True,
                "removed_fixture_sha256": version_2_sha,
                "sentinel_fixture_sha256": version_3_sha,
            }
            self._run_expected_history_failure(
                "scenario_C_expected_missing_history_failure", MISSING_HISTORY_ERROR
            )
            after_failure = self._capture_state(
                "scenario_C_after_failure", (C_VERSION_2,)
            )
            after_failure["pending_probes"] = {
                "version_3": self._probe(
                    "scenario_C_sentinel_after_failure", C_VERSION_3
                )
            }
            validate_fail_closed_snapshot(
                pre_failure, after_failure, expected_absent=("version_3",)
            )
            self.evidence["fail_closed"] = {
                "version_3_probe_rows": 0,
                "version_3_ledger_rows": 0,
                "migration_advisory_lock_rows": 0,
                "ledger_fingerprint_before": rollback.canonical_fingerprint(
                    pre_failure["ledger"]
                ),
                "ledger_fingerprint_after": rollback.canonical_fingerprint(
                    after_failure["ledger"]
                ),
                "application_state_unchanged": True,
                "catalog_state_unchanged": True,
            }

            restored_sha = self._create_fixture(self.paths[2], removed_bytes)
            require(restored_sha == version_2_sha, "Scenario C exact v2 restoration failed", c2.IsolationBlocked)
            self._assert_exact_fixtures((2, 3))
            retry = self.harness._run_migration("scenario_C_recovery_apply")
            require(
                retry.returncode == 0
                and "discovered=3" in retry.stdout
                and "already_applied=2" in retry.stdout
                and "applied_now=1" in retry.stdout,
                "Scenario C recovery did not apply version 3 exactly once",
                c2.ProductContractFailure,
            )
            recovered = self._capture_state(
                "scenario_C_recovered", (C_VERSION_2, C_VERSION_3)
            )
            self._assert_accepted_state(baseline, recovered)
            require(
                self._ledger_versions(recovered["ledger"]) == (1, 2, 3)
                and recovered["ledger"][:2] == established["ledger"]
                and recovered["ledger"][2]["name"] == C_VERSION_3.name
                and recovered["ledger"][2]["sha256"] == version_3_sha
                and recovered["established_probes"][C_VERSION_2.filename]
                == expected_probe(C_VERSION_2)
                and recovered["established_probes"][C_VERSION_3.filename]
                == expected_probe(C_VERSION_3),
                "Scenario C recovery state is incorrect",
                c2.ProductContractFailure,
            )
            idempotent = self.harness._run_migration("scenario_C_idempotent_reapply")
            require(
                idempotent.returncode == 0
                and "discovered=3" in idempotent.stdout
                and "already_applied=3" in idempotent.stdout
                and "applied_now=0" in idempotent.stdout,
                "Scenario C recovery is not idempotent",
                c2.ProductContractFailure,
            )
            idempotent_state = self._capture_state(
                "scenario_C_idempotent", (C_VERSION_2, C_VERSION_3)
            )
            require(
                idempotent_state == recovered,
                "Scenario C idempotent reapply changed PostgreSQL state",
                c2.ProductContractFailure,
            )
            self.evidence["recovery"] = {
                "restored_version_2_sha256": restored_sha,
                "version_3_sha256": version_3_sha,
                "apply_exit": retry.returncode,
                "version_3_ledger_rows": 1,
                "version_3_probe_rows": 1,
                "idempotent_exit": idempotent.returncode,
                "idempotent_applied_now": 0,
                "migration_advisory_lock_rows": 0,
            }
            self.harness._verify_owned_resources()
            self._record_baseline(baseline, recovered)

        def _run_d(self) -> None:
            baseline = self._prepare()
            version_3_sha = self._create_fixture(
                self.paths[3], fixture_sql(D_VERSION_3)
            )
            self._assert_exact_fixtures((3,))
            establish = self.harness._run_migration("scenario_D_apply_version_3")
            require(
                establish.returncode == 0
                and "discovered=2" in establish.stdout
                and "already_applied=1" in establish.stdout
                and "applied_now=1" in establish.stdout,
                "Scenario D could not establish applied history 1,3",
                c2.ProductContractFailure,
            )
            established = self._capture_state("scenario_D_established", (D_VERSION_3,))
            self._assert_accepted_state(baseline, established)
            require(
                self._ledger_versions(established["ledger"]) == (1, 3)
                and established["ledger"][0] == baseline.ledger[0]
                and established["ledger"][1]["name"] == D_VERSION_3.name
                and established["ledger"][1]["sha256"] == version_3_sha
                and established["established_probes"][D_VERSION_3.filename]
                == expected_probe(D_VERSION_3),
                "Scenario D registered state is not exactly versions 1,3",
                c2.ProductContractFailure,
            )
            version_2_sha = self._create_fixture(
                self.paths[2], fixture_sql(D_VERSION_2)
            )
            version_4_sha = self._create_fixture(
                self.paths[4], fixture_sql(D_VERSION_4)
            )
            self._assert_exact_fixtures((2, 3, 4))
            require(
                self._probe("scenario_D_lower_before_failure", D_VERSION_2)
                == ABSENT_PROBE
                and self._probe("scenario_D_sentinel_before_failure", D_VERSION_4)
                == ABSENT_PROBE,
                "Scenario D pending probes exist before failure",
                c2.ProductContractFailure,
            )
            pre_failure = self._capture_state("scenario_D_pre_failure", (D_VERSION_3,))
            self.evidence["construction"] = {
                "applied_versions": [1, 3],
                "discovered_versions": [1, 2, 3, 4],
                "lower_pending_version": 2,
                "sentinel_pending_version": 4,
                "lower_fixture_sha256": version_2_sha,
                "registered_fixture_sha256": version_3_sha,
                "sentinel_fixture_sha256": version_4_sha,
                "pending_probes_absent_before_failure": True,
            }
            self._run_expected_history_failure(
                "scenario_D_expected_nonprefix_history_failure",
                NON_PREFIX_HISTORY_ERROR,
            )
            after_failure = self._capture_state(
                "scenario_D_after_failure", (D_VERSION_3,)
            )
            after_failure["pending_probes"] = {
                "version_2": self._probe(
                    "scenario_D_lower_after_failure", D_VERSION_2
                ),
                "version_4": self._probe(
                    "scenario_D_sentinel_after_failure", D_VERSION_4
                ),
            }
            validate_fail_closed_snapshot(
                pre_failure,
                after_failure,
                expected_absent=("version_2", "version_4"),
            )
            self.evidence["fail_closed"] = {
                "version_2_probe_rows": 0,
                "version_4_probe_rows": 0,
                "version_2_ledger_rows": 0,
                "version_4_ledger_rows": 0,
                "migration_advisory_lock_rows": 0,
                "ledger_fingerprint_before": rollback.canonical_fingerprint(
                    pre_failure["ledger"]
                ),
                "ledger_fingerprint_after": rollback.canonical_fingerprint(
                    after_failure["ledger"]
                ),
                "application_state_unchanged": True,
                "catalog_state_unchanged": True,
            }

            removed_v2 = self._remove_fixture(2)
            require(
                sha256_bytes(removed_v2) == version_2_sha,
                "Scenario D removed fixture bytes changed",
                c2.IsolationBlocked,
            )
            self._assert_exact_fixtures((3, 4))
            retry = self.harness._run_migration("scenario_D_recovery_apply")
            require(
                retry.returncode == 0
                and "discovered=3" in retry.stdout
                and "already_applied=2" in retry.stdout
                and "applied_now=1" in retry.stdout,
                "Scenario D recovery did not apply version 4 exactly once",
                c2.ProductContractFailure,
            )
            recovered = self._capture_state(
                "scenario_D_recovered", (D_VERSION_3, D_VERSION_4)
            )
            self._assert_accepted_state(baseline, recovered)
            require(
                self._ledger_versions(recovered["ledger"]) == (1, 3, 4)
                and recovered["ledger"][:2] == established["ledger"]
                and recovered["ledger"][2]["name"] == D_VERSION_4.name
                and recovered["ledger"][2]["sha256"] == version_4_sha
                and recovered["established_probes"][D_VERSION_3.filename]
                == expected_probe(D_VERSION_3)
                and recovered["established_probes"][D_VERSION_4.filename]
                == expected_probe(D_VERSION_4)
                and self._probe("scenario_D_lower_after_recovery", D_VERSION_2)
                == ABSENT_PROBE,
                "Scenario D recovery state is incorrect",
                c2.ProductContractFailure,
            )
            idempotent = self.harness._run_migration("scenario_D_idempotent_reapply")
            require(
                idempotent.returncode == 0
                and "discovered=3" in idempotent.stdout
                and "already_applied=3" in idempotent.stdout
                and "applied_now=0" in idempotent.stdout,
                "Scenario D recovery is not idempotent",
                c2.ProductContractFailure,
            )
            idempotent_state = self._capture_state(
                "scenario_D_idempotent", (D_VERSION_3, D_VERSION_4)
            )
            require(
                idempotent_state == recovered
                and self._probe("scenario_D_lower_after_idempotent", D_VERSION_2)
                == ABSENT_PROBE,
                "Scenario D idempotent reapply changed PostgreSQL state",
                c2.ProductContractFailure,
            )
            self.evidence["recovery"] = {
                "removed_version_2_sha256": version_2_sha,
                "version_3_sha256": version_3_sha,
                "version_4_sha256": version_4_sha,
                "apply_exit": retry.returncode,
                "version_2_ledger_rows": 0,
                "version_4_ledger_rows": 1,
                "version_2_probe_rows": 0,
                "version_4_probe_rows": 1,
                "idempotent_exit": idempotent.returncode,
                "idempotent_applied_now": 0,
                "migration_advisory_lock_rows": 0,
            }
            self.harness._verify_owned_resources()
            self._record_baseline(baseline, recovered)

        def _record_baseline(self, baseline: Any, state: Mapping[str, Any]) -> None:
            self.evidence["baseline"] = {
                "application_tables": 9,
                "application_append_only_triggers": 4,
                "adoption_rows": len(baseline.adoption),
                "public_schema_fingerprint": baseline.public_fingerprint,
                "application_row_count": sum(
                    len(rows) for rows in baseline.business_rows.values()
                ),
                "metadata_guard_fingerprint": rollback.canonical_fingerprint(
                    baseline.metadata_catalog
                ),
                "final_ledger_versions": list(self._ledger_versions(state["ledger"])),
            }

        def run(self) -> None:
            if self.history_spec.key == "C":
                self._run_c()
            elif self.history_spec.key == "D":
                self._run_d()
            else:
                raise c2.ImplementationBoundaryBlocked(
                    "History scenario scope is not C or D"
                )

    return HistoryScenarioAcceptance


def classify_live_exception(
    c2: Any | None,
    rollback: Any | None,
    accepted: Any | None,
    lock: Any | None,
    exc: Exception,
) -> str:
    if isinstance(exc, HistoryAcceptanceError):
        return exc.result
    if c2 is not None:
        return c2.classify_live_exception(rollback, accepted, lock, exc)
    return RESULT_HARNESS_FAILURE


def sanitized_live_failure(
    c2: Any | None,
    exc: Exception,
    *,
    harness: Any | None = None,
    rollback: Any | None = None,
    accepted: Any | None = None,
    lock: Any | None = None,
) -> str:
    if c2 is not None and not isinstance(exc, HistoryAcceptanceError):
        return c2.sanitized_live_failure(
            exc,
            harness=harness,
            rollback=rollback,
            accepted=accepted,
            lock=lock,
        )
    candidate = f"{type(exc).__name__}: {exc}"
    if SENSITIVE_VALUE_PATTERN.search(candidate):
        return f"{type(exc).__name__}: detail withheld by credential boundary"
    return candidate


def execute_history_scenario(
    c2: Any,
    rollback: Any,
    accepted: Any,
    lock: Any,
    spec: HistoryScenario,
    *,
    expected_inventory_fingerprint: str | None,
) -> tuple[str, str, dict[str, Any]]:
    acceptance_class = make_history_acceptance_class(c2, rollback)
    acceptance = acceptance_class(
        accepted, lock, spec, expected_inventory_fingerprint
    )
    finalizer = rollback.FinalizerOnce(accepted, acceptance.harness)
    result = RESULT_LIVE_PASS
    failure = ""
    try:
        acceptance.run()
    except Exception as exc:
        result = classify_live_exception(c2, rollback, accepted, lock, exc)
        failure = sanitized_live_failure(
            c2,
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
        before_counts = acceptance.evidence.get("inventory", {}).get("before_counts")
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
        c2.assert_sanitized_evidence(acceptance.evidence)
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
            "Run offline history checks or the explicitly authorized "
            "M0-R01.4.2c.3 live A/B/C/D acceptance"
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run-focused-regression-checks",
        action="store_true",
        help="run pure offline contract checks",
    )
    mode.add_argument(
        "--confirm-disposable-synthetic-four-scenario-drift-run",
        action="store_true",
        help="run only disposable drift scenarios A, B, C, and D",
    )
    return parser


def _run_live() -> int:
    result = RESULT_LIVE_PASS
    failure = ""
    c2: Any | None = None
    rollback: Any | None = None
    accepted: Any | None = None
    lock: Any | None = None
    protected_before: dict[str, str] | None = None
    evidence: dict[str, Any] = {
        "ticket": "M0-R01.4.2c.3",
        "live_scope": {
            "scenario_a_constructed": False,
            "scenario_b_constructed": False,
            "scenario_c_constructed": False,
            "scenario_d_constructed": False,
            "fifth_scenario_constructed": False,
        },
        "scenarios": [],
    }
    expected_inventory_fingerprint: str | None = None
    try:
        protected_before = verify_repository_protected_hashes()
        validate_history_scenarios()
        c2, rollback, accepted, lock = load_authenticated_c2()
        protected_pre_live = verify_repository_protected_hashes(
            preflight=protected_before
        )
        evidence.update(
            {
                "repository": lock.repository_evidence(),
                "authenticated_dependencies": {
                    "c2_harness_sha256": C2_SHA256,
                    "rollback_harness_sha256": PROTECTED_HASHES[
                        "infra/postgres/tests/live_migration_rollback_acceptance.py"
                    ],
                    "lifecycle_harness_sha256": PROTECTED_HASHES[
                        "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
                    ],
                    "lock_harness_sha256": PROTECTED_HASHES[
                        "infra/postgres/tests/live_migration_lock_acceptance.py"
                    ],
                },
                "protected_hashes_before": protected_hash_evidence(protected_before),
                "protected_hashes_pre_live": protected_hash_evidence(
                    protected_pre_live
                ),
                "migration_advisory_lock_key": EXPECTED_MIGRATION_LOCK_KEY,
            }
        )

        execution_plan: tuple[tuple[str, Any], ...] = (
            ("A", c2.LIVE_SCENARIOS[0]),
            ("B", c2.LIVE_SCENARIOS[1]),
            ("C", HISTORY_SCENARIOS[0]),
            ("D", HISTORY_SCENARIOS[1]),
        )
        require(
            tuple(key for key, _spec in execution_plan) == ("A", "B", "C", "D")
            and len(execution_plan) == 4,
            "Live execution plan is not exactly A/B/C/D",
            ImplementationBoundaryBlocked,
        )
        project_ids: set[str] = set()
        database_ids: set[str] = set()
        for key, spec in execution_plan:
            verify_repository_protected_hashes(preflight=protected_before)
            evidence["live_scope"][f"scenario_{key.lower()}_constructed"] = True
            if key in ("A", "B"):
                scenario_result, scenario_failure, scenario_evidence = (
                    c2.execute_live_scenario(
                        rollback,
                        accepted,
                        lock,
                        spec,
                        expected_inventory_fingerprint=expected_inventory_fingerprint,
                    )
                )
                expected_result = RESULT_C2_PASS
            else:
                scenario_result, scenario_failure, scenario_evidence = (
                    execute_history_scenario(
                        c2,
                        rollback,
                        accepted,
                        lock,
                        spec,
                        expected_inventory_fingerprint=expected_inventory_fingerprint,
                    )
                )
                expected_result = RESULT_LIVE_PASS

            after_scenario = verify_repository_protected_hashes(
                preflight=protected_before
            )
            evidence[f"protected_hashes_after_{key.lower()}"] = (
                protected_hash_evidence(after_scenario)
            )
            scenario_evidence["protected_boundary"] = {
                "accepted_values_verified": True,
                "preflight_values_verified": True,
                "paths_verified": len(after_scenario),
            }
            evidence["scenarios"].append(scenario_evidence)
            if scenario_result != expected_result:
                result = scenario_result
                failure = scenario_failure
                break

            inventory = scenario_evidence.get("inventory", {})
            require(
                inventory.get("exact_restoration_verified") is True
                and inventory.get("before_fingerprint")
                == inventory.get("after_fingerprint"),
                f"Scenario {key} did not restore its exact Docker inventory",
                IsolationBlocked,
            )
            expected_inventory_fingerprint = str(inventory["before_fingerprint"])
            project = str(scenario_evidence.get("project", ""))
            database = str(scenario_evidence.get("database", ""))
            require(
                bool(project)
                and bool(database)
                and project not in project_ids
                and database not in database_ids,
                "Disposable scenarios did not use unique project/database identities",
                IsolationBlocked,
            )
            project_ids.add(project)
            database_ids.add(database)

        if result == RESULT_LIVE_PASS:
            require(
                len(evidence["scenarios"]) == 4
                and evidence["live_scope"]
                == {
                    "scenario_a_constructed": True,
                    "scenario_b_constructed": True,
                    "scenario_c_constructed": True,
                    "scenario_d_constructed": True,
                    "fifth_scenario_constructed": False,
                }
                and len(project_ids) == len(database_ids) == 4,
                "Live execution scope was not exactly four isolated scenarios",
                ImplementationBoundaryBlocked,
            )
            first_inventory = evidence["scenarios"][0]["inventory"]
            final_inventory = evidence["scenarios"][-1]["inventory"]
            require(
                first_inventory["before_fingerprint"]
                == final_inventory["after_fingerprint"],
                "Final Docker inventory differs from the initial inventory",
                IsolationBlocked,
            )
            evidence["docker_inventory"] = {
                "initial_counts": first_inventory["before_counts"],
                "final_counts": final_inventory["after_counts"],
                "initial_fingerprint": first_inventory["before_fingerprint"],
                "final_fingerprint": final_inventory["after_fingerprint"],
                "exactly_unchanged": True,
            }
    except Exception as exc:
        result = classify_live_exception(c2, rollback, accepted, lock, exc)
        failure = sanitized_live_failure(
            c2,
            exc,
            rollback=rollback,
            accepted=accepted,
            lock=lock,
        )
    finally:
        if protected_before is not None:
            try:
                evidence["protected_hashes_final"] = protected_hash_evidence(
                    verify_repository_protected_hashes(preflight=protected_before)
                )
            except Exception as hash_error:
                result = RESULT_BOUNDARY_BLOCKED
                failure = (
                    "Final protected-boundary verification failed: "
                    f"{type(hash_error).__name__}"
                )

    evidence.update(
        {
            "result": result,
            "failure": failure or None,
            "scenarios_completed": len(evidence["scenarios"]),
            "scenarios_expected": 4,
        }
    )
    try:
        assert_sanitized_evidence(evidence)
        if c2 is not None:
            c2.assert_sanitized_evidence(evidence)
    except Exception:
        result = RESULT_HARNESS_FAILURE
        evidence = {
            "ticket": "M0-R01.4.2c.3",
            "result": result,
            "failure": "Final evidence failed its sanitization boundary",
        }
    print(json.dumps(evidence, sort_keys=True, indent=2))
    print(result)
    return 0 if result == RESULT_LIVE_PASS else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_focused_regression_checks:
        try:
            checks = run_focused_regression_checks()
        except HistoryAcceptanceError as exc:
            print(f"Focused verification failure: {type(exc).__name__}: {exc}")
            return 2
        print(json.dumps(checks, sort_keys=True, indent=2))
        print(FOCUSED_PASS)
        return 0

    if not args.confirm_disposable_synthetic_four_scenario_drift_run:
        print("Explicit offline focused mode is required; no action taken.")
        print(RESULT_ISOLATION_BLOCKED)
        return 2

    return _run_live()


if __name__ == "__main__":
    raise SystemExit(main())
