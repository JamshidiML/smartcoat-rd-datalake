#!/usr/bin/env python3
"""Opt-in M0-R03 fresh/upgraded PostgreSQL transition acceptance.

The live mode reuses the accepted M0-R01.4.1 disposable infrastructure and
requires an explicit flag::

    python3 infra/postgres/tests/live_state_transition_acceptance.py \
        --confirm-disposable-synthetic-transition-run
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
ACCEPTED_HARNESS = (
    ROOT / "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
)
ACCEPTED_HARNESS_SHA256 = (
    "4d7fbe8d33d36b6ff50161f4374cf16477667903253b790cbc37cb3e54707cfd"
)
BASELINE_MIGRATION = (
    ROOT / "infra/postgres/migrations/0001__validate_bootstrap_prerequisites.sql"
)
TRANSITION_MIGRATION = (
    ROOT / "infra/postgres/migrations/0003__enforce_upload_state_transitions.sql"
)
MIGRATIONS = ROOT / "infra/postgres/migrations"
RECOVERY_MIGRATION = MIGRATIONS / "0009__add_operator_ocr_retry_transition.sql"
EXPECTED_MIGRATION_VERSIONS = tuple(range(1, 10))

RESULT_PASS = "PASS_M0_R03"
RESULT_PRODUCT_FAILURE = "FAIL_PRODUCT_CONTRACT"
RESULT_HARNESS_FAILURE = "FAIL_VERIFICATION_HARNESS"
RESULT_ISOLATION_BLOCKED = "BLOCKED_ISOLATION"
RESULT_ENVIRONMENT_BLOCKED = "BLOCKED_ENVIRONMENT"
RESULT_IMPLEMENTATION_BLOCKED = "BLOCKED_IMPLEMENTATION_BOUNDARY"

STATES = (
    "RECEIVED",
    "BRONZE_COMMITTED",
    "OCR_QUEUED",
    "OCR_COMPLETED",
    "SILVER_DRAFT_READY",
    "UNDER_HUMAN_REVIEW",
    "VERIFIED",
    "REJECTED",
    "OCR_FAILED",
    "REVIEW_REJECTED",
)
LEGAL_TRANSITIONS = (
    ("RECEIVED", "BRONZE_COMMITTED", "commit_verified_bronze_pair"),
    ("RECEIVED", "REJECTED", "reject_received_upload"),
    ("BRONZE_COMMITTED", "OCR_QUEUED", "queue_ocr"),
    ("OCR_QUEUED", "OCR_COMPLETED", "complete_ocr"),
    ("OCR_QUEUED", "OCR_FAILED", "fail_ocr"),
    ("OCR_FAILED", "OCR_QUEUED", "operator_retry_failed_ocr"),
    ("OCR_COMPLETED", "SILVER_DRAFT_READY", "publish_unverified_draft"),
    ("SILVER_DRAFT_READY", "UNDER_HUMAN_REVIEW", "begin_human_review"),
    ("UNDER_HUMAN_REVIEW", "VERIFIED", "verify_reviewed_draft"),
    ("UNDER_HUMAN_REVIEW", "REVIEW_REJECTED", "reject_reviewed_draft"),
    ("VERIFIED", "UNDER_HUMAN_REVIEW", "begin_verified_revision_review"),
)


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_accepted_harness() -> Any:
    if not ACCEPTED_HARNESS.is_file():
        raise RuntimeError("accepted lifecycle harness is missing")
    if sha256_path(ACCEPTED_HARNESS) != ACCEPTED_HARNESS_SHA256:
        raise RuntimeError("accepted lifecycle harness hash changed")
    spec = importlib.util.spec_from_file_location(
        "m0r03_accepted_lifecycle", ACCEPTED_HARNESS
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("accepted lifecycle harness cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:
    accepted = load_accepted_harness()
except Exception as boundary_error:
    print(f"Implementation boundary blocked: {boundary_error}", file=sys.stderr)
    print(RESULT_IMPLEMENTATION_BLOCKED)
    raise SystemExit(4)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def state_array_sql() -> str:
    return "ARRAY[" + ",".join(sql_literal(state) for state in STATES) + "]::text[]"


def legal_values_sql() -> str:
    return ",\n".join(
        "(" + ",".join(sql_literal(item) for item in edge) + ")"
        for edge in LEGAL_TRANSITIONS
    )


class LiveStateTransitionAcceptance(accepted.LiveMigrationLifecycleAcceptance):
    """M0-R03 checks on one accepted disposable PostgreSQL project."""

    def __init__(self, volume_mode: str) -> None:
        super().__init__()
        if volume_mode not in {"fresh", "upgraded"}:
            raise ValueError("volume_mode must be fresh or upgraded")
        self.volume_mode = volume_mode
        self.migration_sources = tuple(
            MIGRATIONS / path.name
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]__*.sql"))
        )
        self.transition_sha256 = sha256_path(TRANSITION_MIGRATION)
        self.recovery_sha256 = sha256_path(RECOVERY_MIGRATION)
        self.ingestion_password = secrets.token_hex(24)
        self.ocr_password = secrets.token_hex(24)
        self.review_password = secrets.token_hex(24)
        self.backup_password = secrets.token_hex(24)
        self.hold_applier_access_key = f"m0r03-hold-{secrets.token_hex(8)}"
        self.hold_applier_secret_key = secrets.token_hex(24)
        self.hold_applier_call_token = secrets.token_urlsafe(48)
        self.ingestion_database_url = (
            f"postgresql://smartcoat_ingestion:{self.ingestion_password}"
            f"@postgres:5432/{self.database_name}"
        )
        self.ocr_database_url = (
            f"postgresql://smartcoat_ocr:{self.ocr_password}"
            f"@postgres:5432/{self.database_name}"
        )
        self.review_database_url = (
            f"postgresql://smartcoat_review:{self.review_password}"
            f"@postgres:5432/{self.database_name}"
        )
        self.secret_values.update(
            {
                self.ingestion_password,
                self.ocr_password,
                self.review_password,
                self.backup_password,
                self.ingestion_database_url,
                self.ocr_database_url,
                self.review_database_url,
                self.hold_applier_access_key,
                self.hold_applier_secret_key,
                self.hold_applier_call_token,
            }
        )
        self.evidence.update(
            {
                "ticket": "M0-R03",
                "volume_mode": volume_mode,
                "transition_migration_sha256": self.transition_sha256,
                "checks": [],
            }
        )

    def _write_synthetic_configuration(self) -> None:
        super()._write_synthetic_configuration()
        with self.environment_file.open("a", encoding="utf-8") as environment:
            environment.write(
                "POSTGRES_ROLE_ADMIN_URL=" + self.migration_database_url + "\n"
                "POSTGRES_INGESTION_PASSWORD=" + self.ingestion_password + "\n"
                "POSTGRES_OCR_PASSWORD=" + self.ocr_password + "\n"
                "POSTGRES_REVIEW_PASSWORD=" + self.review_password + "\n"
                "POSTGRES_BACKUP_PASSWORD=" + self.backup_password + "\n"
                "DATABASE_INGESTION_URL=" + self.ingestion_database_url + "\n"
                "DATABASE_OCR_URL=" + self.ocr_database_url + "\n"
                "DATABASE_REVIEW_URL=" + self.review_database_url + "\n"
                "MINIO_HOLD_APPLIER_ACCESS_KEY="
                + self.hold_applier_access_key
                + "\n"
                "MINIO_HOLD_APPLIER_SECRET_KEY="
                + self.hold_applier_secret_key
                + "\n"
                "LEGAL_HOLD_APPLIER_CALL_TOKEN="
                + self.hold_applier_call_token
                + "\n"
            )
        self.environment_file.chmod(0o600)

    def _prepare_baseline_fixture(self) -> None:
        self._require(
            len(self.migration_sources) == len(EXPECTED_MIGRATION_VERSIONS)
            and all(path.is_file() for path in self.migration_sources),
            "Required migration source is unavailable",
            accepted.EnvironmentBlocked,
        )
        baseline = BASELINE_MIGRATION.read_bytes()
        self._require(
            hashlib.sha256(baseline).hexdigest()
            == accepted.EXPECTED_BASELINE_SHA256,
            "Accepted baseline migration checksum changed",
            accepted.ProductContractFailure,
        )
        self.migration_fixture_directory.mkdir(mode=0o700)
        for source in self.migration_sources:
            fixture = self.migration_fixture_directory / source.name
            fixture.write_bytes(source.read_bytes())
            fixture.chmod(0o400)
        self._assert_migration_fixture_host_ownership(expect_pending=False)

    def _assert_migration_fixture_host_ownership(
        self, *, expect_pending: bool
    ) -> None:
        del expect_pending
        directory = self.migration_fixture_directory.resolve()
        self._require(
            directory.parent == self.temporary_directory.resolve()
            and directory.stat().st_uid == os.getuid()
            and stat.S_IMODE(directory.stat().st_mode) == 0o700,
            "Transition fixture directory ownership or mode is unsafe",
            accepted.IsolationBlocked,
        )
        expected_files = {
            (self.migration_fixture_directory / source.name).resolve()
            for source in self.migration_sources
        }
        self._require(
            {path.resolve() for path in directory.iterdir()} == expected_files,
            "Transition fixture directory contains an unexpected file",
            accepted.IsolationBlocked,
        )
        for path in expected_files:
            metadata = path.stat()
            self._require(
                metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o400,
                "Transition fixture ownership or mode is unsafe",
                accepted.IsolationBlocked,
            )
        for source in self.migration_sources:
            self._require(
                sha256_path(self.migration_fixture_directory / source.name)
                == sha256_path(source),
                "Migration fixture differs from the repository source",
                accepted.ProductContractFailure,
            )

    def _insert_upgraded_fixture(self) -> list[dict[str, Any]]:
        state_values = ",\n".join(
            f"({index}, {sql_literal(state)})"
            for index, state in enumerate(STATES, start=1)
        )
        self._psql_success(
            "insert_upgraded_volume_fixture",
            f"""
                INSERT INTO public.users (
                    user_id, display_name, email, role, active, created_at_utc
                ) VALUES (
                    'usr_m0_r03_synthetic', 'M0 R03 Synthetic User',
                    'm0-r03-synthetic@example.invalid', 'UPLOADER', true,
                    TIMESTAMPTZ '2026-08-27T00:00:00Z'
                );

                INSERT INTO public.uploads (
                    ingestion_id, department, uploader_user_id,
                    uploader_display_name, uploaded_at_utc, original_filename,
                    stored_object_key, manifest_object_key, detected_mime_type,
                    declared_file_type, document_category, context_note,
                    capture_date, byte_size, source_sha256,
                    duplicate_of_ingestion_id, source_channel, state
                )
                SELECT
                    (
                        substr(md5('m0r03-upgraded-' || fixture.ordinal), 1, 8)
                        || '-' || substr(md5('m0r03-upgraded-' || fixture.ordinal), 9, 4)
                        || '-' || substr(md5('m0r03-upgraded-' || fixture.ordinal), 13, 4)
                        || '-' || substr(md5('m0r03-upgraded-' || fixture.ordinal), 17, 4)
                        || '-' || substr(md5('m0r03-upgraded-' || fixture.ordinal), 21, 12)
                    )::uuid,
                    'RND', 'usr_m0_r03_synthetic', 'M0 R03 Synthetic User',
                    TIMESTAMPTZ '2026-08-27T00:00:00Z',
                    'upgraded-' || fixture.ordinal || '.jpg',
                    'm0r03/upgraded/' || fixture.ordinal || '/original.jpg',
                    'm0r03/upgraded/' || fixture.ordinal || '/manifest.json',
                    'image/jpeg', 'PHOTO', 'OTHER',
                    'Synthetic upgraded-volume transition fixture.', NULL, 1,
                    repeat(to_hex(fixture.ordinal % 16), 64), NULL,
                    'WEB_UPLOAD', fixture.state
                FROM (VALUES {state_values}) AS fixture(ordinal, state);
            """,
        )
        return self._psql_rows(
            "upgraded_volume_fixture_before",
            """
                SELECT ingestion_id::text AS ingestion_id, state
                FROM public.uploads
                ORDER BY state, ingestion_id
            """,
        )

    def _ensure_synthetic_user(self) -> None:
        self._psql_success(
            "ensure_transition_test_user",
            """
                INSERT INTO public.users (
                    user_id, display_name, email, role, active, created_at_utc
                ) VALUES (
                    'usr_m0_r03_synthetic', 'M0 R03 Synthetic User',
                    'm0-r03-synthetic@example.invalid', 'UPLOADER', true,
                    TIMESTAMPTZ '2026-08-27T00:00:00Z'
                ) ON CONFLICT (user_id) DO NOTHING
            """,
        )

    def _assert_installed_contract(self) -> None:
        edges = self._psql_rows(
            "installed_transition_edges",
            """
                SELECT previous_state, next_state, transition_name
                FROM smartcoat_state.legal_upload_transitions
                ORDER BY previous_state, next_state
            """,
        )
        expected = [
            {
                "previous_state": previous,
                "next_state": following,
                "transition_name": name,
            }
            for previous, following, name in sorted(LEGAL_TRANSITIONS)
        ]
        self._require(edges == expected, "Installed transition graph differs")
        triggers = self._psql_rows(
            "installed_transition_triggers",
            """
                SELECT n.nspname AS table_schema, c.relname AS table_name,
                       t.tgname AS trigger_name, t.tgenabled AS enabled,
                       pn.nspname AS function_schema, p.proname AS function_name,
                       p.prosecdef AS security_definer,
                       pg_get_triggerdef(t.oid, false) AS definition
                FROM pg_trigger AS t
                JOIN pg_class AS c ON c.oid = t.tgrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_proc AS p ON p.oid = t.tgfoid
                JOIN pg_namespace AS pn ON pn.oid = p.pronamespace
                WHERE (n.nspname, c.relname, t.tgname) IN (
                    ('public', 'uploads', 'uploads_initial_state_guard'),
                    ('public', 'uploads', 'uploads_state_transition_guard'),
                    ('smartcoat_state', 'legal_upload_transitions',
                     'legal_upload_transitions_immutable')
                )
                ORDER BY table_schema, table_name, trigger_name
            """,
        )
        self._require(
            len(triggers) == 3
            and all(row["enabled"] == "O" for row in triggers)
            and sum(bool(row["security_definer"]) for row in triggers) == 2,
            "Transition trigger catalog contract is incomplete",
        )
        upload_triggers = [row for row in triggers if row["table_name"] == "uploads"]
        self._require(
            len(upload_triggers) == 2
            and all(
                row["function_schema"] == "smartcoat_state"
                and row["function_name"] == "enforce_upload_state_transition"
                and row["security_definer"] is True
                for row in upload_triggers
            ),
            "Upload state guards do not use the authoritative function",
        )
        runtime_access = self._psql_success(
            "runtime_transition_contract_privileges",
            """
                SELECT concat_ws('|',
                    has_schema_privilege('smartcoat_app', 'smartcoat_state', 'USAGE'),
                    has_table_privilege('smartcoat_app',
                        'smartcoat_state.legal_upload_transitions', 'SELECT'),
                    has_table_privilege('smartcoat_app',
                        'smartcoat_state.legal_upload_transitions', 'INSERT'),
                    has_table_privilege('smartcoat_app',
                        'smartcoat_state.legal_upload_transitions', 'UPDATE'),
                    has_table_privilege('smartcoat_app',
                        'smartcoat_state.legal_upload_transitions', 'DELETE'))
            """,
        )
        self._require(
            runtime_access == "f|f|f|f|f",
            "Runtime role can inspect or mutate the authoritative graph",
        )
        self.evidence["checks"].append("catalog_and_graph_exact")

    def _assert_initial_and_exhaustive_direct_sql(self) -> None:
        state_values = ",".join(f"({sql_literal(state)})" for state in STATES)
        edge_values = legal_values_sql()
        script = f"""
            BEGIN;
            CREATE TEMP TABLE expected_m0r03_edges (
                previous_state text NOT NULL,
                next_state text NOT NULL,
                transition_name text NOT NULL,
                PRIMARY KEY (previous_state, next_state)
            ) ON COMMIT DROP;
            INSERT INTO expected_m0r03_edges VALUES {edge_values};
            GRANT SELECT ON expected_m0r03_edges TO smartcoat_app;

            CREATE TEMP TABLE m0r03_observations (
                observation text PRIMARY KEY,
                observed_count integer NOT NULL
            ) ON COMMIT DROP;
            GRANT INSERT, SELECT ON m0r03_observations TO smartcoat_app;

            DO $initial$
            DECLARE
                candidate text;
                denied integer := 0;
                accepted integer := 0;
                constraint_seen text;
                fixture_id uuid;
            BEGIN
                FOREACH candidate IN ARRAY {state_array_sql()} LOOP
                    fixture_id := (
                        substr(md5('m0r03-initial-' || candidate),1,8) || '-'
                        || substr(md5('m0r03-initial-' || candidate),9,4) || '-'
                        || substr(md5('m0r03-initial-' || candidate),13,4) || '-'
                        || substr(md5('m0r03-initial-' || candidate),17,4) || '-'
                        || substr(md5('m0r03-initial-' || candidate),21,12)
                    )::uuid;
                    BEGIN
                        INSERT INTO public.uploads (
                            ingestion_id, department, uploader_user_id,
                            uploader_display_name, uploaded_at_utc,
                            original_filename, stored_object_key,
                            manifest_object_key, detected_mime_type,
                            declared_file_type, document_category, context_note,
                            capture_date, byte_size, source_sha256,
                            duplicate_of_ingestion_id, source_channel, state
                        ) VALUES (
                            fixture_id, 'RND', 'usr_m0_r03_synthetic',
                            'M0 R03 Synthetic User',
                            TIMESTAMPTZ '2026-08-27T00:00:00Z',
                            'initial-' || candidate || '.jpg',
                            'm0r03/initial/' || candidate || '/original.jpg',
                            'm0r03/initial/' || candidate || '/manifest.json',
                            'image/jpeg', 'PHOTO', 'OTHER',
                            'Synthetic initial-state enforcement fixture.', NULL,
                            1, repeat('a', 64), NULL, 'WEB_UPLOAD', candidate
                        );
                        IF candidate <> 'RECEIVED' THEN
                            RAISE EXCEPTION 'illegal initial state was accepted: %', candidate;
                        END IF;
                        accepted := accepted + 1;
                    EXCEPTION WHEN check_violation THEN
                        GET STACKED DIAGNOSTICS constraint_seen = CONSTRAINT_NAME;
                        IF candidate = 'RECEIVED'
                           OR constraint_seen <> 'uploads_legal_initial_state' THEN
                            RAISE;
                        END IF;
                        denied := denied + 1;
                    END;
                END LOOP;
                INSERT INTO m0r03_observations VALUES
                    ('initial_accepted', accepted), ('initial_denied', denied);
            END
            $initial$;

            CREATE TEMP TABLE m0r03_pairs (
                ingestion_id uuid PRIMARY KEY,
                previous_state text NOT NULL,
                next_state text NOT NULL,
                expected_legal boolean NOT NULL
            ) ON COMMIT DROP;
            INSERT INTO m0r03_pairs
            SELECT
                (
                    substr(md5('m0r03-pair-' || source.state || '-' || target.state),1,8)
                    || '-' || substr(md5('m0r03-pair-' || source.state || '-' || target.state),9,4)
                    || '-' || substr(md5('m0r03-pair-' || source.state || '-' || target.state),13,4)
                    || '-' || substr(md5('m0r03-pair-' || source.state || '-' || target.state),17,4)
                    || '-' || substr(md5('m0r03-pair-' || source.state || '-' || target.state),21,12)
                )::uuid,
                source.state,
                target.state,
                EXISTS (
                    SELECT 1 FROM expected_m0r03_edges AS legal
                    WHERE legal.previous_state = source.state
                      AND legal.next_state = target.state
                )
            FROM (VALUES {state_values}) AS source(state)
            CROSS JOIN (VALUES {state_values}) AS target(state)
            WHERE source.state <> target.state;
            GRANT SELECT ON m0r03_pairs TO smartcoat_app;

            -- Isolate the upload-state graph for the exhaustive 90-pair matrix.
            -- The Bronze-pair guard is independently exercised by the recovery
            -- scenario below and is restored before this transaction commits.
            ALTER TABLE public.uploads
                DISABLE TRIGGER uploads_require_bronze_pair_for_success;

            SET session_replication_role = replica;
            INSERT INTO public.uploads (
                ingestion_id, department, uploader_user_id,
                uploader_display_name, uploaded_at_utc, original_filename,
                stored_object_key, manifest_object_key, detected_mime_type,
                declared_file_type, document_category, context_note,
                capture_date, byte_size, source_sha256,
                duplicate_of_ingestion_id, source_channel, state
            )
            SELECT
                candidate.ingestion_id,
                'RND', 'usr_m0_r03_synthetic', 'M0 R03 Synthetic User',
                TIMESTAMPTZ '2026-08-27T00:00:00Z',
                'pair-' || candidate.previous_state || '-' || candidate.next_state || '.jpg',
                'm0r03/pair/' || candidate.previous_state || '/' || candidate.next_state || '/original.jpg',
                'm0r03/pair/' || candidate.previous_state || '/' || candidate.next_state || '/manifest.json',
                'image/jpeg', 'PHOTO', 'OTHER',
                'Synthetic exhaustive transition fixture.', NULL, 1,
                repeat('b', 64), NULL, 'WEB_UPLOAD', candidate.previous_state
            FROM m0r03_pairs AS candidate;
            SET session_replication_role = origin;

            DO $pairs$
            DECLARE
                candidate record;
                changed integer;
                legal_count integer := 0;
                denied_count integer := 0;
                constraint_seen text;
            BEGIN
                FOR candidate IN
                    SELECT ingestion_id, previous_state, next_state,
                           expected_legal
                    FROM m0r03_pairs
                    ORDER BY previous_state, next_state
                LOOP
                    BEGIN
                        UPDATE public.uploads
                        SET state = candidate.next_state
                        WHERE ingestion_id = candidate.ingestion_id;
                        GET DIAGNOSTICS changed = ROW_COUNT;
                        IF NOT candidate.expected_legal OR changed <> 1 THEN
                            RAISE EXCEPTION 'illegal edge accepted: % -> %',
                                candidate.previous_state, candidate.next_state;
                        END IF;
                        legal_count := legal_count + 1;
                    EXCEPTION WHEN check_violation THEN
                        GET STACKED DIAGNOSTICS constraint_seen = CONSTRAINT_NAME;
                        IF candidate.expected_legal
                           OR constraint_seen <> 'uploads_legal_state_transition' THEN
                            RAISE;
                        END IF;
                        denied_count := denied_count + 1;
                    END;
                END LOOP;
                INSERT INTO m0r03_observations VALUES
                    ('legal_edges', legal_count), ('illegal_edges', denied_count);
            END
            $pairs$;

            ALTER TABLE public.uploads
                ENABLE TRIGGER uploads_require_bronze_pair_for_success;

            SELECT observation || '=' || observed_count
            FROM m0r03_observations
            ORDER BY observation;
            COMMIT;
        """
        completed = self._psql("exhaustive_runtime_transition_matrix", script)
        self._require(
            completed.returncode == 0,
            "Exhaustive direct-runtime transition matrix failed",
        )
        observed = set(completed.stdout.splitlines())
        self._require(
            {"initial_accepted=1", "initial_denied=9", "legal_edges=11", "illegal_edges=79"}
            .issubset(observed),
            "Exhaustive transition counts differ from the 11/79 contract",
        )
        self.evidence["direct_sql_matrix"] = {
            "initial_accepted": 1,
            "initial_denied": 9,
            "legal_edges": 11,
            "illegal_edges": 79,
            "runtime_role": "migration-admin direct matrix",
        }
        self.evidence["checks"].append("direct_sql_exhaustive_legal_illegal")

    def _assert_concurrency_and_retry(self) -> None:
        ingestion_id = "03030303-0303-4303-8303-030303030303"
        self._psql_success(
            "seed_concurrent_review_state",
            f"""
                SET session_replication_role = replica;
                INSERT INTO public.uploads (
                    ingestion_id, department, uploader_user_id,
                    uploader_display_name, uploaded_at_utc, original_filename,
                    stored_object_key, manifest_object_key, detected_mime_type,
                    declared_file_type, document_category, context_note,
                    capture_date, byte_size, source_sha256,
                    duplicate_of_ingestion_id, source_channel, state
                ) VALUES (
                    '{ingestion_id}', 'RND', 'usr_m0_r03_synthetic',
                    'M0 R03 Synthetic User',
                    TIMESTAMPTZ '2026-08-27T00:00:00Z', 'concurrent.jpg',
                    'm0r03/concurrent/original.jpg',
                    'm0r03/concurrent/manifest.json', 'image/jpeg', 'PHOTO',
                    'OTHER', 'Synthetic concurrent transition fixture.', NULL,
                    1, repeat('c', 64), NULL, 'WEB_UPLOAD',
                    'UNDER_HUMAN_REVIEW'
                );
                SET session_replication_role = origin;
            """,
        )

        def compete(target: str) -> Any:
            return self._psql(
                f"concurrent_transition_to_{target.lower()}",
                f"""
                    BEGIN;
                    UPDATE public.uploads SET state = '{target}'
                    WHERE ingestion_id = '{ingestion_id}';
                    SELECT pg_sleep(1);
                    COMMIT;
                """,
                timeout=30,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(compete, ("VERIFIED", "REVIEW_REJECTED"))
            )
        successful = [result for result in results if result.returncode == 0]
        rejected = [result for result in results if result.returncode != 0]
        self._require(
            len(successful) == 1
            and len(rejected) == 1
            and "illegal upload state transition:"
            in (rejected[0].stdout + rejected[0].stderr),
            "Concurrent conflicting transitions did not admit exactly one outcome",
        )
        final_state = self._psql_success(
            "concurrent_final_state",
            f"SELECT state FROM public.uploads WHERE ingestion_id = '{ingestion_id}'",
        )
        self._require(
            final_state in {"VERIFIED", "REVIEW_REJECTED"},
            "Concurrent transition ended outside a legal terminal outcome",
        )
        stale_retry_output = self._psql_success(
            "stale_compare_and_swap_retry",
            f"""
                WITH changed AS (
                    UPDATE public.uploads SET state = 'VERIFIED'
                    WHERE ingestion_id = '{ingestion_id}'
                      AND state = 'UNDER_HUMAN_REVIEW'
                    RETURNING 1
                ) SELECT count(*) FROM changed
            """,
        )
        stale_retry = stale_retry_output.splitlines()[-1]
        self._require(stale_retry == "0", "A stale retry changed the final state")
        self.evidence["concurrency"] = {
            "successful_outcomes": 1,
            "rejected_outcomes": 1,
            "final_state": final_state,
            "stale_retry_rows": 0,
        }
        self.evidence["checks"].append("concurrency_and_stale_retry")

    def _assert_ocr_failed_recovery(self) -> None:
        ingestion_id = "09090909-0909-4909-8909-090909090909"
        job_id = "09090909-0909-4909-8909-090909090910"
        original_id = "09090909-0909-4909-8909-090909090911"
        manifest_id = "09090909-0909-4909-8909-090909090912"
        original_assignment = "09090909-0909-4909-8909-090909090913"
        manifest_assignment = "09090909-0909-4909-8909-090909090914"
        self._psql_success(
            "seed_live_ocr_failed_recovery",
            f"""
                INSERT INTO uploads VALUES (
                    '{ingestion_id}', 'RND', 'usr_m0_r03_synthetic',
                    'M0 R03 Synthetic User', now(), 'recovery.jpg',
                    'rd/recovery/original.jpg', 'rd/recovery/manifest.json',
                    'image/jpeg', 'PHOTO', 'LAB_NOTE',
                    'Synthetic live OCR recovery fixture.', NULL, 1,
                    repeat('9', 64), NULL, 'WEB_UPLOAD', 'RECEIVED'
                );
                INSERT INTO bronze_objects VALUES
                    ('{original_id}', '{ingestion_id}', 'sc-rd-bronze-originals',
                     'rd/recovery/original.jpg', 'ORIGINAL', repeat('9',64),
                     'version-original-0001', 'COMPLIANCE',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z',
                     TIMESTAMPTZ '2026-09-01T00:00:00Z'),
                    ('{manifest_id}', '{ingestion_id}', 'sc-rd-bronze-manifests',
                     'rd/recovery/manifest.json', 'MANIFEST', repeat('8',64),
                     'version-manifest-0001', 'COMPLIANCE',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z',
                     TIMESTAMPTZ '2026-09-01T00:00:00Z');
                INSERT INTO bronze_retention_assignments VALUES
                    ('{original_assignment}', '{original_id}', '{ingestion_id}',
                     'sc-rd-bronze-originals', 'rd/recovery/original.jpg',
                     'ORIGINAL', 'version-original-0001', 'LAB_NOTE', 'permanent',
                     'smartcoat_retention_2026_08_v1', now(), 'live-acceptance',
                     TIMESTAMPTZ '2026-09-01T00:00:00Z',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z', true),
                    ('{manifest_assignment}', '{manifest_id}', '{ingestion_id}',
                     'sc-rd-bronze-manifests', 'rd/recovery/manifest.json',
                     'MANIFEST', 'version-manifest-0001', 'LAB_NOTE', 'permanent',
                     'smartcoat_retention_2026_08_v1', now(), 'live-acceptance',
                     TIMESTAMPTZ '2026-09-01T00:00:00Z',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z', true);
                INSERT INTO bronze_retention_enforcement_evidence (
                    enforcement_evidence_id, retention_assignment_id,
                    bucket_name, object_key, object_kind, object_version_id,
                    data_category, retention_class, retention_policy_version,
                    accepted_storage_at_utc, requested_retention_mode,
                    requested_retain_until_utc, requested_legal_hold_status,
                    observed_object_version_id, observed_retention_mode,
                    observed_retain_until_utc, observed_legal_hold_status,
                    enforcement_verified_at_utc,
                    enforcement_verification_result, failure_code, enforced_by,
                    details_json
                ) VALUES
                    ('09090909-0909-4909-8909-090909090915',
                     '{original_assignment}', 'sc-rd-bronze-originals',
                     'rd/recovery/original.jpg', 'ORIGINAL',
                     'version-original-0001', 'LAB_NOTE', 'permanent',
                     'smartcoat_retention_2026_08_v1',
                     TIMESTAMPTZ '2026-09-01T00:00:00Z', 'COMPLIANCE',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z', 'ON',
                     'version-original-0001', 'COMPLIANCE',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z', 'ON', now(),
                     'SUCCESS', NULL, 'live-acceptance', '{{}}'),
                    ('09090909-0909-4909-8909-090909090916',
                     '{manifest_assignment}', 'sc-rd-bronze-manifests',
                     'rd/recovery/manifest.json', 'MANIFEST',
                     'version-manifest-0001', 'LAB_NOTE', 'permanent',
                     'smartcoat_retention_2026_08_v1',
                     TIMESTAMPTZ '2026-09-01T00:00:00Z', 'COMPLIANCE',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z', 'ON',
                     'version-manifest-0001', 'COMPLIANCE',
                     TIMESTAMPTZ '2036-09-01T00:00:00Z', 'ON', now(),
                     'SUCCESS', NULL, 'live-acceptance', '{{}}');
                INSERT INTO bronze_pairs VALUES (
                    '09090909-0909-4909-8909-090909090917', '{ingestion_id}',
                    '{original_id}', '{manifest_id}', repeat('7',64), 'permanent',
                    'smartcoat_retention_2026_08_v1', now(), 'live-acceptance'
                );
                UPDATE uploads SET state='BRONZE_COMMITTED'
                WHERE ingestion_id='{ingestion_id}';
                INSERT INTO ocr_jobs VALUES (
                    '{job_id}', '{ingestion_id}', 'QUEUED', now(), NULL, NULL, 0, NULL
                );
                UPDATE uploads SET state='OCR_QUEUED'
                WHERE ingestion_id='{ingestion_id}';
                UPDATE ocr_jobs SET status='RUNNING', started_at_utc=now(),
                    attempt_count=1 WHERE ocr_job_id='{job_id}';
                INSERT INTO ocr_runs VALUES (
                    '09090909-0909-4909-8909-090909090918', '{job_id}',
                    '{ingestion_id}', 'paddleocr', 'synthetic', '{{}}',
                    repeat('9',64), NULL, NULL, 'FAILED', now(), now()
                );
                UPDATE ocr_jobs SET status='FAILED', completed_at_utc=now(),
                    error_reason='synthetic first failure' WHERE ocr_job_id='{job_id}';
                UPDATE uploads SET state='OCR_FAILED'
                WHERE ingestion_id='{ingestion_id}';
            """,
        )
        bronze_before = self._psql_rows(
            "ocr_recovery_bronze_before",
            f"""
                SELECT pair_record.*, original.object_version_id AS original_version,
                       manifest.object_version_id AS manifest_version
                FROM bronze_pairs pair_record
                JOIN bronze_objects original ON original.bronze_object_id = pair_record.original_bronze_object_id
                JOIN bronze_objects manifest ON manifest.bronze_object_id = pair_record.manifest_bronze_object_id
                WHERE pair_record.ingestion_id = '{ingestion_id}'
            """,
        )
        self._psql_success(
            "operator_retry_and_terminal_failure",
            f"""
                SET ROLE smartcoat_ocr;
                BEGIN;
                UPDATE ocr_jobs SET status='QUEUED', started_at_utc=NULL,
                    completed_at_utc=NULL, error_reason=NULL
                WHERE ocr_job_id='{job_id}' AND status='FAILED'
                  AND attempt_count < 3;
                INSERT INTO audit_events VALUES (
                    '09090909-0909-4909-8909-090909090919', now(),
                    'usr_m0_r03_synthetic', NULL, 'OCR_JOB', '{job_id}',
                    'OCR_RETRY_INITIATED', 'FAILED', 'QUEUED',
                    '09090909-0909-4909-8909-090909090920',
                    '{{"attempt_count":1,"original_object_version_id":"version-original-0001"}}'
                );
                UPDATE uploads SET state='OCR_QUEUED'
                WHERE ingestion_id='{ingestion_id}' AND state='OCR_FAILED';
                INSERT INTO audit_events VALUES (
                    '09090909-0909-4909-8909-090909090921', now(),
                    'usr_m0_r03_synthetic', NULL, 'UPLOAD', '{ingestion_id}',
                    'UPLOAD_STATE_CHANGED', 'OCR_FAILED', 'OCR_QUEUED',
                    '09090909-0909-4909-8909-090909090922',
                    '{{"ocr_job_id":"{job_id}","original_object_version_id":"version-original-0001"}}'
                );
                COMMIT;
                UPDATE ocr_jobs SET status='RUNNING', started_at_utc=now(),
                    attempt_count=attempt_count+1 WHERE ocr_job_id='{job_id}';
                INSERT INTO ocr_runs VALUES (
                    '09090909-0909-4909-8909-090909090923', '{job_id}',
                    '{ingestion_id}', 'paddleocr', 'synthetic-retry', '{{}}',
                    repeat('9',64), NULL, NULL, 'FAILED', now(), now()
                );
                UPDATE ocr_jobs SET status='FAILED', completed_at_utc=now(),
                    error_reason='synthetic terminal failure' WHERE ocr_job_id='{job_id}';
                UPDATE uploads SET state='OCR_FAILED'
                WHERE ingestion_id='{ingestion_id}' AND state='OCR_QUEUED';
                RESET ROLE;
            """,
        )
        bronze_after = self._psql_rows(
            "ocr_recovery_bronze_after",
            f"""
                SELECT pair_record.*, original.object_version_id AS original_version,
                       manifest.object_version_id AS manifest_version
                FROM bronze_pairs pair_record
                JOIN bronze_objects original ON original.bronze_object_id = pair_record.original_bronze_object_id
                JOIN bronze_objects manifest ON manifest.bronze_object_id = pair_record.manifest_bronze_object_id
                WHERE pair_record.ingestion_id = '{ingestion_id}'
            """,
        )
        terminal = self._psql_rows(
            "ocr_recovery_terminal_state",
            f"""
                SELECT u.state, j.status, j.attempt_count,
                    (SELECT count(*) FROM ocr_jobs WHERE ingestion_id=u.ingestion_id) AS job_count,
                    (SELECT count(*) FROM silver_drafts WHERE ingestion_id=u.ingestion_id) AS draft_count,
                    (SELECT count(*) FROM audit_events WHERE entity_id='{job_id}' AND event_type='OCR_RETRY_INITIATED') AS retry_audits
                FROM uploads u JOIN ocr_jobs j USING (ingestion_id)
                WHERE u.ingestion_id='{ingestion_id}'
            """,
        )
        self._require(bronze_before == bronze_after, "OCR recovery changed the Bronze pair")
        self._require(
            terminal == [{
                "state": "OCR_FAILED", "status": "FAILED", "attempt_count": 2,
                "job_count": 1, "draft_count": 0, "retry_audits": 1,
            }],
            "Live OCR recovery did not preserve one job and a defined terminal state",
        )
        self.evidence["ocr_failed_recovery"] = terminal[0]
        self.evidence["checks"].append("live_ocr_failed_recovery")

    def lifecycle(self) -> None:
        self._install_cleanup_handlers()
        self.state_change_attempted = True
        self._verify_migration_execution_image_boundary()
        started = self._compose(
            "start_isolated_postgres",
            "up",
            "--detach",
            "--no-deps",
            "--pull",
            "never",
            "postgres",
            timeout=180,
        )
        self._require(
            started.returncode == 0,
            "Isolated PostgreSQL could not start",
            accepted.EnvironmentBlocked,
        )
        container_id = self._wait_for_postgres()
        self._verify_running_postgres_isolation(container_id)
        public_before = self._catalog_snapshot(
            "public_catalog_before", accepted.PUBLIC_CATALOG_QUERIES
        )
        self._assert_public_bootstrap(public_before)
        self._require(
            self._metadata_state("metadata_before_adoption") == [False] * 6,
            "Fresh bootstrap unexpectedly contains migration metadata",
        )

        upgraded_before: list[dict[str, Any]] = []
        if self.volume_mode == "upgraded":
            upgraded_before = self._insert_upgraded_fixture()
            self._require(
                len(upgraded_before) == len(STATES),
                "Upgraded fixture does not cover every existing state",
            )

        adoption = self._run_migration(
            "explicit_bootstrap_adoption", "adopt", self.database_name
        )
        self._require(
            adoption.returncode == 0
            and "status=ADOPTED" in adoption.stdout
            and "evidence_inserted=true" in adoption.stdout,
            "Explicit bootstrap adoption failed",
        )
        adoption_before = self._adoption_rows("adoption_before_transition_apply")
        apply_result = self._run_migration("apply_complete_transition_chain", "apply")
        self._require(
            apply_result.returncode == 0
            and "discovered=9" in apply_result.stdout
            and "already_applied=1" in apply_result.stdout
            and "applied_now=8" in apply_result.stdout,
            "Complete migration chain through version 9 did not apply exactly once",
        )
        ledger = self._ledger_rows("transition_ledger")
        self._require(
            len(ledger) == 9
            and ledger[0]["version"] == 1
            and ledger[0]["sha256"] == accepted.EXPECTED_BASELINE_SHA256
            and ledger[2]["version"] == 3
            and ledger[2]["name"] == "enforce_upload_state_transitions"
            and ledger[2]["sha256"] == self.transition_sha256
            and ledger[8]["version"] == 9
            and ledger[8]["name"] == "add_operator_ocr_retry_transition"
            and ledger[8]["sha256"] == self.recovery_sha256,
            "Transition migration ledger evidence is incorrect",
        )
        self._require(
            self._adoption_rows("adoption_after_transition_apply") == adoption_before,
            "Transition migration changed adoption evidence",
        )
        if self.volume_mode == "upgraded":
            upgraded_after = self._psql_rows(
                "upgraded_volume_fixture_after",
                """
                    SELECT ingestion_id::text AS ingestion_id, state
                    FROM public.uploads
                    ORDER BY state, ingestion_id
                """,
            )
            self._require(
                upgraded_after == upgraded_before,
                "Transition migration rewrote existing-volume states",
            )
            self.evidence["upgraded_rows_preserved"] = len(upgraded_after)
        else:
            self._require(
                self._psql_success(
                    "fresh_volume_remains_empty",
                    "SELECT count(*) FROM public.uploads",
                )
                == "0",
                "Fresh transition migration created application rows",
            )

        self._ensure_synthetic_user()
        self._assert_installed_contract()
        self._assert_initial_and_exhaustive_direct_sql()
        self._assert_concurrency_and_retry()
        self._assert_ocr_failed_recovery()
        reapply = self._run_migration("idempotent_transition_reapply", "apply")
        self._require(
            reapply.returncode == 0
            and "discovered=9" in reapply.stdout
            and "already_applied=9" in reapply.stdout
            and "applied_now=0" in reapply.stdout,
            "Transition migration reapplication was not idempotent",
        )
        self.evidence["checks"].append("idempotent_reapply")


def focused_checks() -> dict[str, bool]:
    migration = TRANSITION_MIGRATION.read_text(encoding="utf-8")
    recovery = RECOVERY_MIGRATION.read_text(encoding="utf-8")
    return {
        "accepted_helper_authenticated": sha256_path(ACCEPTED_HARNESS)
        == ACCEPTED_HARNESS_SHA256,
        "explicit_flag_is_required": "--confirm-disposable-synthetic-transition-run"
        in Path(__file__).read_text(encoding="utf-8"),
        "exact_eleven_edges_declared": len(LEGAL_TRANSITIONS) == 11,
        "terminal_states_have_no_edges": not {
            "REJECTED",
            "REVIEW_REJECTED",
        }
        & {edge[0] for edge in LEGAL_TRANSITIONS},
        "direct_sql_guard_present": "OLD.state" in migration
        and "NEW.state" in migration,
        "initial_state_guard_present": "uploads_legal_initial_state" in migration,
        "operator_retry_edge_present": "operator_retry_failed_ocr" in recovery
        and "SILVER_DRAFT_READY" not in recovery,
        "concurrency_check_present": "ThreadPoolExecutor" in Path(__file__).read_text(
            encoding="utf-8"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run M0-R03 only against generated disposable PostgreSQL projects."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--confirm-disposable-synthetic-transition-run",
        action="store_true",
        help="required explicit authorization for fresh and upgraded live scenarios",
    )
    mode.add_argument("--run-focused-regression-checks", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_focused_regression_checks:
        checks = focused_checks()
        print(json.dumps(checks, sort_keys=True, indent=2))
        if all(checks.values()):
            print("FOCUSED_M0_R03_CHECKS_PASS")
            return 0
        print(RESULT_HARNESS_FAILURE)
        return 2
    if not args.confirm_disposable_synthetic_transition_run:
        print(
            "Explicit --confirm-disposable-synthetic-transition-run is required; "
            "Docker and PostgreSQL were not touched.",
            file=sys.stderr,
        )
        print(RESULT_ISOLATION_BLOCKED)
        return 2

    combined: dict[str, Any] = {
        "ticket": "M0-R03",
        "accepted_helper_sha256": ACCEPTED_HARNESS_SHA256,
        "scenarios": [],
    }
    final_result = RESULT_PASS
    for volume_mode in ("fresh", "upgraded"):
        harness = LiveStateTransitionAcceptance(volume_mode)
        scenario_result = RESULT_PASS
        failure = ""
        try:
            harness.preflight()
            harness.lifecycle()
        except accepted.EnvironmentBlocked as exc:
            scenario_result = RESULT_ENVIRONMENT_BLOCKED
            failure = str(exc)
        except accepted.IsolationBlocked as exc:
            scenario_result = RESULT_ISOLATION_BLOCKED
            failure = str(exc)
        except accepted.ProductContractFailure as exc:
            scenario_result = RESULT_PRODUCT_FAILURE
            failure = str(exc)
        except Exception as exc:
            scenario_result = RESULT_HARNESS_FAILURE
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            scenario_result, failure = accepted.finalize_harness(
                harness, scenario_result, failure
            )
        scenario_evidence = harness.sanitized_evidence()
        scenario_evidence["result"] = scenario_result
        if failure:
            scenario_evidence["failure"] = failure
        combined["scenarios"].append(scenario_evidence)
        if scenario_result != RESULT_PASS:
            final_result = scenario_result
            break

    print(json.dumps(combined, sort_keys=True, indent=2))
    print(final_result)
    return 0 if final_result == RESULT_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
