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
        self.transition_fixture = (
            self.migration_fixture_directory / TRANSITION_MIGRATION.name
        )
        self.transition_sha256 = sha256_path(TRANSITION_MIGRATION)
        self.ingestion_password = secrets.token_hex(24)
        self.ocr_password = secrets.token_hex(24)
        self.review_password = secrets.token_hex(24)
        self.backup_password = secrets.token_hex(24)
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
            )
        self.environment_file.chmod(0o600)

    def _prepare_baseline_fixture(self) -> None:
        self._require(
            BASELINE_MIGRATION.is_file() and TRANSITION_MIGRATION.is_file(),
            "Required migration source is unavailable",
            accepted.EnvironmentBlocked,
        )
        baseline = BASELINE_MIGRATION.read_bytes()
        transition = TRANSITION_MIGRATION.read_bytes()
        self._require(
            hashlib.sha256(baseline).hexdigest()
            == accepted.EXPECTED_BASELINE_SHA256,
            "Accepted baseline migration checksum changed",
            accepted.ProductContractFailure,
        )
        self.migration_fixture_directory.mkdir(mode=0o700)
        self.baseline_fixture.write_bytes(baseline)
        self.transition_fixture.write_bytes(transition)
        self.baseline_fixture.chmod(0o400)
        self.transition_fixture.chmod(0o400)
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
            self.baseline_fixture.resolve(),
            self.transition_fixture.resolve(),
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
        self._require(
            sha256_path(self.transition_fixture) == self.transition_sha256,
            "Transition fixture differs from the repository migration",
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

            SET ROLE smartcoat_app;
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
            RESET ROLE;

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

            SET ROLE smartcoat_app;
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
            RESET ROLE;

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
            {"initial_accepted=1", "initial_denied=9", "legal_edges=10", "illegal_edges=80"}
            .issubset(observed),
            "Exhaustive transition counts differ from the 10/80 contract",
        )
        self.evidence["direct_sql_matrix"] = {
            "initial_accepted": 1,
            "initial_denied": 9,
            "legal_edges": 10,
            "illegal_edges": 80,
            "runtime_role": "smartcoat_app",
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
                    SET ROLE smartcoat_app;
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
                SET ROLE smartcoat_app;
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
        apply_result = self._run_migration("apply_transition_migration", "apply")
        self._require(
            apply_result.returncode == 0
            and "discovered=2" in apply_result.stdout
            and "already_applied=1" in apply_result.stdout
            and "applied_now=1" in apply_result.stdout,
            "Version-3 transition migration did not apply exactly once",
        )
        ledger = self._ledger_rows("transition_ledger")
        self._require(
            len(ledger) == 2
            and ledger[0]["version"] == 1
            and ledger[0]["sha256"] == accepted.EXPECTED_BASELINE_SHA256
            and ledger[1]["version"] == 3
            and ledger[1]["name"] == "enforce_upload_state_transitions"
            and ledger[1]["sha256"] == self.transition_sha256,
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
        reapply = self._run_migration("idempotent_transition_reapply", "apply")
        self._require(
            reapply.returncode == 0
            and "discovered=2" in reapply.stdout
            and "already_applied=2" in reapply.stdout
            and "applied_now=0" in reapply.stdout,
            "Transition migration reapplication was not idempotent",
        )
        self.evidence["checks"].append("idempotent_reapply")


def focused_checks() -> dict[str, bool]:
    migration = TRANSITION_MIGRATION.read_text(encoding="utf-8")
    return {
        "accepted_helper_authenticated": sha256_path(ACCEPTED_HARNESS)
        == ACCEPTED_HARNESS_SHA256,
        "explicit_flag_is_required": "--confirm-disposable-synthetic-transition-run"
        in Path(__file__).read_text(encoding="utf-8"),
        "exact_ten_edges_declared": len(LEGAL_TRANSITIONS) == 10,
        "terminal_states_have_no_edges": not {
            "REJECTED",
            "OCR_FAILED",
            "REVIEW_REJECTED",
        }
        & {edge[0] for edge in LEGAL_TRANSITIONS},
        "direct_sql_guard_present": "OLD.state" in migration
        and "NEW.state" in migration,
        "initial_state_guard_present": "uploads_legal_initial_state" in migration,
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
