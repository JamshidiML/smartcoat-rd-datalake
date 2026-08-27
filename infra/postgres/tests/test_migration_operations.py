from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = ROOT / "compose.yaml"
EXAMPLE_ENV = ROOT / ".env.example"
MIGRATION_SERVICE = "postgres-migrate"

EXPECTED_EXISTING_NETWORKS = {
    "postgres": {"backend", "edge"},
    "minio": {"backend", "edge"},
    "minio-bootstrap": {"backend"},
    "api": {"backend", "edge"},
    "web": {"edge"},
    "ocr-worker": {"backend"},
}
EXPECTED_EXISTING_PORTS = {
    "postgres": [("127.0.0.1", "5432", 5432)],
    "minio": [
        ("127.0.0.1", "9000", 9000),
        ("127.0.0.1", "9001", 9001),
    ],
    "minio-bootstrap": [],
    "api": [("127.0.0.1", "8000", 8000)],
    "web": [("127.0.0.1", "8080", 8080)],
    "ocr-worker": [],
}


def render_compose(env_file: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("MIGRATION_DATABASE_URL", None)
    environment.pop("COMPOSE_FILE", None)
    environment.pop("COMPOSE_PROFILES", None)
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def environment_without_migration_url(replacement: str | None) -> str:
    lines = EXAMPLE_ENV.read_text().splitlines(keepends=True)
    matches = [line for line in lines if line.startswith("MIGRATION_DATABASE_URL=")]
    if len(matches) != 1:
        raise AssertionError(".env.example must define exactly one migration URL")
    result = [line for line in lines if not line.startswith("MIGRATION_DATABASE_URL=")]
    if replacement is not None:
        result.append(f"MIGRATION_DATABASE_URL={replacement}\n")
    return "".join(result)


class MigrationOperationsTests(unittest.TestCase):
    compose: dict[str, Any]
    services: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        rendered = render_compose(EXAMPLE_ENV)
        if rendered.returncode != 0:
            raise AssertionError(
                "synthetic Compose rendering failed: "
                + (rendered.stderr or rendered.stdout).strip()
            )
        cls.compose = json.loads(rendered.stdout)
        cls.services = cls.compose["services"]

    def test_dedicated_one_shot_service_defaults_only_to_apply(self) -> None:
        migration = self.services[MIGRATION_SERVICE]

        self.assertEqual(["python", "/opt/smartcoat-postgres/migrate.py"], migration["entrypoint"])
        self.assertEqual(["apply"], migration["command"])
        self.assertEqual("no", migration["restart"])

    def test_migration_service_reuses_exact_api_build(self) -> None:
        migration = self.services[MIGRATION_SERVICE]
        api = self.services["api"]

        self.assertEqual(api["build"], migration["build"])
        self.assertEqual(
            (ROOT / "apps/api/src").resolve(),
            Path(migration["build"]["context"]).resolve(),
        )

    def test_migration_source_tree_is_mounted_read_only(self) -> None:
        migration = self.services[MIGRATION_SERVICE]
        self.assertEqual(1, len(migration["volumes"]))
        mount = migration["volumes"][0]

        self.assertEqual("bind", mount["type"])
        self.assertEqual((ROOT / "infra/postgres").resolve(), Path(mount["source"]).resolve())
        self.assertEqual("/opt/smartcoat-postgres", mount["target"])
        self.assertIs(True, mount["read_only"])
        for relative_path in (
            "migrate.py",
            "bootstrap_contract.py",
            "init.sql",
            "migrations",
        ):
            self.assertTrue((ROOT / "infra/postgres" / relative_path).exists())

    def test_migration_service_is_backend_only_without_ports(self) -> None:
        migration = self.services[MIGRATION_SERVICE]

        self.assertEqual({"backend"}, set(migration["networks"]))
        self.assertNotIn("edge", migration["networks"])
        self.assertNotIn("ports", migration)

    def test_migration_waits_for_postgres_health(self) -> None:
        dependency = self.services[MIGRATION_SERVICE]["depends_on"]["postgres"]
        self.assertEqual("service_healthy", dependency["condition"])

    def test_api_and_worker_wait_for_successful_migration(self) -> None:
        for service_name in ("api", "ocr-worker"):
            dependencies = self.services[service_name]["depends_on"]
            self.assertEqual(
                "service_completed_successfully",
                dependencies[MIGRATION_SERVICE]["condition"],
            )
            self.assertEqual("service_healthy", dependencies["postgres"]["condition"])
            self.assertEqual(
                "service_completed_successfully",
                dependencies["minio-bootstrap"]["condition"],
            )
        self.assertEqual(
            "service_healthy",
            self.services["web"]["depends_on"]["api"]["condition"],
        )
        self.assertEqual(
            "service_healthy",
            self.services["minio-bootstrap"]["depends_on"]["minio"]["condition"],
        )

    def test_migration_credential_is_required_and_isolated(self) -> None:
        credential_consumers = {
            service_name
            for service_name, service in self.services.items()
            if "MIGRATION_DATABASE_URL" in service.get("environment", {})
        }
        migration_environment = self.services[MIGRATION_SERVICE]["environment"]

        self.assertEqual({MIGRATION_SERVICE}, credential_consumers)
        self.assertEqual({"MIGRATION_DATABASE_URL"}, set(migration_environment))
        self.assertNotIn("DATABASE_URL", migration_environment)
        for forbidden_prefix in ("MINIO_", "LOCAL_USER_", "SESSION_", "OCR_"):
            self.assertFalse(
                any(name.startswith(forbidden_prefix) for name in migration_environment)
            )

    def test_existing_network_memberships_and_ports_are_unchanged(self) -> None:
        observed_networks = {
            service_name: set(self.services[service_name].get("networks", {}))
            for service_name in EXPECTED_EXISTING_NETWORKS
        }
        observed_ports = {
            service_name: [
                (port.get("host_ip"), port.get("published"), port.get("target"))
                for port in self.services[service_name].get("ports", [])
            ]
            for service_name in EXPECTED_EXISTING_PORTS
        }

        self.assertEqual(EXPECTED_EXISTING_NETWORKS, observed_networks)
        self.assertEqual(EXPECTED_EXISTING_PORTS, observed_ports)

    def test_missing_or_empty_migration_credential_fails_compose_rendering(self) -> None:
        for label, replacement in (("missing", None), ("empty", "")):
            with self.subTest(label=label), tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".env"
            ) as temporary:
                temporary.write(environment_without_migration_url(replacement))
                temporary.flush()

                rendered = render_compose(Path(temporary.name))

                self.assertNotEqual(0, rendered.returncode)
                diagnostic = rendered.stderr or rendered.stdout
                self.assertIn(
                    "MIGRATION_DATABASE_URL must be explicitly configured",
                    diagnostic,
                )

    def test_no_default_service_action_invokes_adopt(self) -> None:
        for service_name, service in self.services.items():
            entrypoint = service.get("entrypoint") or []
            command = service.get("command") or []
            startup = entrypoint + command
            with self.subTest(service=service_name):
                self.assertNotIn("adopt", " ".join(startup).lower())

    def test_runbook_separates_adopt_apply_and_failure_boundaries(self) -> None:
        runbook = (ROOT / "docs/runbooks/VPS_DEPLOYMENT.md").read_text()
        adopt = (
            "docker compose --env-file .env run --rm --no-deps "
            "postgres-migrate adopt smartcoat_rd"
        )
        apply = (
            "docker compose --env-file .env run --rm --no-deps "
            "postgres-migrate apply"
        )

        self.assertIn(adopt, runbook)
        self.assertIn(apply, runbook)
        self.assertIn("An unmanaged database makes ordinary `apply` fail", runbook)
        self.assertIn("Adoption is never a default command", runbook)
        self.assertIn("MIGRATION_DATABASE_URL", runbook)
        self.assertIn("M0-R02", runbook)
        self.assertIn("`BLOCKED`", runbook)
        self.assertIn("M0-R05", runbook)


