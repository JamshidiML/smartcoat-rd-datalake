#!/usr/bin/env python3
"""Run reviewed migration live harnesses with integrated RBAC render inputs.

The reviewed harnesses remain byte-for-byte unchanged and authenticate their
normal dependency chain. This adapter supplies only generated synthetic values
for Compose variables introduced by M0-R02; it does not alter lifecycle,
locking, rollback, drift, history, cleanup, or evidence assertions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import secrets
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = ROOT / "infra/postgres/tests"
SUITES = {
    "lifecycle": (
        "live_migration_lifecycle_acceptance.py",
        "4d7fbe8d33d36b6ff50161f4374cf16477667903253b790cbc37cb3e54707cfd",
        "--confirm-disposable-synthetic-run",
    ),
    "lock": (
        "live_migration_lock_acceptance.py",
        "dca6c0c8473c72134f68a11938be24324c6bbdbffc77c9dd7d56ed4bab736b53",
        "--confirm-disposable-synthetic-lock-run",
    ),
    "rollback": (
        "live_migration_rollback_acceptance.py",
        "dd549b3f9d51e9843c5db6c2127479eb6a6e0e8cef104d08ddef612d19b4ac16",
        "--confirm-disposable-synthetic-rollback-run",
    ),
    "drift": (
        "live_migration_drift_acceptance.py",
        "81b6910784c2294d68ef41b5f8afc9de369ea16bd7295ba5dec508d068c0edb7",
        "--confirm-disposable-synthetic-drift-run",
    ),
    "history": (
        "live_migration_history_drift_acceptance.py",
        "e796bcff71922cf0add8247b162c153f2665ff40d97e91ee31fa9883cb04a08a",
        "--confirm-disposable-synthetic-four-scenario-drift-run",
    ),
}


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_suite(suite: str) -> ModuleType:
    filename, expected_sha256, _flag = SUITES[suite]
    path = TEST_ROOT / filename
    if not path.is_file() or sha256_path(path) != expected_sha256:
        raise RuntimeError(f"reviewed {suite} harness authentication failed")
    spec = importlib.util.spec_from_file_location(
        f"integrated_wave1_{suite}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"reviewed {suite} harness could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_rbac_render_adapter(module: ModuleType) -> None:
    accepted = module if module.__name__.endswith("_lifecycle") else module.accepted
    harness_type = accepted.LiveMigrationLifecycleAcceptance
    original: Callable[[Any], None] = harness_type._write_synthetic_configuration

    def write_integrated_configuration(harness: Any) -> None:
        original(harness)
        values = {
            "POSTGRES_INGESTION_PASSWORD": secrets.token_hex(24),
            "POSTGRES_OCR_PASSWORD": secrets.token_hex(24),
            "POSTGRES_REVIEW_PASSWORD": secrets.token_hex(24),
            "POSTGRES_BACKUP_PASSWORD": secrets.token_hex(24),
        }
        urls = {
            "DATABASE_INGESTION_URL": (
                "postgresql://smartcoat_ingestion:"
                + values["POSTGRES_INGESTION_PASSWORD"]
                + f"@postgres:5432/{harness.database_name}"
            ),
            "DATABASE_OCR_URL": (
                "postgresql://smartcoat_ocr:"
                + values["POSTGRES_OCR_PASSWORD"]
                + f"@postgres:5432/{harness.database_name}"
            ),
            "DATABASE_REVIEW_URL": (
                "postgresql://smartcoat_review:"
                + values["POSTGRES_REVIEW_PASSWORD"]
                + f"@postgres:5432/{harness.database_name}"
            ),
        }
        harness.secret_values.update(values.values())
        harness.secret_values.update(urls.values())
        with harness.environment_file.open("a", encoding="utf-8") as environment:
            environment.write(
                "POSTGRES_ROLE_ADMIN_URL=" + harness.migration_database_url + "\n"
            )
            for name, value in (*values.items(), *urls.items()):
                environment.write(f"{name}={value}\n")
        harness.environment_file.chmod(0o600)

    harness_type._write_synthetic_configuration = write_integrated_configuration


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in SUITES:
        print("usage: live_integrated_wave1_migration_acceptance.py SUITE FLAG")
        return 2
    suite = sys.argv[1]
    expected_flag = SUITES[suite][2]
    if sys.argv[2] != expected_flag:
        print(f"explicit {expected_flag} is required")
        return 2
    module = load_suite(suite)
    install_rbac_render_adapter(module)
    sys.argv = [str(TEST_ROOT / SUITES[suite][0]), expected_flag]
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
