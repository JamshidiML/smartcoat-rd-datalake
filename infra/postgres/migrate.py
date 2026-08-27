from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, Protocol

import bootstrap_contract


MIGRATION_DATABASE_ENV = "MIGRATION_DATABASE_URL"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().with_name("migrations")
DEFAULT_INIT_SQL = Path(__file__).resolve().with_name("init.sql")
MIGRATION_FILENAME = re.compile(
    r"^(?P<version>[0-9]{4})__(?P<name>[a-z][a-z0-9]*(?:_[a-z0-9]+)*)\.sql$"
)
ADVISORY_LOCK_KEY = int.from_bytes(b"SCMIGR01", byteorder="big", signed=False)

LEDGER_EXISTS_SQL = (
    "SELECT to_regclass('smartcoat_migrations.applied_migrations') IS NOT NULL"
)
SELECT_APPLIED_SQL = """
SELECT version, name, sha256
FROM smartcoat_migrations.applied_migrations
ORDER BY version
"""
INSERT_APPLIED_SQL = """
INSERT INTO smartcoat_migrations.applied_migrations (version, name, sha256)
VALUES (%s, %s, %s)
"""
TRY_LOCK_SQL = "SELECT pg_try_advisory_lock(%s)"
UNLOCK_SQL = "SELECT pg_advisory_unlock(%s)"

