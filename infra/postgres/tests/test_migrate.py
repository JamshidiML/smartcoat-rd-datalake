from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator


POSTGRES_INFRA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POSTGRES_INFRA))

import migrate  # noqa: E402
import bootstrap_contract  # noqa: E402


class FakeCursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class FakeTransaction(AbstractContextManager[None]):
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.snapshot: dict[str, Any] | None = None

    def __enter__(self) -> None:
        self.snapshot = self.connection.persistent_snapshot()
        self.connection.transaction_entries += 1
        self.connection.calls.append("transaction_enter")
        return None

    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> bool:
        if exception_type is None:
            self.connection.commits += 1
            self.connection.calls.append("transaction_commit")
            return False
        assert self.snapshot is not None
        self.connection.restore_persistent_snapshot(self.snapshot)
        self.connection.rollbacks += 1
        self.connection.calls.append("transaction_rollback")
        return False


class FakeConnection:
    def __init__(
        self,
        *,
        lock_available: bool = True,
        ledger_present: bool = True,
        ledger: dict[int, tuple[str, str]] | None = None,
        fail_on_applied_read: bool = False,
        identity: tuple[str, int, str, str] = (
            "smartcoat_rd", 4242, "migration_admin", "17.6"
        ),
        catalog: dict[str, list[Any]] | None = None,
        migration_state: tuple[bool, ...] | None = None,
        fail_at: str | None = None,
    ) -> None:
        self.lock_available = lock_available
        self.ledger_present = ledger_present
        self.ledger = dict(ledger or {})
        self.fail_on_applied_read = fail_on_applied_read
        self.identity = identity
        self.catalog = deepcopy(
            bootstrap_contract.EXPECTED_CATALOG if catalog is None else catalog
        )
        state = migration_state or (
            ledger_present,
            ledger_present,
            ledger_present,
            ledger_present,
            ledger_present,
            ledger_present,
        )
        (
            self.migration_schema_present,
            self.ledger_present,
            self.adoption_table_present,
            self.metadata_function_present,
            self.ledger_trigger_present,
            self.adoption_trigger_present,
        ) = state
        self.guard_function = (
            deepcopy(migrate.EXPECTED_METADATA_GUARD_FUNCTION)
            if self.metadata_function_present
            else None
        )
        self.guard_triggers = [
            deepcopy(trigger)
            for trigger in migrate.EXPECTED_METADATA_GUARD_TRIGGERS
            if (
                trigger[2] == "applied_migrations_append_only"
                and self.ledger_trigger_present
            )
            or (
                trigger[2] == "adoption_decisions_append_only"
                and self.adoption_trigger_present
            )
        ]
        self.fail_at = fail_at
        self.evidence: list[tuple[Any, ...]] = []
        self.business_rows = [{"synthetic": "row-remains-untouched"}]
        self.effects: list[str] = []
        self.calls: list[str] = []
        self.transaction_entries = 0
        self.commits = 0
        self.rollbacks = 0
        self.lock_attempts = 0
        self.unlocks = 0

    def persistent_snapshot(self) -> dict[str, Any]:
        return deepcopy(
            {
                "ledger": self.ledger,
                "effects": self.effects,
                "migration_schema_present": self.migration_schema_present,
                "ledger_present": self.ledger_present,
                "adoption_table_present": self.adoption_table_present,
                "metadata_function_present": self.metadata_function_present,
                "ledger_trigger_present": self.ledger_trigger_present,
                "adoption_trigger_present": self.adoption_trigger_present,
                "guard_function": self.guard_function,
                "guard_triggers": self.guard_triggers,
                "evidence": self.evidence,
                "business_rows": self.business_rows,
            }
        )

    def restore_persistent_snapshot(self, snapshot: dict[str, Any]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)

    def maybe_fail(self, label: str) -> None:
        if self.fail_at == label:
            raise RuntimeError(f"synthetic failure at {label}")

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def execute(
        self, query: str, params: tuple[Any, ...] | None = None
    ) -> FakeCursor:
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT pg_try_advisory_lock"):
            self.lock_attempts += 1
            self.calls.append("lock")
            return FakeCursor([(self.lock_available,)])
        if normalized.startswith("SELECT pg_advisory_unlock"):
            self.unlocks += 1
            self.calls.append("unlock")
            return FakeCursor([(True,)])
        if normalized.startswith("SELECT current_database()"):
            self.calls.append("database_identity")
            return FakeCursor([self.identity])
        if "to_regnamespace('smartcoat_migrations')" in normalized:
            self.calls.append("metadata_state")
            return FakeCursor([(
                self.migration_schema_present,
                self.ledger_present,
                self.adoption_table_present,
                self.metadata_function_present,
                self.ledger_trigger_present,
                self.adoption_trigger_present,
            )])
        if normalized == " ".join(migrate.SELECT_METADATA_GUARD_FUNCTION_SQL.split()):
            self.calls.append("select_guard_function")
            self.maybe_fail("guard_function_contract")
            return FakeCursor(
                [] if self.guard_function is None else [deepcopy(self.guard_function)]
            )
        if normalized == " ".join(migrate.SELECT_METADATA_GUARD_TRIGGERS_SQL.split()):
            self.calls.append("select_guard_triggers")
            self.maybe_fail("guard_trigger_contract")
            return FakeCursor(deepcopy(self.guard_triggers))
        for category, catalog_query in bootstrap_contract.CATALOG_QUERIES.items():
            if normalized == " ".join(catalog_query.split()):
                self.calls.append(f"catalog:{category}")
                self.maybe_fail(f"catalog:{category}")
                if category not in self.catalog:
                    raise RuntimeError("synthetic catalog visibility failure")
                return FakeCursor(deepcopy(self.catalog[category]))
        if normalized.startswith("SELECT to_regclass"):
            self.calls.append("ledger_exists")
            return FakeCursor([(self.ledger_present,)])
        if normalized.startswith("CREATE SCHEMA"):
            self.calls.append("create_schema")
            self.maybe_fail("create_schema")
            self.migration_schema_present = True
            return FakeCursor()
        if normalized.startswith("CREATE TABLE smartcoat_migrations.applied_migrations"):
            self.calls.append("create_ledger")
            self.maybe_fail("create_ledger")
            self.ledger_present = True
            return FakeCursor()
        if normalized.startswith("CREATE TABLE smartcoat_migrations.adoption_decisions"):
            self.calls.append("create_evidence_table")
            self.maybe_fail("create_evidence_table")
            self.adoption_table_present = True
            return FakeCursor()
        if normalized.startswith("CREATE FUNCTION smartcoat_migrations"):
            self.calls.append("create_metadata_function")
            self.maybe_fail("create_metadata_function")
            self.metadata_function_present = True
            self.guard_function = deepcopy(migrate.EXPECTED_METADATA_GUARD_FUNCTION)
            return FakeCursor()
        if normalized.startswith("CREATE TRIGGER applied_migrations_append_only"):
            self.calls.append("create_ledger_trigger")
            self.maybe_fail("create_ledger_trigger")
            self.ledger_trigger_present = True
            self.guard_triggers.append(
                deepcopy(
                    next(
                        trigger
                        for trigger in migrate.EXPECTED_METADATA_GUARD_TRIGGERS
                        if trigger[2] == "applied_migrations_append_only"
                    )
                )
            )
            return FakeCursor()
        if normalized.startswith("CREATE TRIGGER adoption_decisions_append_only"):
            self.calls.append("create_adoption_trigger")
            self.maybe_fail("create_adoption_trigger")
            self.adoption_trigger_present = True
            self.guard_triggers.append(
                deepcopy(
                    next(
                        trigger
                        for trigger in migrate.EXPECTED_METADATA_GUARD_TRIGGERS
                        if trigger[2] == "adoption_decisions_append_only"
                    )
                )
            )
            return FakeCursor()
        if normalized.startswith("SET LOCAL lock_timeout"):
            self.calls.append("set_lock_timeout")
            return FakeCursor()
        if normalized.startswith("LOCK TABLE"):
            self.calls.append("lock_bootstrap_tables")
            self.maybe_fail("lock_bootstrap_tables")
            return FakeCursor()
        if normalized.startswith("SELECT version, name, sha256"):
            self.calls.append("select_applied")
            if self.fail_on_applied_read:
                raise RuntimeError("synthetic applied-migration read failure")
            return FakeCursor(
                [
                    (version, name, checksum)
                    for version, (name, checksum) in sorted(self.ledger.items())
                ]
            )
        if normalized.startswith("INSERT INTO smartcoat_migrations.applied_migrations"):
            self.calls.append("ledger_insert")
            assert params is not None
            version, name, checksum = params
            if version in self.ledger:
                raise RuntimeError("duplicate ledger version")
            self.maybe_fail("ledger_insert")
            self.ledger[int(version)] = (str(name), str(checksum))
            self.maybe_fail("after_ledger_insert")
            return FakeCursor()
        if normalized.startswith("INSERT INTO smartcoat_migrations.adoption_decisions"):
            self.calls.append("evidence_insert")
            self.maybe_fail("evidence_insert")
            assert params is not None
            # The sixth value is the PostgreSQL-generated adopted_at_utc column.
            self.evidence.append(tuple(params[:5]) + ("2026-08-22T12:00:00+00:00",) + tuple(params[5:]))
            self.maybe_fail("after_evidence_insert")
            return FakeCursor()
        if normalized.startswith("SELECT action_identifier, database_name"):
            self.calls.append("select_evidence")
            return FakeCursor(deepcopy(self.evidence))
        if normalized.startswith("UPDATE smartcoat_migrations.adoption_decisions"):
            if self.adoption_trigger_present:
                raise RuntimeError("adoption_decisions is append-only")
        if normalized.startswith("DELETE FROM smartcoat_migrations.adoption_decisions"):
            if self.adoption_trigger_present:
                raise RuntimeError("adoption_decisions is append-only")
        if normalized.startswith("UPDATE smartcoat_migrations.applied_migrations"):
            if self.ledger_trigger_present:
                raise RuntimeError("applied_migrations is append-only")
        if normalized.startswith("DELETE FROM smartcoat_migrations.applied_migrations"):
            if self.ledger_trigger_present:
                raise RuntimeError("applied_migrations is append-only")
        self.calls.append("migration_sql")
        self.effects.append(query)
        if "M0-R01.1 migration-foundation baseline validation marker" in query:
            self.calls[-1] = "baseline_sql"
            self.maybe_fail("baseline_sql")
        if "RAISE_SYNTHETIC_FAILURE" in query:
            raise RuntimeError("synthetic migration failure")
        return FakeCursor()


