#!/usr/bin/env python3
"""Opt-in isolated live acceptance for the legal-hold mediation boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / "infra" / "minio"
SERVER_REF = "minio/minio:RELEASE.2025-07-23T15-54-02Z"
MC_REF = "minio/mc:RELEASE.2025-07-21T05-28-08Z"
SDK_REF = "smartcoat-rd-datalake-api:latest"
CONFIRM_FLAG = "--confirm-disposable-synthetic-legal-hold-mediation-run"
APPLIER_IMAGE_FLAG = "--legal-hold-applier-image"
PASS = "PASS_LEGAL_HOLD_AUTHORITY_READY_PRODUCTION_IMAGE"
FAIL = "FAIL_LEGAL_HOLD_AUTHORITY_READY"
LABEL = "smartcoat.legal-hold-mediation.project"
PROJECT = re.compile(r"^m0hold-[0-9a-f]{12}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class AcceptanceFailure(RuntimeError):
    pass


class EnvironmentBlocked(AcceptanceFailure):
    pass


def run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    clean_environment = os.environ.copy()
    if environment:
        clean_environment.update(environment)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=clean_environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvironmentBlocked(f"command boundary unavailable: {command[0]}") from exc
    if check and result.returncode != 0:
        raise AcceptanceFailure(
            f"sanitized command failed: executable={command[0]} exit={result.returncode}"
        )
    return result


def docker(*arguments: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return run(["docker", *arguments], check=check, timeout=timeout)


def image_id(reference: str) -> str:
    result = docker("image", "inspect", reference, "--format", "{{.Id}}", check=False)
    value = result.stdout.strip()
    if result.returncode != 0 or not IMAGE_ID.fullmatch(value):
        raise EnvironmentBlocked(f"required local image is unavailable: {reference}")
    return value


def production_image_contract(reference: str) -> dict[str, Any]:
    inspected = docker("image", "inspect", reference, check=False)
    if inspected.returncode != 0:
        raise EnvironmentBlocked("production legal-hold image is unavailable")
    values = json.loads(inspected.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise AcceptanceFailure("production legal-hold image identity is ambiguous")
    config = values[0].get("Config", {})
    version = docker(
        "run", "--rm", "--pull=never", "--entrypoint", "python", reference,
        "-c", "import minio; print(minio.__version__)",
    ).stdout.strip()
    if version != "7.2.16":
        raise AcceptanceFailure("production legal-hold image SDK version is not pinned")
    return {
        "sdk_version": version,
        "entrypoint": config.get("Entrypoint"),
        "command": config.get("Cmd"),
        "user": config.get("User"),
        "working_directory": config.get("WorkingDir"),
    }


def inventory() -> dict[str, list[str]]:
    return {
        "containers": sorted(filter(None, docker("ps", "-aq").stdout.splitlines())),
        "networks": sorted(filter(None, docker("network", "ls", "-q").stdout.splitlines())),
        "volumes": sorted(filter(None, docker("volume", "ls", "-q").stdout.splitlines())),
    }


def inventory_evidence(value: dict[str, list[str]]) -> dict[str, Any]:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "counts": {name: len(items) for name, items in value.items()},
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def owned_resources(project: str) -> dict[str, list[str]]:
    return {
        "containers": sorted(filter(None, docker(
            "ps", "-aq", "--filter", f"label={LABEL}={project}"
        ).stdout.splitlines())),
        "networks": sorted(filter(None, docker(
            "network", "ls", "-q", "--filter", f"label={LABEL}={project}"
        ).stdout.splitlines())),
        "volumes": sorted(filter(None, docker(
            "volume", "ls", "-q", "--filter", f"label={LABEL}={project}"
        ).stdout.splitlines())),
    }


def cleanup(project: str) -> None:
    resources = owned_resources(project)
    if resources["containers"]:
        docker("rm", "-f", *resources["containers"], check=False)
    resources = owned_resources(project)
    for network in resources["networks"]:
        docker("network", "rm", network, check=False)
    for volume in resources["volumes"]:
        docker("volume", "rm", volume, check=False)
    if any(owned_resources(project).values()):
        raise AcceptanceFailure("owned disposable resources survived cleanup")


def mc(
    project: str,
    network: str,
    image: str,
    script: str,
    environment: dict[str, str],
    *,
    mount_control: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    name = f"{project}-mc-{secrets.token_hex(3)}"
    command = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--label", f"{LABEL}={project}", "--network", network,
    ]
    for key in environment:
        command.extend(["--env", key])
    if mount_control:
        command.extend(["--mount", f"type=bind,src={CONTROL},dst=/control,readonly"])
    command.extend(["--entrypoint", "/bin/sh", image, "-c", script])
    return run(command, environment=environment, check=check)


def alias_script(user_key: str, secret_key: str, body: str) -> str:
    return (
        f'mc alias set --quiet local http://minio:9000 "${user_key}" "${secret_key}" '
        f'>/dev/null 2>&1 || exit 90\n{body}'
    )


def wait_for_minio(project: str, network: str, mc_image: str, root: dict[str, str]) -> None:
    script = alias_script(
        "ROOT_USER", "ROOT_PASSWORD", "mc ready local >/dev/null 2>&1"
    )
    for _ in range(60):
        result = mc(project, network, mc_image, script, root, check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise EnvironmentBlocked("owned disposable MinIO did not become ready")


def json_version(output: str) -> str:
    match = re.search(r'"(?:versionId|versionID|VersionId|VersionID)":"([^"\\]+)"', output)
    if match:
        return match.group(1)
    raise AcceptanceFailure("exact uploaded version identity was unavailable")


def http_request(
    project: str,
    network: str,
    sdk_image: str,
    payload: dict[str, Any],
    *,
    expect_reachable: bool = True,
) -> dict[str, Any]:
    script = """