DATABASE_IDENTITY_SQL = """
SELECT current_database(), d.oid::bigint, current_user,
       current_setting('server_version')
FROM pg_database AS d
WHERE d.datname = current_database()
"""
MIGRATION_METADATA_STATE_SQL = """
SELECT
    to_regnamespace('smartcoat_migrations') IS NOT NULL,
    to_regclass('smartcoat_migrations.applied_migrations') IS NOT NULL,
    to_regclass('smartcoat_migrations.adoption_decisions') IS NOT NULL,
    to_regprocedure('smartcoat_migrations.reject_metadata_mutation()') IS NOT NULL,
    EXISTS (
        SELECT 1 FROM pg_trigger AS t
        WHERE t.tgrelid = to_regclass('smartcoat_migrations.applied_migrations')
          AND t.tgname = 'applied_migrations_append_only'
    ),
    EXISTS (
        SELECT 1 FROM pg_trigger AS t
        WHERE t.tgrelid = to_regclass('smartcoat_migrations.adoption_decisions')
          AND t.tgname = 'adoption_decisions_append_only'
    )
"""
SELECT_METADATA_GUARD_FUNCTION_SQL = """
SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
       pg_get_function_result(p.oid), l.lanname, p.prosecdef,
       p.provolatile, p.prosrc
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
JOIN pg_language AS l ON l.oid = p.prolang
WHERE n.nspname = 'smartcoat_migrations'
  AND p.proname = 'reject_metadata_mutation'
ORDER BY pg_get_function_identity_arguments(p.oid), p.oid
"""
SELECT_METADATA_GUARD_TRIGGERS_SQL = """
SELECT table_n.nspname, table_c.relname, t.tgname, t.tgenabled,
       t.tgisinternal, function_n.nspname, p.proname,
       pg_get_function_identity_arguments(p.oid),
       (t.tgtype & 1) <> 0, (t.tgtype & 2) <> 0,
       (t.tgtype & 4) <> 0, (t.tgtype & 8) <> 0,
       (t.tgtype & 16) <> 0, (t.tgtype & 32) <> 0,
       (t.tgtype & 64) <> 0
FROM pg_trigger AS t
JOIN pg_class AS table_c ON table_c.oid = t.tgrelid
JOIN pg_namespace AS table_n ON table_n.oid = table_c.relnamespace
JOIN pg_proc AS p ON p.oid = t.tgfoid
JOIN pg_namespace AS function_n ON function_n.oid = p.pronamespace
WHERE table_n.nspname = 'smartcoat_migrations'
  AND table_c.relname IN ('applied_migrations', 'adoption_decisions')
  AND t.tgname IN ('applied_migrations_append_only', 'adoption_decisions_append_only')
ORDER BY table_n.nspname, table_c.relname, t.tgname
"""
EXPECTED_METADATA_GUARD_FUNCTION = (
    "smartcoat_migrations",
    "reject_metadata_mutation",
    "",
    "trigger",
    "plpgsql",
    False,
    "v",
    "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END;",
)
EXPECTED_METADATA_GUARD_TRIGGERS = (
    (
        "smartcoat_migrations",
        "adoption_decisions",
        "adoption_decisions_append_only",
        "O",
        False,
        "smartcoat_migrations",
        "reject_metadata_mutation",
        "",
        True,
        True,
        False,
        True,
        True,
        False,
        False,
    ),
    (
        "smartcoat_migrations",
        "applied_migrations",
        "applied_migrations_append_only",
        "O",
        False,
        "smartcoat_migrations",
        "reject_metadata_mutation",
        "",
        True,
        True,
        False,
        True,
        True,
        False,
        False,
    ),
)
SET_ADOPTION_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
LOCK_BOOTSTRAP_TABLES_SQL = """
LOCK TABLE
    public.users,
    public.uploads,
    public.bronze_objects,
    public.ocr_jobs,
    public.ocr_runs,
    public.silver_drafts,
    public.review_decisions,
    public.silver_verified_records,
    public.audit_events
IN ACCESS SHARE MODE
"""
CREATE_MIGRATION_SCHEMA_SQL = "CREATE SCHEMA smartcoat_migrations"
CREATE_LEDGER_SQL = """
CREATE TABLE smartcoat_migrations.applied_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    name text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at_utc timestamptz NOT NULL DEFAULT transaction_timestamp(),
    applied_by text NOT NULL DEFAULT current_user
)
"""
CREATE_ADOPTION_EVIDENCE_SQL = """
CREATE TABLE smartcoat_migrations.adoption_decisions (
    action_identifier text PRIMARY KEY
        CHECK (action_identifier = 'ADOPT_BOOTSTRAP_BASELINE'),
    database_name text NOT NULL,
    database_oid bigint NOT NULL,
    migration_actor text NOT NULL,
    database_server_version text NOT NULL,
    adopted_at_utc timestamptz NOT NULL DEFAULT transaction_timestamp(),
    expected_structural_fingerprint text NOT NULL
        CHECK (expected_structural_fingerprint ~ '^[0-9a-f]{64}$'),
    observed_structural_fingerprint text NOT NULL
        CHECK (observed_structural_fingerprint ~ '^[0-9a-f]{64}$'),
    init_sql_sha256 text NOT NULL CHECK (init_sql_sha256 ~ '^[0-9a-f]{64}$'),
    baseline_version integer NOT NULL CHECK (baseline_version > 0),
    baseline_name text NOT NULL,
    baseline_sha256 text NOT NULL CHECK (baseline_sha256 ~ '^[0-9a-f]{64}$'),
    contract_version text NOT NULL,
    compared_categories_json jsonb NOT NULL,
    authorization_statement text NOT NULL
        CHECK (authorization_statement =
            'explicit adopt command with matching database identity')
)
"""
CREATE_METADATA_GUARD_FUNCTION_SQL = """
CREATE FUNCTION smartcoat_migrations.reject_metadata_mutation()
RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$guard$
"""
CREATE_LEDGER_GUARD_TRIGGER_SQL = """
CREATE TRIGGER applied_migrations_append_only
BEFORE UPDATE OR DELETE ON smartcoat_migrations.applied_migrations
FOR EACH ROW EXECUTE FUNCTION smartcoat_migrations.reject_metadata_mutation()
"""
CREATE_ADOPTION_GUARD_TRIGGER_SQL = """
CREATE TRIGGER adoption_decisions_append_only
BEFORE UPDATE OR DELETE ON smartcoat_migrations.adoption_decisions
FOR EACH ROW EXECUTE FUNCTION smartcoat_migrations.reject_metadata_mutation()
"""
INSERT_ADOPTION_EVIDENCE_SQL = """
INSERT INTO smartcoat_migrations.adoption_decisions (
    action_identifier, database_name, database_oid, migration_actor,
    database_server_version, expected_structural_fingerprint,
    observed_structural_fingerprint, init_sql_sha256, baseline_version,
    baseline_name, baseline_sha256, contract_version,
    compared_categories_json, authorization_statement
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
"""
SELECT_ADOPTION_EVIDENCE_SQL = """
SELECT action_identifier, database_name, database_oid, migration_actor,
       database_server_version, adopted_at_utc, expected_structural_fingerprint,
       observed_structural_fingerprint, init_sql_sha256, baseline_version,
       baseline_name, baseline_sha256, contract_version,
       compared_categories_json::text, authorization_statement
FROM smartcoat_migrations.adoption_decisions
ORDER BY adopted_at_utc, action_identifier
"""
ADOPTION_ACTION = "ADOPT_BOOTSTRAP_BASELINE"
ADOPTION_AUTHORIZATION = "explicit adopt command with matching database identity"