def write_migration(directory: Path, filename: str, content: str) -> Path:
    path = directory / filename
    path.write_text(content)
    return path


def write_migration_bytes(directory: Path, filename: str, content: bytes) -> Path:
    path = directory / filename
    path.write_bytes(content)
    return path


class MigrationRunnerTests(unittest.TestCase):
    def test_discovery_orders_migrations_by_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0002__second.sql", "SELECT 2;\n")
            write_migration(directory, "0001__first.sql", "SELECT 1;\n")
            self.assertEqual(
                [1, 2],
                [migration.version for migration in migrate.discover_migrations(directory)],
            )

    def test_duplicate_versions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__first.sql", "SELECT 1;\n")
            write_migration(directory, "0001__duplicate.sql", "SELECT 2;\n")
            with self.assertRaisesRegex(migrate.MigrationDefinitionError, "Duplicate"):
                migrate.discover_migrations(directory)

    def test_malformed_sql_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "baseline.sql", "SELECT 1;\n")
            with self.assertRaisesRegex(migrate.MigrationDefinitionError, "Malformed"):
                migrate.discover_migrations(directory)

    def test_version_zero_is_rejected_before_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0000__invalid.sql", "SELECT 0;\n")
            called = False

            @contextmanager
            def connection_factory(database_url: str) -> Iterator[FakeConnection]:
                nonlocal called
                called = True
                del database_url
                yield FakeConnection()

            with self.assertRaisesRegex(
                migrate.MigrationDefinitionError, "version must be positive"
            ):
                migrate.run_migrations(
                    "postgresql://synthetic",
                    directory=directory,
                    connection_factory=connection_factory,
                )
            self.assertFalse(called)

    def test_content_checksum_is_stable_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            content = b"SELECT '\xc3\xa9';\r\n"
            write_migration_bytes(directory, "0001__exact_bytes.sql", content)

            migration = migrate.discover_migrations(directory)[0]

            self.assertEqual(content, migration.content)
            self.assertEqual(hashlib.sha256(content).hexdigest(), migration.sha256)
            self.assertEqual(content.decode("utf-8"), migration.sql)

    def test_offline_inspection_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration_bytes(
                directory,
                "0001__invalid_utf8.sql",
                b"SELECT '\xff';\n",
            )
            with self.assertRaisesRegex(
                migrate.MigrationDefinitionError,
                r"0001__invalid_utf8\.sql must be valid UTF-8",
            ):
                migrate.inspect_migrations(directory)

    def test_later_invalid_utf8_rejects_plan_before_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__valid.sql", "SELECT 1;\n")
            write_migration_bytes(
                directory,
                "0002__invalid_utf8.sql",
                b"SELECT '\xff';\n",
            )
            connection = FakeConnection()
            called = False

            @contextmanager
            def connection_factory(database_url: str) -> Iterator[FakeConnection]:
                nonlocal called
                called = True
                del database_url
                yield connection

            with self.assertRaisesRegex(
                migrate.MigrationDefinitionError,
                r"0002__invalid_utf8\.sql must be valid UTF-8",
            ):
                migrate.run_migrations(
                    "postgresql://synthetic",
                    directory=directory,
                    connection_factory=connection_factory,
                )
            self.assertFalse(called)
            self.assertEqual([], connection.calls)
            self.assertEqual([], connection.effects)

    def test_non_prefix_applied_history_with_missing_middle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__first.sql", "SELECT 1;\n")
            write_migration(directory, "0002__second.sql", "SELECT 2;\n")
            write_migration(directory, "0003__third.sql", "SELECT 3;\n")
            migrations = migrate.discover_migrations(directory)
            connection = FakeConnection(
                ledger={
                    migration.version: (migration.name, migration.sha256)
                    for migration in migrations
                    if migration.version in {1, 3}
                }
            )
            before_ledger = deepcopy(connection.ledger)

            with self.assertRaisesRegex(
                migrate.MigrationDriftError,
                "not an ordered prefix.*unsafe out-of-order migration",
            ):
                migrate.apply_migrations(connection, migrations)

            self.assertEqual(before_ledger, connection.ledger)
            self.assertEqual([], connection.effects)
            self.assertEqual(0, connection.transaction_entries)
            self.assertEqual(0, connection.commits)
            self.assertEqual(0, connection.rollbacks)
            self.assertEqual(
                ["lock", "ledger_exists", "select_applied", "unlock"],
                connection.calls,
            )

    def test_non_prefix_applied_history_starting_at_second_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__first.sql", "SELECT 1;\n")
            write_migration(directory, "0002__second.sql", "SELECT 2;\n")
            migrations = migrate.discover_migrations(directory)
            second = migrations[1]

            with self.assertRaisesRegex(
                migrate.MigrationDriftError,
                "not an ordered prefix.*unsafe out-of-order migration",
            ):
                migrate.pending_migrations(
                    migrations,
                    [
                        migrate.AppliedMigration(
                            second.version,
                            second.name,
                            second.sha256,
                        )
                    ],
                )

    def test_discovered_numbering_gap_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__first.sql", "SELECT 1;\n")
            write_migration(directory, "0003__third.sql", "SELECT 3;\n")
            migrations = migrate.discover_migrations(directory)
            first = migrations[0]

            pending = migrate.pending_migrations(
                migrations,
                [
                    migrate.AppliedMigration(
                        first.version,
                        first.name,
                        first.sha256,
                    )
                ],
            )

            self.assertEqual([3], [migration.version for migration in pending])

    def test_checksum_drift_for_applied_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__baseline.sql", "SELECT 1;\n")
            migration = migrate.discover_migrations(directory)[0]
            connection = FakeConnection(ledger={1: (migration.name, "0" * 64)})
            with self.assertRaises(migrate.MigrationDriftError):
                migrate.apply_migrations(connection, [migration])
            self.assertEqual([], connection.effects)
            self.assertEqual(1, connection.unlocks)
            self.assertEqual(
                ["lock", "ledger_exists", "select_applied", "unlock"],
                connection.calls,
            )

    def test_unmanaged_database_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__baseline.sql", "SELECT 1;\n")
            migration = migrate.discover_migrations(directory)[0]
            connection = FakeConnection(ledger_present=False)
            before = (
                connection.ledger_present,
                deepcopy(connection.ledger),
                list(connection.effects),
            )

            with self.assertRaisesRegex(
                migrate.MigrationUnmanagedDatabaseError, "M0-R01.2 adoption"
            ):
                migrate.apply_migrations(connection, [migration])

            self.assertEqual(
                before,
                (
                    connection.ledger_present,
                    connection.ledger,
                    connection.effects,
                ),
            )
            self.assertEqual(0, connection.transaction_entries)
            self.assertEqual(1, connection.unlocks)
            self.assertEqual(["lock", "ledger_exists", "unlock"], connection.calls)

    def test_success_records_migration_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__baseline.sql", "SELECT 1;\n")
            migration = migrate.discover_migrations(directory)[0]
            connection = FakeConnection()
            result = migrate.apply_migrations(connection, [migration])
            self.assertEqual((1,), result.applied_now)
            self.assertEqual((migration.name, migration.sha256), connection.ledger[1])
            self.assertEqual(1, len(connection.effects))
            self.assertEqual(1, connection.unlocks)
            self.assertEqual(
                [
                    "lock",
                    "ledger_exists",
                    "select_applied",
                    "transaction_enter",
                    "migration_sql",
                    "ledger_insert",
                    "transaction_commit",
                    "unlock",
                ],
                connection.calls,
            )

    def test_matching_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__baseline.sql", "SELECT 1;\n")
            migrations = migrate.discover_migrations(directory)
            connection = FakeConnection()
            first = migrate.apply_migrations(connection, migrations)
            second = migrate.apply_migrations(connection, migrations)
            self.assertEqual((1,), first.applied_now)
            self.assertEqual((), second.applied_now)
            self.assertEqual(1, second.already_applied)
            self.assertEqual(1, len(connection.effects))
            self.assertEqual(2, connection.unlocks)
            self.assertEqual(
                ["lock", "ledger_exists", "select_applied", "unlock"],
                connection.calls[-4:],
            )

    def test_failed_migration_rolls_back_its_effect_and_ledger_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__first.sql", "SELECT 1;\n")
            write_migration(
                directory,
                "0002__fails.sql",
                "SELECT 2; -- RAISE_SYNTHETIC_FAILURE\n",
            )
            connection = FakeConnection()
            with self.assertRaisesRegex(migrate.MigrationExecutionError, "rolled back"):
                migrate.apply_migrations(
                    connection, migrate.discover_migrations(directory)
                )
            self.assertIn(1, connection.ledger)
            self.assertNotIn(2, connection.ledger)
            self.assertEqual(["SELECT 1;\n"], connection.effects)
            self.assertEqual(1, connection.rollbacks)
            self.assertEqual(1, connection.unlocks)
            self.assertEqual(
                ["transaction_enter", "migration_sql", "transaction_rollback", "unlock"],
                connection.calls[-4:],
            )

    def test_unexpected_ledger_read_failure_releases_lock(self) -> None:
        connection = FakeConnection(fail_on_applied_read=True)
        with self.assertRaisesRegex(RuntimeError, "synthetic applied-migration read failure"):
            migrate.apply_migrations(connection, [])
        self.assertEqual(1, connection.unlocks)
        self.assertEqual(
            ["lock", "ledger_exists", "select_applied", "unlock"],
            connection.calls,
        )

    def test_advisory_lock_excludes_concurrent_runner(self) -> None:
        connection = FakeConnection(lock_available=False)
        with self.assertRaises(migrate.MigrationLockUnavailable):
            migrate.apply_migrations(connection, [])
        self.assertEqual(1, connection.lock_attempts)
        self.assertEqual(0, connection.transaction_entries)
        self.assertEqual(0, connection.unlocks)

    def test_missing_migration_configuration_fails_before_connect(self) -> None:
        called = False

        @contextmanager
        def connection_factory(database_url: str) -> Iterator[FakeConnection]:
            nonlocal called
            called = True
            del database_url
            yield FakeConnection()

        with self.assertRaises(migrate.MigrationConfigurationError):
            migrate.run_from_environment({}, connection_factory=connection_factory)
        self.assertFalse(called)

    def test_database_url_is_not_used_as_fallback(self) -> None:
        with self.assertRaisesRegex(
            migrate.MigrationConfigurationError, "DATABASE_URL is intentionally ignored"
        ):
            migrate.migration_database_url(
                {"DATABASE_URL": "postgresql://ordinary-runtime-credential"}
            )

    def test_connection_failure_does_not_echo_connection_configuration(self) -> None:
        connection_value = "postgresql://migration-user:sensitive-value@localhost/database"

        @contextmanager
        def failing_factory(database_url: str) -> Iterator[FakeConnection]:
            raise RuntimeError(f"synthetic driver failure for {database_url}")
            yield FakeConnection()  # pragma: no cover

        with self.assertRaises(migrate.MigrationExecutionError) as caught:
            migrate.run_migrations(
                connection_value,
                connection_factory=failing_factory,
            )
        self.assertNotIn("sensitive-value", str(caught.exception))

    def test_offline_inspection_needs_no_database_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(directory, "0001__baseline.sql", "SELECT 1;\n")
            before_psycopg = sys.modules.get("psycopg")
            migrations = migrate.inspect_migrations(directory)
            self.assertEqual(["0001__baseline"], [item.identifier for item in migrations])
            self.assertIs(before_psycopg, sys.modules.get("psycopg"))

    def test_repository_baseline_marker_does_not_mutate_business_schema(self) -> None:
        baseline = (
            POSTGRES_INFRA
            / "migrations"
            / "0001__validate_bootstrap_prerequisites.sql"
        ).read_text()
        executable = "\n".join(
            line for line in baseline.splitlines() if not line.lstrip().startswith("--")
        )
        self.assertNotRegex(
            executable.upper(),
            r"\b(CREATE|ALTER|DROP|TRUNCATE|INSERT|UPDATE|DELETE)\b",
        )
        self.assertIn("to_regclass", executable)
        self.assertIn("M0-R01.2", baseline)
        self.assertIn(
            "M0-R01.1 does not authorize recording this baseline on an unmanaged database.",
            baseline,
        )


class AdoptionRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = migrate.prepare_adoption()

    def unmanaged(
        self,
        **kwargs: Any,
    ) -> FakeConnection:
        return FakeConnection(
            ledger_present=False,
            migration_state=(False, False, False, False, False, False),
            **kwargs,
        )

    def assert_repeat_guard_rejection_preserves_state(
        self,
        connection: FakeConnection,
        expected_exception: type[BaseException] = migrate.MigrationAdoptionError,
    ) -> None:
        before = connection.persistent_snapshot()
        transactions_before = connection.transaction_entries
        commits_before = connection.commits
        unlocks_before = connection.unlocks
        with self.assertRaises(expected_exception):
            migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        self.assertEqual(before, connection.persistent_snapshot())
        self.assertEqual(transactions_before, connection.transaction_entries)
        self.assertEqual(commits_before, connection.commits)
        self.assertEqual(unlocks_before + 1, connection.unlocks)
        self.assertEqual("unlock", connection.calls[-1])

    def test_matching_init_sql_and_baseline_permit_local_adoption_plan(self) -> None:
        self.assertEqual(
            bootstrap_contract.EXPECTED_INIT_SQL_SHA256,
            self.plan.init_sql_sha256,
        )
        self.assertEqual(1, self.plan.baseline.version)
        self.assertEqual(
            bootstrap_contract.EXPECTED_BASELINE_SHA256,
            self.plan.baseline.sha256,
        )

    def test_modified_init_sql_fails_before_connecting(self) -> None:
        called = False
        with tempfile.TemporaryDirectory() as temporary:
            changed_init = Path(temporary) / "init.sql"
            changed_init.write_bytes(migrate.DEFAULT_INIT_SQL.read_bytes() + b"\n-- drift\n")

            @contextmanager
            def factory(database_url: str) -> Iterator[FakeConnection]:
                nonlocal called
                called = True
                del database_url
                yield self.unmanaged()

            with self.assertRaisesRegex(migrate.MigrationDefinitionError, "accepted SHA-256"):
                migrate.run_adoption(
                    "postgresql://synthetic",
                    "smartcoat_rd",
                    init_sql_path=changed_init,
                    connection_factory=factory,
                )
        self.assertFalse(called)

    def test_missing_init_sql_fails_before_connecting(self) -> None:
        called = False

        @contextmanager
        def factory(database_url: str) -> Iterator[FakeConnection]:
            nonlocal called
            called = True
            del database_url
            yield self.unmanaged()

        with self.assertRaisesRegex(migrate.MigrationDefinitionError, "cannot be read"):
            migrate.run_adoption(
                "postgresql://synthetic",
                "smartcoat_rd",
                init_sql_path=Path("/synthetic/missing/init.sql"),
                connection_factory=factory,
            )
        self.assertFalse(called)

    def test_baseline_checksum_drift_fails_before_connecting(self) -> None:
        called = False
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration(
                directory,
                "0001__validate_bootstrap_prerequisites.sql",
                "SELECT 'changed';\n",
            )

            @contextmanager
            def factory(database_url: str) -> Iterator[FakeConnection]:
                nonlocal called
                called = True
                del database_url
                yield self.unmanaged()

            with self.assertRaisesRegex(migrate.MigrationDefinitionError, "accepted name and checksum"):
                migrate.run_adoption(
                    "postgresql://synthetic",
                    "smartcoat_rd",
                    directory=directory,
                    connection_factory=factory,
                )
        self.assertFalse(called)

    def test_invalid_utf8_still_fails_before_connecting(self) -> None:
        called = False
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_migration_bytes(
                directory,
                "0001__validate_bootstrap_prerequisites.sql",
                self.plan.baseline.content,
            )
            write_migration_bytes(directory, "0002__invalid_utf8.sql", b"SELECT '\xff';\n")

            @contextmanager
            def factory(database_url: str) -> Iterator[FakeConnection]:
                nonlocal called
                called = True
                del database_url
                yield self.unmanaged()

            with self.assertRaisesRegex(migrate.MigrationDefinitionError, "valid UTF-8"):
                migrate.run_adoption(
                    "postgresql://synthetic",
                    "smartcoat_rd",
                    directory=directory,
                    connection_factory=factory,
                )
        self.assertFalse(called)

    def test_exact_bootstrap_is_recognized_and_adopted_atomically(self) -> None:
        connection = self.unmanaged()
        before_rows = deepcopy(connection.business_rows)

        result = migrate.adopt_database(connection, self.plan, "smartcoat_rd")

        self.assertEqual("ADOPTED", result.status)
        self.assertTrue(result.evidence_inserted)
        self.assertEqual(
            (self.plan.baseline.name, self.plan.baseline.sha256),
            connection.ledger[1],
        )
        self.assertEqual(1, len(connection.evidence))
        evidence = connection.evidence[0]
        self.assertEqual(migrate.ADOPTION_ACTION, evidence[0])
        self.assertEqual(("smartcoat_rd", 4242, "migration_admin", "17.6"), evidence[1:5])
        self.assertIsNotNone(evidence[5])
        self.assertEqual(
            bootstrap_contract.EXPECTED_STRUCTURAL_FINGERPRINT,
            evidence[6],
        )
        self.assertEqual(evidence[6], evidence[7])
        self.assertEqual(bootstrap_contract.EXPECTED_INIT_SQL_SHA256, evidence[8])
        self.assertEqual((1, self.plan.baseline.name, self.plan.baseline.sha256), evidence[9:12])
        self.assertEqual(bootstrap_contract.CONTRACT_VERSION, evidence[12])
        self.assertEqual(bootstrap_contract.COMPARED_CATEGORIES_JSON, evidence[13])
        self.assertEqual(migrate.ADOPTION_AUTHORIZATION, evidence[14])
        self.assertNotIn("postgresql://", repr(evidence))
        self.assertNotIn("password", repr(evidence).lower())
        self.assertEqual(before_rows, connection.business_rows)
        self.assertEqual(1, connection.commits)
        self.assertEqual(0, connection.rollbacks)
        self.assertEqual(1, connection.unlocks)
        self.assertLess(connection.calls.index("database_identity"), connection.calls.index("transaction_enter"))
        self.assertLess(connection.calls.index("lock_bootstrap_tables"), connection.calls.index("catalog:schemas"))
        self.assertLess(connection.calls.index("catalog:column_privileges"), connection.calls.index("create_schema"))
        self.assertLess(connection.calls.index("baseline_sql"), connection.calls.index("ledger_insert"))
        self.assertLess(connection.calls.index("ledger_insert"), connection.calls.index("evidence_insert"))

    def test_same_tables_with_changed_column_type_are_rejected(self) -> None:
        catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
        row = list(catalog["columns"][0])
        row[4] = "integer"
        catalog["columns"][0] = tuple(row)
        connection = self.unmanaged(catalog=catalog)
        with self.assertRaisesRegex(migrate.MigrationRecognitionError, "columns"):
            migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        self.assertEqual({}, connection.ledger)
        self.assertEqual(1, connection.rollbacks)
        self.assertEqual(1, connection.unlocks)

    def test_same_tables_with_changed_nullability_are_rejected(self) -> None:
        catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
        row = list(catalog["columns"][1])
        row[5] = not row[5]
        catalog["columns"][1] = tuple(row)
        with self.assertRaisesRegex(migrate.MigrationRecognitionError, "columns"):
            migrate.adopt_database(self.unmanaged(catalog=catalog), self.plan, "smartcoat_rd")

    def test_missing_or_altered_constraint_is_rejected(self) -> None:
        for category in ("key_constraints", "check_constraints"):
            with self.subTest(category=category):
                catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
                catalog[category] = catalog[category][1:]
                with self.assertRaisesRegex(migrate.MigrationRecognitionError, category):
                    migrate.adopt_database(
                        self.unmanaged(catalog=catalog), self.plan, "smartcoat_rd"
                    )

    def test_trigger_drift_variants_are_rejected(self) -> None:
        variants: dict[str, Any] = {}
        triggers = deepcopy(bootstrap_contract.EXPECTED_CATALOG["triggers"])
        variants["missing"] = triggers[1:]
        disabled = [list(row) for row in triggers]
        disabled[0][3] = "D"
        variants["disabled"] = [tuple(row) for row in disabled]
        wrong_table = [list(row) for row in triggers]
        wrong_table[0][1] = "uploads"
        variants["wrong_table"] = [tuple(row) for row in wrong_table]
        modified = [list(row) for row in triggers]
        modified[0][11] = "different_function"
        variants["modified"] = [tuple(row) for row in modified]
        for name, rows in variants.items():
            with self.subTest(name=name):
                catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
                catalog["triggers"] = rows
                with self.assertRaisesRegex(migrate.MigrationRecognitionError, "triggers"):
                    migrate.adopt_database(
                        self.unmanaged(catalog=catalog), self.plan, "smartcoat_rd"
                    )

    def test_role_or_grant_drift_is_rejected(self) -> None:
        for category in ("roles", "schema_privileges", "table_privileges", "column_privileges"):
            with self.subTest(category=category):
                catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
                catalog[category] = catalog[category][1:]
                with self.assertRaisesRegex(migrate.MigrationRecognitionError, category):
                    migrate.adopt_database(
                        self.unmanaged(catalog=catalog), self.plan, "smartcoat_rd"
                    )

    def test_enum_or_index_drift_is_rejected(self) -> None:
        cases = {
            "enums": [("public", "unexpected_enum", 1.0, "VALUE")],
            "indexes": bootstrap_contract.EXPECTED_CATALOG["indexes"][1:],
        }
        for category, rows in cases.items():
            with self.subTest(category=category):
                catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
                catalog[category] = rows
                with self.assertRaisesRegex(migrate.MigrationRecognitionError, category):
                    migrate.adopt_database(
                        self.unmanaged(catalog=catalog), self.plan, "smartcoat_rd"
                    )

    def test_missing_catalog_visibility_fails_closed(self) -> None:
        catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
        del catalog["trigger_functions"]
        connection = self.unmanaged(catalog=catalog)
        with self.assertRaisesRegex(migrate.MigrationRecognitionError, "trigger_functions"):
            migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        self.assertEqual(1, connection.rollbacks)
        self.assertEqual(1, connection.unlocks)

    def test_catalog_normalization_sorts_rows_and_collapses_sql_whitespace(self) -> None:
        catalog = deepcopy(bootstrap_contract.EXPECTED_CATALOG)
        catalog["triggers"] = list(reversed(catalog["triggers"]))
        function = list(catalog["trigger_functions"][0])
        function[-1] = "\n BEGIN   RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;\nEND;\n"
        catalog["trigger_functions"] = [tuple(function)]
        self.assertEqual(
            bootstrap_contract.EXPECTED_NORMALIZED_CATALOG,
            bootstrap_contract.normalize_catalog(catalog),
        )

    def test_mismatched_database_name_fails_before_persistent_mutation(self) -> None:
        connection = self.unmanaged()
        before = connection.persistent_snapshot()
        with self.assertRaisesRegex(migrate.MigrationAdoptionError, "explicitly expected"):
            migrate.adopt_database(connection, self.plan, "wrong_database")
        self.assertEqual(before, connection.persistent_snapshot())
        self.assertEqual(["lock", "database_identity", "unlock"], connection.calls)

    def test_adopt_cli_requires_expected_database_name_and_has_no_bypass(self) -> None:
        parser = migrate.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["adopt"])
        for forbidden in ("--force", "--skip-fingerprint", "--accept-any-schema"):
            with self.subTest(forbidden=forbidden), self.assertRaises(SystemExit):
                parser.parse_args(["adopt", "smartcoat_rd", forbidden])
        help_text = parser.format_help()
        self.assertIn("adopt", help_text)
        self.assertNotIn("force", help_text)
        self.assertNotIn("skip-fingerprint", help_text)

    def test_adoption_requires_dedicated_environment_variable(self) -> None:
        called = False

        @contextmanager
        def factory(database_url: str) -> Iterator[FakeConnection]:
            nonlocal called
            called = True
            del database_url
            yield self.unmanaged()

        with self.assertRaisesRegex(migrate.MigrationConfigurationError, "DATABASE_URL is intentionally ignored"):
            migrate.run_adoption_from_environment(
                "smartcoat_rd",
                {"DATABASE_URL": "postgresql://ordinary-runtime"},
                connection_factory=factory,
            )
        self.assertFalse(called)

    def test_adoption_failure_points_roll_back_all_metadata_and_release_lock(self) -> None:
        for fail_at in (
            "create_schema",
            "create_ledger",
            "create_evidence_table",
            "create_metadata_function",
            "create_ledger_trigger",
            "create_adoption_trigger",
            "baseline_sql",
            "ledger_insert",
            "after_ledger_insert",
            "evidence_insert",
            "after_evidence_insert",
        ):
            with self.subTest(fail_at=fail_at):
                connection = self.unmanaged(fail_at=fail_at)
                before_rows = deepcopy(connection.business_rows)
                with self.assertRaises(migrate.MigrationAdoptionError):
                    migrate.adopt_database(connection, self.plan, "smartcoat_rd")
                self.assertFalse(connection.migration_schema_present)
                self.assertFalse(connection.ledger_present)
                self.assertFalse(connection.adoption_table_present)
                self.assertFalse(connection.metadata_function_present)
                self.assertFalse(connection.ledger_trigger_present)
                self.assertFalse(connection.adoption_trigger_present)
                self.assertIsNone(connection.guard_function)
                self.assertEqual([], connection.guard_triggers)
                self.assertEqual({}, connection.ledger)
                self.assertEqual([], connection.evidence)
                self.assertEqual([], connection.effects)
                self.assertEqual(before_rows, connection.business_rows)
                self.assertEqual(1, connection.rollbacks)
                self.assertEqual(1, connection.unlocks)

    def test_adoption_and_ledger_evidence_are_append_only(self) -> None:
        connection = self.unmanaged()
        migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        for statement in (
            "UPDATE smartcoat_migrations.adoption_decisions SET database_name = 'x'",
            "DELETE FROM smartcoat_migrations.adoption_decisions",
            "UPDATE smartcoat_migrations.applied_migrations SET name = 'x'",
            "DELETE FROM smartcoat_migrations.applied_migrations",
        ):
            with self.subTest(statement=statement), self.assertRaisesRegex(RuntimeError, "append-only"):
                connection.execute(statement)

    def test_repeated_correct_adoption_is_idempotent(self) -> None:
        connection = self.unmanaged()
        first = migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        call_boundary = len(connection.calls)
        second = migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        self.assertEqual("ADOPTED", first.status)
        self.assertEqual("ALREADY_ADOPTED", second.status)
        self.assertFalse(second.evidence_inserted)
        self.assertEqual(1, len(connection.evidence))
        self.assertEqual(1, len(connection.ledger))
        self.assertEqual(1, connection.transaction_entries)
        self.assertEqual(
            [
                "lock",
                "database_identity",
                "metadata_state",
                "select_guard_function",
                "select_guard_triggers",
                "select_applied",
                "select_evidence",
                "unlock",
            ],
            connection.calls[call_boundary:],
        )

    def test_repeat_rejects_guard_function_semantic_drift_without_mutation(self) -> None:
        variants = {
            "modified_body": (7, "BEGIN RETURN NEW; END;"),
            "wrong_return_type": (3, "void"),
            "wrong_language": (4, "sql"),
            "security_definer": (5, True),
            "wrong_schema": (0, "public"),
            "unexpected_arguments": (2, "integer"),
        }
        for name, (index, value) in variants.items():
            with self.subTest(name=name):
                connection = self.unmanaged()
                migrate.adopt_database(connection, self.plan, "smartcoat_rd")
                assert connection.guard_function is not None
                guard_function = list(connection.guard_function)
                guard_function[index] = value
                connection.guard_function = tuple(guard_function)

                self.assert_repeat_guard_rejection_preserves_state(connection)

    def test_repeat_rejects_guard_trigger_semantic_drift_without_mutation(self) -> None:
        variants = {
            "wrong_trigger_name": (2, "different_append_only_guard"),
            "disabled": (3, "D"),
            "internal": (4, True),
            "wrong_function_binding": (6, "different_guard_function"),
            "statement_level": (8, False),
            "wrong_timing": (9, False),
            "missing_delete": (11, False),
            "missing_update": (12, False),
            "wrong_target": (1, "different_metadata_table"),
        }
        for name, (index, value) in variants.items():
            with self.subTest(name=name):
                connection = self.unmanaged()
                migrate.adopt_database(connection, self.plan, "smartcoat_rd")
                trigger = list(connection.guard_triggers[0])
                trigger[index] = value
                connection.guard_triggers[0] = tuple(trigger)

                self.assert_repeat_guard_rejection_preserves_state(connection)

    def test_repeat_fails_closed_when_guard_contract_is_unreadable(self) -> None:
        connection = self.unmanaged()
        migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        connection.fail_at = "guard_function_contract"

        self.assert_repeat_guard_rejection_preserves_state(
            connection,
            migrate.MigrationRecognitionError,
        )

    def test_repeat_uses_evidence_and_prefix_not_frozen_application_schema(self) -> None:
        connection = self.unmanaged()
        migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        future = migrate.Migration(
            2,
            "future_change",
            Path("0002__future_change.sql"),
            b"SELECT 2;\n",
            hashlib.sha256(b"SELECT 2;\n").hexdigest(),
        )
        future_plan = migrate.AdoptionPlan(
            self.plan.init_sql_sha256,
            self.plan.migrations + (future,),
            self.plan.baseline,
        )
        connection.ledger[2] = (future.name, future.sha256)
        connection.catalog["columns"] = []  # represents a legitimate later shape
        call_boundary = len(connection.calls)

        result = migrate.adopt_database(connection, future_plan, "smartcoat_rd")

        self.assertEqual("ALREADY_ADOPTED", result.status)
        self.assertFalse(
            any(call.startswith("catalog:") for call in connection.calls[call_boundary:])
        )

    def test_partial_migration_owned_state_fails_closed(self) -> None:
        connection = FakeConnection(
            ledger_present=False,
            migration_state=(True, False, False, False, False, False),
        )
        before = connection.persistent_snapshot()
        with self.assertRaises(migrate.MigrationPartialStateError):
            migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        self.assertEqual(before, connection.persistent_snapshot())
        self.assertEqual(
            ["lock", "database_identity", "metadata_state", "unlock"],
            connection.calls,
        )

    def test_lock_contention_fails_without_mutation_or_extra_unlock(self) -> None:
        connection = self.unmanaged(lock_available=False)
        before = connection.persistent_snapshot()
        with self.assertRaises(migrate.MigrationLockUnavailable):
            migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        self.assertEqual(before, connection.persistent_snapshot())
        self.assertEqual(["lock"], connection.calls)
        self.assertEqual(0, connection.unlocks)

    def test_ordinary_apply_recognizes_database_after_adoption(self) -> None:
        connection = self.unmanaged()
        migrate.adopt_database(connection, self.plan, "smartcoat_rd")
        result = migrate.apply_migrations(
            connection,
            self.plan.migrations,
        )
        self.assertEqual(1, result.already_applied)
        self.assertEqual(
            tuple(migration.version for migration in self.plan.migrations[1:]),
            result.applied_now,
        )
        self.assertEqual(1, len(connection.evidence))
        self.assertEqual(2, connection.unlocks)

    def test_already_adopted_evidence_or_ledger_drift_fails_closed(self) -> None:
        for drift in ("evidence", "ledger"):
            with self.subTest(drift=drift):
                connection = self.unmanaged()
                migrate.adopt_database(connection, self.plan, "smartcoat_rd")
                if drift == "evidence":
                    row = list(connection.evidence[0])
                    row[8] = "0" * 64
                    connection.evidence[0] = tuple(row)
                else:
                    connection.ledger[1] = (self.plan.baseline.name, "0" * 64)
                with self.assertRaises((migrate.MigrationAdoptionError, migrate.MigrationDriftError)):
                    migrate.adopt_database(connection, self.plan, "smartcoat_rd")
                self.assertEqual(2, connection.unlocks)

    def test_recognition_queries_never_reference_business_rows(self) -> None:
        forbidden = ("SELECT * FROM users", "SELECT * FROM uploads", "COUNT(")
        for query in bootstrap_contract.CATALOG_QUERIES.values():
            upper = query.upper()
            for marker in forbidden:
                self.assertNotIn(marker, upper)

    def test_adoption_ddl_contains_no_repair_or_bypass_constructs(self) -> None:
        ddl = "\n".join((
            migrate.CREATE_MIGRATION_SCHEMA_SQL,
            migrate.CREATE_LEDGER_SQL,
            migrate.CREATE_ADOPTION_EVIDENCE_SQL,
            migrate.CREATE_METADATA_GUARD_FUNCTION_SQL,
            migrate.CREATE_LEDGER_GUARD_TRIGGER_SQL,
            migrate.CREATE_ADOPTION_GUARD_TRIGGER_SQL,
        )).upper()
        self.assertNotIn("IF NOT EXISTS", ddl)
        self.assertNotIn("CREATE OR REPLACE", ddl)
        self.assertNotRegex(ddl, r"\bDROP\b")
        self.assertIn("BEFORE UPDATE OR DELETE", ddl)


if __name__ == "__main__":
    unittest.main()
