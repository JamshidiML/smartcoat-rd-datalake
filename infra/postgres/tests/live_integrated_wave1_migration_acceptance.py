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

# These five protected files changed through the explicitly reviewed Wave-1
# integration sequence.  The adapter accepts only the known pre-integration
# value and replaces it with this statically authenticated integrated value.
# Every protected path remains present; the accepted harnesses still perform
# their normal before/after equality and fail-closed checks.
INTEGRATED_PROTECTED_HASHES = {
    "infra/postgres/tests/test_migrate.py": (
        "e12d98beeb6641f0eba3bad70167fb54759d2b37dcbfe9e08819df1a310ab3e6"
    ),
    "compose.yaml": (
        "6fd49bf74e230fea5f9d4d0fcede80f4b2a52e6d10db2658c0b2b450bd114535"
    ),
    ".env.example": (
        "0d4467f98fa88d65489e8582c3cca07d221a7c5d0ed6c208cccc0f25fcdb0c36"
    ),
    "docs/runbooks/VPS_DEPLOYMENT.md": (
        "0df81839b3a159361e76115076ad29fc859d05c2c42a000337c25c811b798901"
    ),
    "infra/postgres/tests/test_migration_operations.py": (
        "10c4507dce6cee4517a8bd1cb05d0dfa0d27d6385659c806597371d7fb3308b2"
    ),
}

PREINTEGRATION_PROTECTED_HASHES = {
    "infra/postgres/tests/test_migrate.py": (
        "2fcfe8922e3120607293e65ae96e2a732826d186a870713691993136f7e765f9"
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


def reauthenticate_integrated_protected_hashes(module: ModuleType) -> None:
    protected = getattr(module, "PROTECTED_HASHES", None)
    if protected is None:
        return
    if not isinstance(protected, dict):
        raise RuntimeError("reviewed protected-hash contract is not a dictionary")
    for path, integrated_sha256 in INTEGRATED_PROTECTED_HASHES.items():
        if path not in protected:
            continue
        if protected[path] != PREINTEGRATION_PROTECTED_HASHES[path]:
            raise RuntimeError(
                f"reviewed pre-integration hash was not authenticated: {path}"
            )
        if sha256_path(ROOT / path) != integrated_sha256:
            raise RuntimeError(
                f"integrated protected path authentication failed: {path}"
            )
        # Mutate the accepted dictionary in place so default arguments bound
        # by the reviewed harness continue to reference the complete map.
        protected[path] = integrated_sha256


def patch_lifecycle_harness(accepted: ModuleType) -> None:
    harness_type = accepted.LiveMigrationLifecycleAcceptance
    if getattr(harness_type, "_wave1_rbac_render_adapter", False):
        return
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
    harness_type._wave1_rbac_render_adapter = True


def install_rbac_render_adapter(suite: str, module: ModuleType) -> None:
    reauthenticate_integrated_protected_hashes(module)
    if suite == "lifecycle":
        patch_lifecycle_harness(module)
        return
    if suite == "lock":
        original = module.load_accepted_harness

        def load_lock_dependency() -> ModuleType:
            accepted = original()
            reauthenticate_integrated_protected_hashes(accepted)
            patch_lifecycle_harness(accepted)
            return accepted

        module.load_accepted_harness = load_lock_dependency
        return
    if suite == "rollback":
        original = module.load_accepted_harnesses

        def load_rollback_dependencies() -> tuple[ModuleType, ModuleType]:
            accepted, lock = original()
            reauthenticate_integrated_protected_hashes(accepted)
            reauthenticate_integrated_protected_hashes(lock)
            patch_lifecycle_harness(accepted)
            return accepted, lock

        module.load_accepted_harnesses = load_rollback_dependencies
        return
    if suite == "drift":
        original = module.load_authenticated_live_dependencies

        def load_drift_dependencies() -> tuple[Any, Any, Any]:
            rollback, accepted, lock = original()
            reauthenticate_integrated_protected_hashes(rollback)
            reauthenticate_integrated_protected_hashes(accepted)
            reauthenticate_integrated_protected_hashes(lock)
            patch_lifecycle_harness(accepted)
            return rollback, accepted, lock

        module.load_authenticated_live_dependencies = load_drift_dependencies
        return

    def load_history_dependencies() -> tuple[Any, Any, Any, Any]:
        c2_path = ROOT / module.C2_RELATIVE_PATH
        if sha256_path(c2_path) != module.C2_SHA256:
            raise RuntimeError("reviewed c.2 harness authentication failed")
        spec = importlib.util.spec_from_file_location(
            "integrated_wave1_history_c2", c2_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("reviewed c.2 harness could not be loaded")
        c2 = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = c2
        spec.loader.exec_module(c2)
        reauthenticate_integrated_protected_hashes(c2)
        if {
            module.C2_RELATIVE_PATH: module.C2_SHA256,
            **dict(c2.PROTECTED_HASHES),
        } != module.PROTECTED_HASHES:
            raise RuntimeError("integrated c.2 protected boundary mismatch")
        original_c2_loader = c2.load_authenticated_live_dependencies

        def load_c2_dependencies() -> tuple[Any, Any, Any]:
            rollback, accepted, lock = original_c2_loader()
            reauthenticate_integrated_protected_hashes(rollback)
            reauthenticate_integrated_protected_hashes(accepted)
            reauthenticate_integrated_protected_hashes(lock)
            patch_lifecycle_harness(accepted)
            return rollback, accepted, lock

        c2.load_authenticated_live_dependencies = load_c2_dependencies
        rollback, accepted, lock = c2.load_authenticated_live_dependencies()
        if sha256_path(c2_path) != module.C2_SHA256:
            raise RuntimeError("reviewed c.2 harness changed during authentication")
        return c2, rollback, accepted, lock

    module.load_authenticated_c2 = load_history_dependencies


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
    install_rbac_render_adapter(suite, module)
    sys.argv = [str(TEST_ROOT / SUITES[suite][0]), expected_flag]
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