class MigrationError(RuntimeError):
    pass


class MigrationDefinitionError(MigrationError):
    pass


class MigrationConfigurationError(MigrationError):
    pass


class MigrationDriftError(MigrationError):
    pass


class MigrationLockUnavailable(MigrationError):
    pass


class MigrationUnmanagedDatabaseError(MigrationError):
    pass


class MigrationExecutionError(MigrationError):
    pass


class MigrationRecognitionError(MigrationError):
    pass


class MigrationAdoptionError(MigrationError):
    pass


class MigrationPartialStateError(MigrationAdoptionError):
    pass


def _decode_migration_content(path: Path, content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationDefinitionError(
            f"Migration {path.name} must be valid UTF-8"
        ) from exc


class CursorLike(Protocol):
    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


class ConnectionLike(Protocol):
    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> CursorLike: ...

    def transaction(self) -> ContextManager[Any]: ...


ConnectionFactory = Callable[[str], ContextManager[ConnectionLike]]


@dataclass(frozen=True, order=True)
class Migration:
    version: int
    name: str
    path: Path
    content: bytes
    sha256: str

    @property
    def sql(self) -> str:
        return _decode_migration_content(self.path, self.content)

    @property
    def identifier(self) -> str:
        return f"{self.version:04d}__{self.name}"


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    sha256: str


@dataclass(frozen=True)
class MigrationResult:
    discovered: int
    already_applied: int
    applied_now: tuple[int, ...]


@dataclass(frozen=True)
class DatabaseIdentity:
    name: str
    oid: int
    role: str
    server_version: str


@dataclass(frozen=True)
class AdoptionPlan:
    init_sql_sha256: str
    migrations: tuple[Migration, ...]
    baseline: Migration


@dataclass(frozen=True)
class AdoptionResult:
    status: str
    database_name: str
    database_oid: int
    structural_fingerprint: str
    evidence_inserted: bool


def content_checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def discover_migrations(directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[Migration]:
    """Discover .sql migrations; non-SQL directory artifacts are intentionally ignored."""
    if not directory.is_dir():
        raise MigrationDefinitionError(f"Migration directory does not exist: {directory}")

    migrations: list[Migration] = []
    versions: dict[int, Path] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationDefinitionError(
                f"Malformed migration filename {path.name!r}; expected NNNN__lower_snake_name.sql"
            )
        version = int(match.group("version"))
        if version <= 0:
            raise MigrationDefinitionError(f"Migration version must be positive: {path.name}")
        if version in versions:
            raise MigrationDefinitionError(
                f"Duplicate migration version {version:04d}: "
                f"{versions[version].name} and {path.name}"
            )
        content = path.read_bytes()
        if not content.strip():
            raise MigrationDefinitionError(f"Migration is empty: {path.name}")
        _decode_migration_content(path, content)
        versions[version] = path
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                content=content,
                sha256=content_checksum(content),
            )
        )
    if not migrations:
        raise MigrationDefinitionError(f"No migration files found in {directory}")
    return sorted(migrations, key=lambda migration: migration.version)