import json, os, urllib.error, urllib.request
request = urllib.request.Request(
    'http://legal-hold-applier:8090/apply',
    data=os.environ['PAYLOAD'].encode(),
    headers={'Content-Type': 'application/json'},
    method='POST',
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        print(json.dumps({'reachable': True, 'status': response.status,
                          'body': json.loads(response.read())}, sort_keys=True))
except urllib.error.HTTPError as exc:
    print(json.dumps({'reachable': True, 'status': exc.code,
                      'body': json.loads(exc.read())}, sort_keys=True))
except Exception:
    print(json.dumps({'reachable': False}, sort_keys=True))
"""
    result = run(
        [
            "docker", "run", "--rm", "--pull=never",
            "--name", f"{project}-http-{secrets.token_hex(3)}",
            "--label", f"{LABEL}={project}", "--network", network,
            "--env", "PAYLOAD", "--entrypoint", "python", sdk_image,
            "-c", script,
        ],
        environment={"PAYLOAD": json.dumps(payload, separators=(",", ":"))},
    )
    value = json.loads(result.stdout)
    if bool(value.get("reachable")) != expect_reachable:
        raise AcceptanceFailure("network reachability did not match the mediation boundary")
    return value


def direct_hold_attempt(
    project: str,
    network: str,
    mc_image: str,
    credentials: dict[str, str],
    version_id: str,
    operation: str,
) -> bool:
    subcommand = "set" if operation == "ON" else "clear"
    body = (
        f'mc legalhold {subcommand} --version-id "$VERSION_ID" '
        'local/sc-rd-bronze-originals/rd/synthetic/evidence.bin >/dev/null 2>&1'
    )
    result = mc(
        project, network, mc_image,
        alias_script("ACCESS_KEY", "SECRET_KEY", body),
        {**credentials, "VERSION_ID": version_id},
        check=False,
    )
    return result.returncode != 0


def legal_hold_status(
    project: str,
    network: str,
    sdk_image: str,
    root: dict[str, str],
    version_id: str,
) -> str:
    program = """
import os
from minio import Minio
client = Minio('minio:9000', access_key=os.environ['ROOT_USER'],
               secret_key=os.environ['ROOT_PASSWORD'], secure=False)
enabled = client.is_object_legal_hold_enabled(
    'sc-rd-bronze-originals', 'rd/synthetic/evidence.bin',
    version_id=os.environ['VERSION_ID'])
print('ON' if enabled else 'OFF')
"""
    result = run(
        [
            "docker", "run", "--rm", "--pull=never",
            "--name", f"{project}-sdk-readback-{secrets.token_hex(3)}",
            "--label", f"{LABEL}={project}", "--network", network,
            "--env", "ROOT_USER", "--env", "ROOT_PASSWORD", "--env", "VERSION_ID",
            "--entrypoint", "python", sdk_image, "-c", program,
        ],
        environment={**root, "VERSION_ID": version_id},
    )
    output = result.stdout.strip()
    if output in {"ON", "OFF"}:
        return output
    raise AcceptanceFailure("exact-version legal-hold status was not recognized")


def live(applier_reference: str) -> tuple[str, dict[str, Any]]:
    project = f"m0hold-{secrets.token_hex(6)}"
    if not PROJECT.fullmatch(project):
        raise AcceptanceFailure("generated project identity is invalid")
    backend = f"{project}-backend"
    edge = f"{project}-edge"
    volume = f"{project}-data"
    minio_name = f"{project}-minio"
    applier_name = f"{project}-applier"
    initial = inventory()
    images = {
        "server": image_id(SERVER_REF),
        "mc": image_id(MC_REF),
        "sdk": image_id(SDK_REF),
        "applier": image_id(applier_reference),
    }
    applier_contract = production_image_contract(images["applier"])
    root = {"ROOT_USER": f"root{secrets.token_hex(8)}", "ROOT_PASSWORD": secrets.token_hex(24)}
    identities = {
        name: {"ACCESS_KEY": f"{name}{secrets.token_hex(6)}", "SECRET_KEY": secrets.token_hex(24)}
        for name in ("app", "ocr", "reviewer", "backup", "mediator", "breakglass")
    }
    secrets_to_hide = [*root.values(), *(value for item in identities.values() for value in item.values())]
    constructed = False
    evidence: dict[str, Any] = {
        "project": project,
        "images": images,
        "production_image_contract": applier_contract,
        "initial_inventory": inventory_evidence(initial),
    }
    try:
        docker("network", "create", "--internal", "--label", f"{LABEL}={project}", backend)
        constructed = True
        docker("network", "create", "--label", f"{LABEL}={project}", edge)
        docker("volume", "create", "--label", f"{LABEL}={project}", volume)
        run(
            [
                "docker", "run", "-d", "--pull=never", "--name", minio_name,
                "--label", f"{LABEL}={project}", "--network", backend,
                "--network-alias", "minio", "--env", "MINIO_ROOT_USER",
                "--env", "MINIO_ROOT_PASSWORD", "--mount",
                f"type=volume,src={volume},dst=/data", images["server"],
                "server", "/data", "--console-address", ":9001",
            ],
            environment={
                "MINIO_ROOT_USER": root["ROOT_USER"],
                "MINIO_ROOT_PASSWORD": root["ROOT_PASSWORD"],
            },
        )
        inspected = json.loads(docker("inspect", minio_name, "--format", "{{json .Config.Env}}").stdout)
        if not any(item == f"MINIO_ROOT_USER={root['ROOT_USER']}" for item in inspected):
            raise AcceptanceFailure("owned MinIO root configuration was not injected")
        wait_for_minio(project, backend, images["mc"], root)

        provision_environment = {
            **root,
            **{
                f"{name.upper()}_{field}": value
                for name, values in identities.items()
                if name != "breakglass"
                for field, value in values.items()
            },
        }
        provision_body = """
mc mb --quiet --with-lock local/sc-rd-bronze-originals
mc mb --quiet --with-lock local/sc-rd-bronze-manifests
mc mb --quiet local/sc-rd-ocr-artifacts
mc version enable --quiet local/sc-rd-bronze-originals
mc version enable --quiet local/sc-rd-bronze-manifests
mc version enable --quiet local/sc-rd-ocr-artifacts
mc retention set --quiet --default COMPLIANCE 1d local/sc-rd-bronze-originals
mc retention set --quiet --default COMPLIANCE 1d local/sc-rd-bronze-manifests
mc admin policy create local app-bronze-write /control/policies/app-bronze-write.json >/dev/null
mc admin policy create local ocr-worker /control/policies/ocr-worker.json >/dev/null
mc admin policy create local reviewer-read /control/policies/reviewer-read.json >/dev/null
mc admin policy create local legal-hold-applier /control/policies/legal-hold-applier.json >/dev/null
mc admin user add local "$APP_ACCESS_KEY" "$APP_SECRET_KEY" >/dev/null
mc admin user add local "$OCR_ACCESS_KEY" "$OCR_SECRET_KEY" >/dev/null
mc admin user add local "$REVIEWER_ACCESS_KEY" "$REVIEWER_SECRET_KEY" >/dev/null
mc admin user add local "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" >/dev/null
mc admin user add local "$MEDIATOR_ACCESS_KEY" "$MEDIATOR_SECRET_KEY" >/dev/null
mc admin policy attach local app-bronze-write --user "$APP_ACCESS_KEY" >/dev/null
mc admin policy attach local ocr-worker --user "$OCR_ACCESS_KEY" >/dev/null
mc admin policy attach local reviewer-read --user "$REVIEWER_ACCESS_KEY" >/dev/null
mc admin policy attach local reviewer-read --user "$BACKUP_ACCESS_KEY" >/dev/null
mc admin policy attach local legal-hold-applier --user "$MEDIATOR_ACCESS_KEY" >/dev/null
"""
        mc(
            project, backend, images["mc"],
            alias_script("ROOT_USER", "ROOT_PASSWORD", provision_body),
            provision_environment,
        )

        upload_program = """
import io, json, os
from minio import Minio
client = Minio('minio:9000', access_key=os.environ['ROOT_USER'],
               secret_key=os.environ['ROOT_PASSWORD'], secure=False)
versions = []
for body in (b'synthetic-evidence-version-one', b'synthetic-evidence-version-two'):
    result = client.put_object('sc-rd-bronze-originals',
                               'rd/synthetic/evidence.bin', io.BytesIO(body), len(body))
    versions.append(result.version_id)
print(json.dumps({'versions': versions}, sort_keys=True))
"""
        uploads = run(
            [
                "docker", "run", "--rm", "--pull=never",
                "--name", f"{project}-sdk-upload", "--label", f"{LABEL}={project}",
                "--network", backend, "--env", "ROOT_USER", "--env", "ROOT_PASSWORD",
                "--entrypoint", "python", images["sdk"], "-c", upload_program,
            ],
            environment=root,
        )
        upload_value = json.loads(uploads.stdout)
        if not isinstance(upload_value.get("versions"), list) or len(upload_value["versions"]) != 2:
            raise AcceptanceFailure("two exact synthetic object versions were not created")
        version_one, version_two = upload_value["versions"]
        if not all(isinstance(value, str) and value for value in (version_one, version_two)):
            raise AcceptanceFailure("exact synthetic object version identity was unavailable")
        if version_one == version_two:
            raise AcceptanceFailure("synthetic exact-version identities were not distinct")

        mediator_environment = {
            "MINIO_HOLD_APPLIER_ENDPOINT": "minio:9000",
            "MINIO_HOLD_APPLIER_SECURE": "false",
            "MINIO_HOLD_APPLIER_ACCESS_KEY": identities["mediator"]["ACCESS_KEY"],
            "MINIO_HOLD_APPLIER_SECRET_KEY": identities["mediator"]["SECRET_KEY"],
            "LEGAL_HOLD_APPLIER_PORT": "8090",
        }
        mediator_run = [
            "docker", "run", "-d", "--pull=never", "--name", applier_name,
            "--label", f"{LABEL}={project}", "--network", backend,
            "--network-alias", "legal-hold-applier", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        ]
        for key in mediator_environment:
            mediator_run.extend(["--env", key])
        mediator_run.append(images["applier"])
        run(mediator_run, environment=mediator_environment)

        context_names: list[str] = []
        for context in ("api", "ocr"):
            name = f"{project}-{context}-context"
            context_names.append(name)
            docker(
                "run", "-d", "--pull=never", "--name", name,
                "--label", f"{LABEL}={project}", "--network", backend,
                "--env", f"SMARTCOAT_CONTEXT={context}",
                "--entrypoint", "sleep", images["sdk"], "120",
            )

        ready = False
        for _ in range(30):
            probe = http_request(
                project, backend, images["sdk"],
                {"bucket": "unapproved", "object_key": "rd/probe", "version_id": "probe"},
            )
            if probe.get("status") == 400:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise EnvironmentBlocked("owned mediator did not become ready")

        mediator_inspect = json.loads(
            docker("inspect", applier_name, "--format", "{{json .NetworkSettings.Networks}}").stdout
        )
        port_bindings = docker(
            "inspect", applier_name, "--format", "{{json .HostConfig.PortBindings}}"
        ).stdout.strip()
        if set(mediator_inspect) != {backend} or port_bindings not in {"null", "{}"}:
            raise AcceptanceFailure("mediator escaped backend-only no-host-port isolation")
        for name in context_names:
            context_environment = json.loads(
                docker("inspect", name, "--format", "{{json .Config.Env}}").stdout
            )
            if any(item.startswith("MINIO_HOLD_APPLIER_") for item in context_environment):
                raise AcceptanceFailure("mediator credential leaked into an ordinary context")

        direct_negative: dict[str, dict[str, bool]] = {}
        for identity in ("app", "ocr", "reviewer", "backup"):
            direct_negative[identity] = {
                "on_denied": direct_hold_attempt(
                    project, backend, images["mc"], identities[identity], version_one, "ON"
                ),
                "off_denied": direct_hold_attempt(
                    project, backend, images["mc"], identities[identity], version_one, "OFF"
                ),
            }
        if not all(all(result.values()) for result in direct_negative.values()):
            raise AcceptanceFailure("an ordinary identity obtained direct legal-hold authority")

        malformed_results = {
            "status_off": http_request(project, backend, images["sdk"], {
                "bucket": "sc-rd-bronze-originals",
                "object_key": "rd/synthetic/evidence.bin",
                "version_id": version_one,
                "status": "OFF",
            }),
            "generic_proxy": http_request(project, backend, images["sdk"], {
                "action": "delete", "bucket": "sc-rd-bronze-originals",
                "object_key": "rd/synthetic/evidence.bin", "version_id": version_one,
            }),
            "unauthorized_bucket": http_request(project, backend, images["sdk"], {
                "bucket": "sc-rd-ocr-artifacts",
                "object_key": "rd/synthetic/evidence.bin", "version_id": version_one,
            }),
            "missing_version": http_request(project, backend, images["sdk"], {
                "bucket": "sc-rd-bronze-originals",
                "object_key": "rd/synthetic/evidence.bin",
            }),
            "malformed_version": http_request(project, backend, images["sdk"], {
                "bucket": "sc-rd-bronze-originals",
                "object_key": "rd/synthetic/evidence.bin", "version_id": "bad/version",
            }),
        }
        if not all(value.get("status") == 400 for value in malformed_results.values()):
            raise AcceptanceFailure("mediator accepted a non-ON or proxy-style request")

        edge_probe = http_request(
            project, edge, images["sdk"],
            {"bucket": "sc-rd-bronze-originals", "object_key": "rd/synthetic/evidence.bin", "version_id": version_one},
            expect_reachable=False,
        )
        applied = http_request(
            project, backend, images["sdk"],
            {"bucket": "sc-rd-bronze-originals", "object_key": "rd/synthetic/evidence.bin", "version_id": version_one},
        )
        if applied.get("status") != 200 or applied.get("body", {}).get("legal_hold") != "ON":
            raise AcceptanceFailure("mediator exact-version ON request did not pass")
        if legal_hold_status(project, backend, images["sdk"], root, version_one) != "ON":
            raise AcceptanceFailure("mediator ON read-back was not independently confirmed")
        if legal_hold_status(project, backend, images["sdk"], root, version_two) != "OFF":
            raise AcceptanceFailure("mediator changed a version other than the exact target")
        if not direct_hold_attempt(
            project, backend, images["mc"], identities["app"], version_one, "OFF"
        ):
            raise AcceptanceFailure("ordinary identity cleared the mediated hold")

        provision_break_glass_environment = {
            "MINIO_HOLD_ENDPOINT": "http://minio:9000",
            "MINIO_PROVISIONING_ROOT_USER": root["ROOT_USER"],
            "MINIO_PROVISIONING_ROOT_PASSWORD": root["ROOT_PASSWORD"],
            "MINIO_HOLD_BREAK_GLASS_ACCESS_KEY": identities["breakglass"]["ACCESS_KEY"],
            "MINIO_HOLD_BREAK_GLASS_SECRET_KEY": identities["breakglass"]["SECRET_KEY"],
            "LEGAL_HOLD_BREAK_GLASS_PROVISIONING_CONFIRMATION": "CONFIRM_CREATE_LEGAL_HOLD_BREAK_GLASS",
            "LEGAL_HOLD_CONTROL_ROOT": "/control",
        }
        provision_break_glass = mc(
            project, backend, images["mc"],
            "/bin/sh /control/provision_legal_hold_break_glass.sh --confirm-create-legal-hold-break-glass",
            provision_break_glass_environment,
            check=False,
        )
        if provision_break_glass.returncode != 0 or "PASS_LEGAL_HOLD_BREAK_GLASS_PROVISIONED" not in provision_break_glass.stdout:
            diagnostic = provision_break_glass.stderr.strip().splitlines()[-1] if provision_break_glass.stderr.strip() else "no diagnostic"
            raise AcceptanceFailure(f"break-glass provisioning failed: {diagnostic}")

        decision_id = f"decision-{secrets.token_hex(8)}"
        break_glass_environment = {
            "MINIO_HOLD_ENDPOINT": "minio:9000",
            "MINIO_HOLD_BREAK_GLASS_ACCESS_KEY": identities["breakglass"]["ACCESS_KEY"],
            "MINIO_HOLD_BREAK_GLASS_SECRET_KEY": identities["breakglass"]["SECRET_KEY"],
            "DECISION_ID": decision_id,
            "VERSION_ID": version_one,
        }
        break_glass = run(
            [
                "docker", "run", "--rm", "--pull=never",
                "--name", f"{project}-break-glass", "--label", f"{LABEL}={project}",
                "--network", backend, "--mount", f"type=bind,src={CONTROL},dst=/control,readonly",
                "--env", "MINIO_HOLD_ENDPOINT", "--env", "MINIO_HOLD_BREAK_GLASS_ACCESS_KEY",
                "--env", "MINIO_HOLD_BREAK_GLASS_SECRET_KEY", "--entrypoint", "python",
                images["sdk"], "/control/legal_hold_break_glass.py",
                "--decision-id", decision_id,
                "--actor", "synthetic-compliance-officer",
                "--reason", "synthetic-disposable-compliance-test",
                "--timestamp-utc", "2026-08-28T00:00:00Z",
                "--bucket", "sc-rd-bronze-originals",
                "--key", "rd/synthetic/evidence.bin",
                "--version-id", version_one,
                "--confirm", "CONFIRM_BREAK_GLASS_LEGAL_HOLD_CLEAR",
            ],
            environment=break_glass_environment,
            check=False,
        )
        if break_glass.returncode != 0:
            diagnostic = break_glass.stderr.strip().splitlines()[-1] if break_glass.stderr.strip() else "no diagnostic"
            raise AcceptanceFailure(f"break-glass clear failed: {diagnostic}")
        break_glass_evidence = json.loads(break_glass.stdout)
        if break_glass_evidence.get("classification") != "PASS_LEGAL_HOLD_BREAK_GLASS_CLEAR":
            raise AcceptanceFailure("break-glass exact-version clear did not pass")
        if legal_hold_status(project, backend, images["sdk"], root, version_one) != "OFF":
            raise AcceptanceFailure("break-glass OFF read-back was not independently confirmed")

        compliance_check = mc(
            project, backend, images["mc"],
            alias_script(
                "ROOT_USER", "ROOT_PASSWORD",
                """
mc --json retention info --version-id "$VERSION_ID" local/sc-rd-bronze-originals/rd/synthetic/evidence.bin
mc rm --force --version-id "$VERSION_ID" local/sc-rd-bronze-originals/rd/synthetic/evidence.bin >/dev/null 2>&1
""",
            ),
            {**root, "VERSION_ID": version_one},
            check=False,
        )
        if compliance_check.returncode == 0 or "COMPLIANCE" not in compliance_check.stdout.upper():
            raise AcceptanceFailure("COMPLIANCE floor did not continue to deny deletion after OFF")

        audit_check = mc(
            project, backend, images["mc"],
            alias_script(
                "ROOT_USER", "ROOT_PASSWORD",
                'mc --json ls --recursive local/sc-rd-legal-hold-audit/break-glass/"$DECISION_ID"/',
            ),
            {**root, "DECISION_ID": decision_id},
        )
        audit_rows = [line for line in audit_check.stdout.splitlines() if line.strip()]
        if len(audit_rows) != 2:
            raise AcceptanceFailure("append-only break-glass audit pair was not present")

        evidence.update({
            "network": {
                "mediator_networks": sorted(mediator_inspect),
                "host_port_bindings": None if port_bindings == "null" else {},
                "edge_peer_reachable": edge_probe["reachable"],
                "backend_peer_reachable": True,
                "minio_reachable_from_mediator": True,
            },
            "credential_isolation": {
                "api_context_has_mediator_credential": False,
                "ocr_context_has_mediator_credential": False,
                "ordinary_direct_attempts": direct_negative,
            },
            "mediator": {
                "request_contract_rejections": {
                    name: value["status"] for name, value in malformed_results.items()
                },
                "target_version": version_one,
                "other_version": version_two,
                "target_readback": "ON",
                "other_version_readback": "OFF",
                "ordinary_off_after_apply": "AccessDenied",
            },
            "break_glass": {
                "classification": break_glass_evidence["classification"],
                "decision_id": decision_id,
                "target_readback": "OFF",
                "audit_receipts": 2,
                "compliance_floor": "ACTIVE",
                "delete_after_off": "AccessDenied",
            },
        })
        return PASS, evidence
    finally:
        if constructed:
            cleanup(project)
        final = inventory()
        evidence["final_inventory"] = inventory_evidence(final)
        evidence["inventory_equal"] = final == initial
        evidence["cleanup_remaining"] = owned_resources(project)
        if final != initial:
            raise AcceptanceFailure("pre-existing Docker inventory changed")
        serialized = json.dumps(evidence, sort_keys=True)
        if any(secret and secret in serialized for secret in secrets_to_hide):
            raise AcceptanceFailure("synthetic secret escaped into evidence")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    parser.add_argument(APPLIER_IMAGE_FLAG)
    args = parser.parse_args()
    if not getattr(args, CONFIRM_FLAG[2:].replace("-", "_")):
        print(f"{FAIL}: explicit {CONFIRM_FLAG} is required")
        return 2
    applier_reference = getattr(args, APPLIER_IMAGE_FLAG[2:].replace("-", "_"))
    if not applier_reference or not IMAGE_ID.fullmatch(applier_reference):
        print(f"{FAIL}: explicit immutable {APPLIER_IMAGE_FLAG} is required")
        return 2
    try:
        classification, evidence = live(applier_reference)
    except AcceptanceFailure as exc:
        print(json.dumps({"classification": FAIL, "reason": str(exc)}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({
            "classification": FAIL,
            "reason": f"verification harness error: {type(exc).__name__}",
        }, sort_keys=True))
        return 1
    print(json.dumps(evidence, sort_keys=True))
    print(classification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