class MigrationDocumentationSafetyTests(unittest.TestCase):
    def test_real_env_configuration_validation_is_nonprinting(self) -> None:
        runbook = (ROOT / "docs/runbooks/VPS_DEPLOYMENT.md").read_text()
        example = EXAMPLE_ENV.read_text()
        safe_command = "docker compose --env-file .env config --quiet"
        real_env_config_commands = [
            line.strip()
            for line in runbook.splitlines()
            if line.strip().startswith("docker compose --env-file .env config")
        ]

        self.assertEqual([safe_command], real_env_config_commands)
        self.assertNotIn("docker compose --env-file .env config\n", runbook)
        self.assertFalse(
            any("--format" in command for command in real_env_config_commands)
        )
        self.assertFalse(
            any("--output" in command for command in real_env_config_commands)
        )
        self.assertIn("MIGRATION_DATABASE_URL", runbook)
        self.assertIn("POSTGRES_USER", runbook)
        self.assertIn("POSTGRES_PASSWORD", runbook)
        self.assertIn("Percent-encode URI-reserved characters", runbook)
        self.assertIn("MIGRATION_DATABASE_URL=", example)

    def test_example_migration_url_matches_existing_admin_credentials(self) -> None:
        example = EXAMPLE_ENV.read_text()
        required_names = {
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "MIGRATION_DATABASE_URL",
        }
        values = {
            name: value
            for line in example.splitlines()
            if line and not line.startswith("#") and "=" in line
            for name, value in (line.split("=", 1),)
            if name in required_names
        }
        self.assertEqual(required_names, set(values))

        migration_url = urlsplit(values["MIGRATION_DATABASE_URL"])
        self.assertEqual(values["POSTGRES_USER"], unquote(migration_url.username or ""))
        self.assertEqual(
            values["POSTGRES_PASSWORD"],
            unquote(migration_url.password or ""),
        )
        self.assertEqual(values["POSTGRES_DB"], migration_url.path.removeprefix("/"))


if __name__ == "__main__":
    unittest.main()