def inspect_migrations(directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[Migration]:
    """Return the offline plan without importing a PostgreSQL client or opening a connection."""
    return discover_migrations(directory)


def migration_database_url(environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    database_url = environment.get(MIGRATION_DATABASE_ENV, "").strip()
    if not database_url:
        raise MigrationConfigurationError(
            f"{MIGRATION_DATABASE_ENV} must be set explicitly for database execution; "
            "DATABASE_URL is intentionally ignored"
        )
    return database_url


def _default_connection_factory(database_url: str) -> ContextManager[ConnectionLike]:
    try:
        import psycopg
    except ImportError as exc:
        raise MigrationConfigurationError(
            "Database execution requires the repository's existing psycopg dependency; "
            "offline inspection remains available"
        ) from exc
    return psycopg.connect(database_url, autocommit=True)


def _single_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    return row[0]


def _applied_migration(row: Any) -> AppliedMigration:
    if isinstance(row, Mapping):
        return AppliedMigration(int(row["version"]), str(row["name"]), str(row["sha256"]))
    return AppliedMigration(int(row[0]), str(row[1]), str(row[2]))


def pending_migrations(
    migrations: Iterable[Migration], applied: Iterable[AppliedMigration]
) -> list[Migration]:
    ordered = sorted(migrations, key=lambda migration: migration.version)
    local = {migration.version: migration for migration in ordered}
    recorded: dict[int, AppliedMigration] = {}
    for item in applied:
        if item.version in recorded:
            raise MigrationDriftError(f"Migration ledger contains duplicate version {item.version:04d}")
        recorded[item.version] = item

    for version, item in recorded.items():
        migration = local.get(version)
        if migration is None:
            raise MigrationDriftError(
                f"Applied migration {version:04d} is missing from the repository"
            )
        if item.name != migration.name or item.sha256 != migration.sha256:
            raise MigrationDriftError(
                f"Applied migration {version:04d} no longer matches its recorded name and checksum"
            )

    discovered_versions = [migration.version for migration in ordered]
    applied_versions = sorted(recorded)
    if applied_versions != discovered_versions[: len(applied_versions)]:
        raise MigrationDriftError(
            "Applied migration history is not an ordered prefix of discovered migrations; "
            "refusing an unsafe out-of-order migration"
        )
    return ordered[len(applied_versions) :]


@contextmanager
def migration_advisory_lock(connection: ConnectionLike) -> Iterator[None]:
    """Acquire the one session lock shared by ordinary apply and explicit adopt."""
    lock_row = connection.execute(TRY_LOCK_SQL, (ADVISORY_LOCK_KEY,)).fetchone()
    if not bool(_single_value(lock_row)):
        raise MigrationLockUnavailable("Another migration runner holds the PostgreSQL advisory lock")
    try:
        yield
    finally:
        connection.execute(UNLOCK_SQL, (ADVISORY_LOCK_KEY,))


def prepare_adoption(
    directory: Path = DEFAULT_MIGRATIONS_DIR,
    init_sql_path: Path = DEFAULT_INIT_SQL,
) -> AdoptionPlan:
    """Validate every repository-owned adoption input before a connection exists."""
    try:
        init_content = init_sql_path.read_bytes()
    except OSError as exc:
        raise MigrationDefinitionError(
            f"Authoritative bootstrap cannot be read: {init_sql_path}"
        ) from exc
    init_checksum = content_checksum(init_content)
    if init_checksum != bootstrap_contract.EXPECTED_INIT_SQL_SHA256:
        raise MigrationDefinitionError(
            "Authoritative bootstrap init.sql does not match its accepted SHA-256"
        )

    migrations = discover_migrations(directory)
    baseline = next(
        (item for item in migrations if item.version == bootstrap_contract.EXPECTED_BASELINE_VERSION),
        None,
    )
    if baseline is None:
        raise MigrationDefinitionError("Accepted baseline migration 0001 is missing")
    if (
        baseline.name != bootstrap_contract.EXPECTED_BASELINE_NAME
        or baseline.sha256 != bootstrap_contract.EXPECTED_BASELINE_SHA256
    ):
        raise MigrationDefinitionError(
            "Baseline migration 0001 does not match its accepted name and checksum"
        )
    return AdoptionPlan(init_checksum, tuple(migrations), baseline)


def _database_identity(row: Any) -> DatabaseIdentity:
    if row is None:
        raise MigrationRecognitionError("Required database identity evidence is unavailable")
    if isinstance(row, Mapping):
        values = (
            row.get("current_database"),
            row.get("database_oid"),
            row.get("current_user"),
            row.get("server_version"),
        )
    else:
        values = tuple(row)
    if len(values) != 4 or any(value is None for value in values):
        raise MigrationRecognitionError("Required database identity evidence is incomplete")
    return DatabaseIdentity(str(values[0]), int(values[1]), str(values[2]), str(values[3]))


def _metadata_state(row: Any) -> tuple[bool, ...]:
    if row is None:
        raise MigrationRecognitionError("Migration-owned catalog evidence is unavailable")
    values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
    if len(values) != 6 or any(value is None for value in values):
        raise MigrationRecognitionError("Migration-owned catalog evidence is incomplete")
    return tuple(bool(value) for value in values)


def _normalize_metadata_guard_rows(rows: Iterable[Any]) -> tuple[tuple[Any, ...], ...]:
    normalized: list[tuple[Any, ...]] = []
    for row in rows:
        values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
        normalized.append(
            tuple(" ".join(value.split()) if isinstance(value, str) else value for value in values)
        )
    return tuple(sorted(normalized, key=repr))


def validate_metadata_guard_contract(connection: ConnectionLike) -> None:
    """Verify repeat-adoption metadata guards still enforce append-only semantics."""
    try:
        function_rows = connection.execute(
            SELECT_METADATA_GUARD_FUNCTION_SQL
        ).fetchall()
        trigger_rows = connection.execute(
            SELECT_METADATA_GUARD_TRIGGERS_SQL
        ).fetchall()
    except Exception as exc:
        raise MigrationRecognitionError(
            "Migration-owned append-only guard contract could not be read"
        ) from exc

    observed_function = _normalize_metadata_guard_rows(function_rows)
    observed_triggers = _normalize_metadata_guard_rows(trigger_rows)
    if observed_function != (EXPECTED_METADATA_GUARD_FUNCTION,):
        raise MigrationAdoptionError(
            "Migration-owned append-only guard function semantics do not match the accepted contract"
        )
    if observed_triggers != EXPECTED_METADATA_GUARD_TRIGGERS:
        raise MigrationAdoptionError(
            "Migration-owned append-only trigger semantics do not match the accepted contract"
        )


def read_catalog_snapshot(connection: ConnectionLike) -> dict[str, list[Any]]:
    snapshot: dict[str, list[Any]] = {}
    for category, query in bootstrap_contract.CATALOG_QUERIES.items():
        try:
            rows = connection.execute(query).fetchall()
        except Exception as exc:
            raise MigrationRecognitionError(
                f"Required catalog category {category!r} could not be read"
            ) from exc
        if rows is None:
            raise MigrationRecognitionError(
                f"Required catalog category {category!r} returned no evidence collection"
            )
        snapshot[category] = rows
    return snapshot


def recognize_bootstrap(connection: ConnectionLike) -> str:
    """Require exact structural equality with the pinned bootstrap catalog."""
    snapshot = read_catalog_snapshot(connection)
    try:
        observed = bootstrap_contract.normalize_catalog(snapshot)
    except ValueError as exc:
        raise MigrationRecognitionError("Bootstrap catalog evidence is incomplete") from exc
    if observed != bootstrap_contract.EXPECTED_NORMALIZED_CATALOG:
        differing = [
            category
            for category in sorted(bootstrap_contract.CATALOG_QUERIES)
            if observed[category]
            != bootstrap_contract.EXPECTED_NORMALIZED_CATALOG[category]
        ]
        raise MigrationRecognitionError(
            "Database does not match the accepted bootstrap structural contract; "
            f"differing categories: {', '.join(differing)}"
        )
    fingerprint = bootstrap_contract.catalog_fingerprint(snapshot)
    if fingerprint != bootstrap_contract.EXPECTED_STRUCTURAL_FINGERPRINT:
        raise MigrationRecognitionError("Normalized bootstrap fingerprint is inconsistent")
    return fingerprint


def _evidence_values(
    identity: DatabaseIdentity, plan: AdoptionPlan, fingerprint: str
) -> tuple[Any, ...]:
    return (
        ADOPTION_ACTION,
        identity.name,
        identity.oid,
        identity.role,
        identity.server_version,
        bootstrap_contract.EXPECTED_STRUCTURAL_FINGERPRINT,
        fingerprint,
        plan.init_sql_sha256,
        plan.baseline.version,
        plan.baseline.name,
        plan.baseline.sha256,
        bootstrap_contract.CONTRACT_VERSION,
        bootstrap_contract.COMPARED_CATEGORIES_JSON,
        ADOPTION_AUTHORIZATION,
    )


def _validate_existing_adoption(
    connection: ConnectionLike,
    plan: AdoptionPlan,
    identity: DatabaseIdentity,
) -> None:
    validate_metadata_guard_contract(connection)
    applied = [
        _applied_migration(row)
        for row in connection.execute(SELECT_APPLIED_SQL).fetchall()
    ]
    pending_migrations(plan.migrations, applied)
    rows = connection.execute(SELECT_ADOPTION_EVIDENCE_SQL).fetchall()
    if len(rows) != 1:
        raise MigrationAdoptionError(
            "Managed database must contain exactly one baseline adoption decision"
        )
    row = rows[0]
    values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
    if len(values) != 15 or values[5] is None:
        raise MigrationAdoptionError("Existing adoption evidence is incomplete")
    compared_categories = json.dumps(
        json.loads(str(values[13])), sort_keys=True, separators=(",", ":")
    )
    expected = _evidence_values(
        identity,
        plan,
        bootstrap_contract.EXPECTED_STRUCTURAL_FINGERPRINT,
    )
    # Server patch versions and the actor that first adopted are retained as
    # evidence but do not make a later, valid migration-admin session drift.
    comparable_observed = values[:3] + values[6:13] + (compared_categories, values[14])
    comparable_expected = expected[:3] + expected[5:12] + (expected[12], expected[13])
    if comparable_observed != comparable_expected:
        raise MigrationAdoptionError(
            "Existing adoption evidence does not match the accepted baseline contract"
        )


def adopt_database(
    connection: ConnectionLike,
    plan: AdoptionPlan,
    expected_database_name: str,
) -> AdoptionResult:
    if not expected_database_name.strip():
        raise MigrationConfigurationError("Expected database identity must not be empty")

    with migration_advisory_lock(connection):
        identity = _database_identity(connection.execute(DATABASE_IDENTITY_SQL).fetchone())
        if identity.name != expected_database_name:
            raise MigrationAdoptionError(
                "Connected database identity does not match the explicitly expected name"
            )

        state = _metadata_state(connection.execute(MIGRATION_METADATA_STATE_SQL).fetchone())
        if all(state):
            _validate_existing_adoption(connection, plan, identity)
            return AdoptionResult(
                "ALREADY_ADOPTED",
                identity.name,
                identity.oid,
                bootstrap_contract.EXPECTED_STRUCTURAL_FINGERPRINT,
                False,
            )
        if any(state):
            raise MigrationPartialStateError(
                "Migration-owned metadata is partial or inconsistent; refusing repair"
            )

        try:
            with connection.transaction():
                connection.execute(SET_ADOPTION_LOCK_TIMEOUT_SQL)
                connection.execute(LOCK_BOOTSTRAP_TABLES_SQL)
                fingerprint = recognize_bootstrap(connection)
                connection.execute(CREATE_MIGRATION_SCHEMA_SQL)
                connection.execute(CREATE_LEDGER_SQL)
                connection.execute(CREATE_ADOPTION_EVIDENCE_SQL)
                connection.execute(CREATE_METADATA_GUARD_FUNCTION_SQL)
                connection.execute(CREATE_LEDGER_GUARD_TRIGGER_SQL)
                connection.execute(CREATE_ADOPTION_GUARD_TRIGGER_SQL)
                connection.execute(plan.baseline.sql)
                connection.execute(
                    INSERT_APPLIED_SQL,
                    (plan.baseline.version, plan.baseline.name, plan.baseline.sha256),
                )
                connection.execute(
                    INSERT_ADOPTION_EVIDENCE_SQL,
                    _evidence_values(identity, plan, fingerprint),
                )
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationAdoptionError(
                "Baseline adoption failed and all migration-owned changes were rolled back"
            ) from exc

        return AdoptionResult(
            "ADOPTED", identity.name, identity.oid, fingerprint, True
        )


def apply_migrations(
    connection: ConnectionLike, migrations: Iterable[Migration]
) -> MigrationResult:
    ordered = sorted(migrations, key=lambda migration: migration.version)
    with migration_advisory_lock(connection):
        ledger_row = connection.execute(LEDGER_EXISTS_SQL).fetchone()
        if not bool(_single_value(ledger_row)):
            raise MigrationUnmanagedDatabaseError(
                "Database is unmanaged; M0-R01.2 adoption is required before ordinary "
                "migration apply"
            )

        applied = [
            _applied_migration(row)
            for row in connection.execute(SELECT_APPLIED_SQL).fetchall()
        ]
        pending = pending_migrations(ordered, applied)
        applied_now: list[int] = []
        for migration in pending:
            try:
                with connection.transaction():
                    connection.execute(migration.sql)
                    connection.execute(
                        INSERT_APPLIED_SQL,
                        (migration.version, migration.name, migration.sha256),
                    )
            except Exception as exc:
                raise MigrationExecutionError(
                    f"Migration {migration.identifier} failed and was rolled back"
                ) from exc
            applied_now.append(migration.version)
        return MigrationResult(
            discovered=len(ordered),
            already_applied=len(applied),
            applied_now=tuple(applied_now),
        )


def run_migrations(
    database_url: str,
    directory: Path = DEFAULT_MIGRATIONS_DIR,
    connection_factory: ConnectionFactory | None = None,
) -> MigrationResult:
    if not database_url.strip():
        raise MigrationConfigurationError("Migration database configuration must not be empty")
    migrations = discover_migrations(directory)
    factory = _default_connection_factory if connection_factory is None else connection_factory
    try:
        with factory(database_url) as connection:
            return apply_migrations(connection, migrations)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationExecutionError(
            "Migration database operation failed; connection details are not reported"
        ) from exc


def run_from_environment(
    environ: Mapping[str, str] | None = None,
    directory: Path = DEFAULT_MIGRATIONS_DIR,
    connection_factory: ConnectionFactory | None = None,
) -> MigrationResult:
    return run_migrations(
        migration_database_url(environ),
        directory=directory,
        connection_factory=connection_factory,
    )


def run_adoption(
    database_url: str,
    expected_database_name: str,
    directory: Path = DEFAULT_MIGRATIONS_DIR,
    init_sql_path: Path = DEFAULT_INIT_SQL,
    connection_factory: ConnectionFactory | None = None,
) -> AdoptionResult:
    if not database_url.strip():
        raise MigrationConfigurationError("Migration database configuration must not be empty")
    if not expected_database_name.strip():
        raise MigrationConfigurationError("Expected database identity must not be empty")
    plan = prepare_adoption(directory, init_sql_path)
    factory = _default_connection_factory if connection_factory is None else connection_factory
    try:
        with factory(database_url) as connection:
            return adopt_database(connection, plan, expected_database_name)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationExecutionError(
            "Adoption database operation failed; connection details are not reported"
        ) from exc


def run_adoption_from_environment(
    expected_database_name: str,
    environ: Mapping[str, str] | None = None,
    directory: Path = DEFAULT_MIGRATIONS_DIR,
    init_sql_path: Path = DEFAULT_INIT_SQL,
    connection_factory: ConnectionFactory | None = None,
) -> AdoptionResult:
    return run_adoption(
        migration_database_url(environ),
        expected_database_name,
        directory=directory,
        init_sql_path=init_sql_path,
        connection_factory=connection_factory,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect, explicitly adopt, or apply repository-local PostgreSQL migrations."
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="migration directory (default: infra/postgres/migrations)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect", help="validate and print the offline migration plan")
    subparsers.add_parser(
        "apply",
        help=f"apply migrations using the explicitly supplied {MIGRATION_DATABASE_ENV}",
    )
    adopt_parser = subparsers.add_parser(
        "adopt",
        help="explicitly recognize and adopt an existing pinned-bootstrap database",
    )
    adopt_parser.add_argument(
        "expected_database_name",
        help="exact connected database name the operator explicitly intends to adopt",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            migrations = inspect_migrations(args.migrations_dir)
            for migration in migrations:
                print(f"{migration.identifier} sha256={migration.sha256}")
            return 0

        if args.command == "adopt":
            result = run_adoption_from_environment(
                args.expected_database_name,
                directory=args.migrations_dir,
            )
            print(
                f"Adoption result: status={result.status} "
                f"database={result.database_name} oid={result.database_oid} "
                f"evidence_inserted={str(result.evidence_inserted).lower()}"
            )
            return 0

        result = run_from_environment(directory=args.migrations_dir)
        print(
            f"Migration run complete: discovered={result.discovered} "
            f"already_applied={result.already_applied} applied_now={len(result.applied_now)}"
        )
        return 0
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
