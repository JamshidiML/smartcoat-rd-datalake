#!/usr/bin/env python3
"""Opt-in isolated pinned-MinIO acceptance for exact-version retention."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
LEGAL_HARNESS = ROOT / "infra/minio/tests/live_legal_hold_mediation_acceptance.py"
EXPECTED_LEGAL_HARNESS_SHA256 = "0d50e5c0b3420a2e015b2e2bdfc78a02533f2326dc887248681b87cc62182d0e"
CONFIRM_FLAG = "--confirm-disposable-synthetic-retention-enforcement-run"
RETENTION_IMAGE_FLAG = "--retention-image"
LEGAL_IMAGE_FLAG = "--legal-hold-image"
PASS = "PASS_RETENTION_ENFORCEMENT_READY"
FAIL = "FAIL_RETENTION_ENFORCEMENT_READY"
BLOCKED = "BLOCKED_ISOLATION"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_legal_harness():
    if not LEGAL_HARNESS.is_file() or sha256(LEGAL_HARNESS) != EXPECTED_LEGAL_HARNESS_SHA256:
        raise RuntimeError("accepted legal-hold harness boundary changed")
    spec = importlib.util.spec_from_file_location("accepted_legal_hold_harness", LEGAL_HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("accepted legal-hold harness could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


legal = load_legal_harness()


UPLOAD_PROGRAM = r'''
import io, json, os
from minio import Minio

client = Minio('minio:9000', access_key=os.environ['ROOT_USER'],
               secret_key=os.environ['ROOT_PASSWORD'], secure=False)
targets = []
for retention_class in ('permanent', 'long_term_10y', 'short_90d'):
    for kind, bucket in (
        ('ORIGINAL', 'sc-rd-bronze-originals'),
        ('MANIFEST', 'sc-rd-bronze-manifests'),
    ):
        key = f'rd/retention/{retention_class}/{kind.lower()}.bin'
        body = f'synthetic-{retention_class}-{kind}'.encode()
        result = client.put_object(bucket, key, io.BytesIO(body), len(body))
        stat = client.stat_object(bucket, key, version_id=result.version_id)
        targets.append({
            'bucket_name': bucket,
            'object_key': key,
            'object_version_id': result.version_id,
            'object_kind': kind,
            'retention_class': retention_class,
            'storage_last_modified': stat.last_modified.isoformat(),
        })
print(json.dumps(targets, sort_keys=True))
'''


RETENTION_DENIAL_TARGET_PROGRAM = r'''
import io, json, os
from minio import Minio

client = Minio('minio:9000', access_key=os.environ['ROOT_USER'],
               secret_key=os.environ['ROOT_PASSWORD'], secure=False)
bucket = 'sc-rd-bronze-originals'
key = 'rd/retention/negative/retention-api-denial.bin'
body = b'synthetic-retention-api-denial'
result = client.put_object(bucket, key, io.BytesIO(body), len(body))
print(json.dumps({
    'bucket_name': bucket,
    'object_key': key,
    'object_version_id': result.version_id,
    'object_kind': 'ORIGINAL',
    'retention_class': 'permanent',
}, sort_keys=True))
'''


ENFORCE_PROGRAM = r'''
import json, os
from types import MappingProxyType
from minio import Minio
import retention_policy
from retention_policy import CategoryRule, retain_until_for
from retention_enforcement import (
    ExactVersionRetentionEnforcer, ExactVersionTarget,
    HttpLegalHoldMediator, MinioExactVersionRetentionStorage,
)

# Disposable-only approved-rule fixture exercises the otherwise dormant
# canonical long_term_10y storage path without changing repository policy.
retention_policy._RULES = MappingProxyType({
    **dict(retention_policy.approved_rules()),
    'SYNTHETIC_LONG_TERM_10Y': CategoryRule(
        'SYNTHETIC_LONG_TERM_10Y', 'long_term_10y',
        'Disposable exact-version acceptance', 'synthetic_non_company_data'),
})
client = Minio('minio:9000', access_key=os.environ['ACCESS_KEY'],
               secret_key=os.environ['SECRET_KEY'], secure=False)
service = ExactVersionRetentionEnforcer(
    MinioExactVersionRetentionStorage(client),
    HttpLegalHoldMediator(
        'http://legal-hold-applier:8090', os.environ['CALL_TOKEN']),
)
categories = {
    'permanent': 'LAB_NOTE',
    'long_term_10y': 'SYNTHETIC_LONG_TERM_10Y',
    'short_90d': 'PLATFORM_OPERATIONAL_LOG',
}
results = []
for index, item in enumerate(json.loads(os.environ['TARGETS'])):
    try:
        evidence = service.enforce(
            target=ExactVersionTarget(
                item['bucket_name'], item['object_key'],
                item['object_version_id'], item['object_kind']),
            retention_assignment_id=f'synthetic-assignment-{index}',
            data_category=categories[item['retention_class']],
            retention_class=item['retention_class'],
            retention_policy_version=retention_policy.RETENTION_POLICY_VERSION,
            enforced_by='synthetic_live_acceptance',
        )
    except Exception as exc:
        results.append({
            'enforcement_verification_result': 'FAILURE',
            'retention_class': item['retention_class'],
            'object_kind': item['object_kind'],
            'error_type': type(exc).__name__,
            'error_code': getattr(exc, 'code', None),
        })
        continue
    row = evidence.as_record()
    row['policy_retain_until_utc'] = retain_until_for(
        item['retention_class'], evidence.accepted_storage_at_utc)
    results.append(row)
print(json.dumps(results, default=str, sort_keys=True))
'''


NEGATIVE_PROGRAM = r'''
import json, os
from minio import Minio
import retention_policy
from retention_enforcement import (
    ExactVersionRetentionEnforcer, ExactVersionTarget,
    HttpLegalHoldMediator, MinioExactVersionRetentionStorage,
)

item = json.loads(os.environ['TARGET'])
variant = os.environ['VARIANT']
bucket = item['bucket_name']
key = item['object_key']
version = item['object_version_id']
policy_version = retention_policy.RETENTION_POLICY_VERSION
mediator = 'http://legal-hold-applier:8090'
if variant == 'unauthorized_bucket': bucket = 'sc-rd-ocr-artifacts'
if variant == 'wrong_bucket': bucket = 'sc-rd-bronze-manifests'
if variant == 'malformed_metadata': key = 'latest'
if variant == 'malformed_version': version = ''
if variant == 'missing_version': version = 'synthetic-missing-version'
if variant == 'unknown_policy': policy_version = 'unknown-policy'
if variant in {'mediator_failure', 'interrupted_execution'}:
    mediator = 'http://127.0.0.1:1'
client = Minio('minio:9000', access_key=os.environ['ACCESS_KEY'],
               secret_key=os.environ['SECRET_KEY'], secure=False)
class ForbiddenMediator:
    def apply_on(self, target):
        raise RuntimeError('MEDIATOR_MUST_NOT_BE_REACHED')
    def read_status(self, target):
        raise RuntimeError('MEDIATOR_MUST_NOT_BE_REACHED')
hold_mediator = (
    ForbiddenMediator()
    if variant == 'retention_api_denial'
    else HttpLegalHoldMediator(mediator, os.environ['CALL_TOKEN'], 0.5)
)
service = ExactVersionRetentionEnforcer(
    MinioExactVersionRetentionStorage(client),
    hold_mediator)
try:
    service.enforce(
        target=ExactVersionTarget(bucket, key, version, item['object_kind']),
        retention_assignment_id='synthetic-negative-assignment',
        data_category='LAB_NOTE', retention_class='permanent',
        retention_policy_version=policy_version,
        enforced_by='synthetic_live_acceptance')
except Exception as exc:
    print(json.dumps({'variant': variant, 'failed_closed': True,
                      'error_type': type(exc).__name__,
                      'error_code': getattr(exc, 'code', None)}, sort_keys=True))
else:
    print(json.dumps({'variant': variant, 'failed_closed': False}, sort_keys=True))
'''


READBACK_MISMATCH_PROGRAM = r'''
import json, os
from dataclasses import replace
from minio import Minio
import retention_policy
from retention_enforcement import (
    ExactVersionRetentionEnforcer, ExactVersionTarget,
    HttpLegalHoldMediator, MinioExactVersionRetentionStorage,
)
item = json.loads(os.environ['TARGET'])
base = MinioExactVersionRetentionStorage(Minio(
    'minio:9000', access_key=os.environ['ACCESS_KEY'],
    secret_key=os.environ['SECRET_KEY'], secure=False))
class Mismatch:
    def __init__(self): self.reads = 0
    def stat_exact(self, target): return base.stat_exact(target)
    def set_retention_exact(self, target, until): return base.set_retention_exact(target, until)
    def get_retention_exact(self, target):
        value = base.get_retention_exact(target)
        self.reads += 1
        if self.reads >= 2 and value is not None:
            return replace(value, retain_until_utc=value.retain_until_utc.replace(year=2020))
        return value
try:
    ExactVersionRetentionEnforcer(
        Mismatch(), HttpLegalHoldMediator(
            'http://legal-hold-applier:8090', os.environ['CALL_TOKEN'])
    ).enforce(
        target=ExactVersionTarget(item['bucket_name'], item['object_key'],
                                  item['object_version_id'], item['object_kind']),
        retention_assignment_id='synthetic-mismatch', data_category='LAB_NOTE',
        retention_class='permanent',
        retention_policy_version=retention_policy.RETENTION_POLICY_VERSION,
        enforced_by='synthetic_live_acceptance')
except Exception as exc:
    print(json.dumps({'failed_closed': True, 'error_code': getattr(exc, 'code', None)}, sort_keys=True))
else:
    print(json.dumps({'failed_closed': False}, sort_keys=True))
'''


def run_python(
    project: str,
    network: str,
    image: str,
    program: str,
    environment: dict[str, str],
) -> dict[str, Any] | list[dict[str, Any]]:
    command = [
        "docker", "run", "--rm", "--pull=never",
        "--name", f"{project}-python-{secrets.token_hex(3)}",
        "--label", f"{legal.LABEL}={project}", "--network", network,
    ]
    for name in environment:
        command.extend(["--env", name])
    command.extend(["--entrypoint", "python", image, "-c", program])
    result = legal.run(command, environment=environment)
    return json.loads(result.stdout)


def scenario(name: str, retention_image: str, legal_image: str) -> dict[str, Any]:
    project = f"m0hold-{secrets.token_hex(6)}"
    backend = f"{project}-backend"
    edge = f"{project}-edge"
    volume = f"{project}-data"
    minio_name = f"{project}-minio"
    applier_name = f"{project}-applier"
    initial = legal.inventory()
    root = {"ROOT_USER": f"root{secrets.token_hex(8)}", "ROOT_PASSWORD": secrets.token_hex(24)}
    app = {"ACCESS_KEY": f"app{secrets.token_hex(6)}", "SECRET_KEY": secrets.token_hex(24)}
    denied = {"ACCESS_KEY": f"denied{secrets.token_hex(6)}", "SECRET_KEY": secrets.token_hex(24)}
    mediator = {"ACCESS_KEY": f"mediator{secrets.token_hex(6)}", "SECRET_KEY": secrets.token_hex(24)}
    call_token = secrets.token_urlsafe(48)
    server = legal.image_id(legal.SERVER_REF)
    mc_image = legal.image_id(legal.MC_REF)
    retention_image = legal.image_id(retention_image)
    legal_image = legal.image_id(legal_image)
    evidence: dict[str, Any] = {
        "scenario": name,
        "project": project,
        "images": {
            "server": server,
            "mc": mc_image,
            "retention": retention_image,
            "legal_hold": legal_image,
        },
        "initial_inventory": legal.inventory_evidence(initial),
    }
    constructed = False
    stage = "construct_disposable_storage"
    try:
        legal.docker("network", "create", "--internal", "--label", f"{legal.LABEL}={project}", backend)
        constructed = True
        legal.docker("network", "create", "--label", f"{legal.LABEL}={project}", edge)
        legal.docker("volume", "create", "--label", f"{legal.LABEL}={project}", volume)
        legal.run([
            "docker", "run", "-d", "--pull=never", "--name", minio_name,
            "--label", f"{legal.LABEL}={project}", "--network", backend,
            "--network-alias", "minio", "--env", "MINIO_ROOT_USER", "--env",
            "MINIO_ROOT_PASSWORD", "--mount", f"type=volume,src={volume},dst=/data",
            server, "server", "/data", "--console-address", ":9001",
        ], environment={"MINIO_ROOT_USER": root["ROOT_USER"], "MINIO_ROOT_PASSWORD": root["ROOT_PASSWORD"]})
        legal.wait_for_minio(project, backend, mc_image, root)

        default_retention = ""
        if name == "upgraded_volume":
            default_retention = """
mc retention set --quiet --default COMPLIANCE 365d local/sc-rd-bronze-originals
mc retention set --quiet --default COMPLIANCE 365d local/sc-rd-bronze-manifests
"""
        stage = "provision_buckets_and_identities"
        provision = legal.alias_script("ROOT_USER", "ROOT_PASSWORD", f"""
mc mb --quiet --with-lock local/sc-rd-bronze-originals
mc mb --quiet --with-lock local/sc-rd-bronze-manifests
mc mb --quiet local/sc-rd-ocr-artifacts
mc version enable --quiet local/sc-rd-bronze-originals
mc version enable --quiet local/sc-rd-bronze-manifests
{default_retention}
mc admin policy create local app-retention /control/policies/app-bronze-write.json >/dev/null
mc admin policy create local denied-retention /control/policies/reviewer-read.json >/dev/null
mc admin policy create local legal-hold-applier /control/policies/legal-hold-applier.json >/dev/null
mc admin user add local "$APP_ACCESS_KEY" "$APP_SECRET_KEY" >/dev/null
mc admin user add local "$DENIED_ACCESS_KEY" "$DENIED_SECRET_KEY" >/dev/null
mc admin user add local "$MEDIATOR_ACCESS_KEY" "$MEDIATOR_SECRET_KEY" >/dev/null
mc admin policy attach local app-retention --user "$APP_ACCESS_KEY" >/dev/null
mc admin policy attach local denied-retention --user "$DENIED_ACCESS_KEY" >/dev/null
mc admin policy attach local legal-hold-applier --user "$MEDIATOR_ACCESS_KEY" >/dev/null
""")
        legal.mc(
            project, backend, mc_image, provision,
            {
                **root,
                "APP_ACCESS_KEY": app["ACCESS_KEY"], "APP_SECRET_KEY": app["SECRET_KEY"],
                "DENIED_ACCESS_KEY": denied["ACCESS_KEY"], "DENIED_SECRET_KEY": denied["SECRET_KEY"],
                "MEDIATOR_ACCESS_KEY": mediator["ACCESS_KEY"], "MEDIATOR_SECRET_KEY": mediator["SECRET_KEY"],
            },
        )

        stage = "upload_exact_versions"
        targets = run_python(project, backend, retention_image, UPLOAD_PROGRAM, root)
        if not isinstance(targets, list) or len(targets) != 6:
            raise legal.AcceptanceFailure("six class/kind exact-version fixtures were not created")

        stage = "start_legal_hold_mediator"
        mediator_environment = {
            "MINIO_HOLD_APPLIER_ENDPOINT": "minio:9000",
            "MINIO_HOLD_APPLIER_SECURE": "false",
            "MINIO_HOLD_APPLIER_ACCESS_KEY": mediator["ACCESS_KEY"],
            "MINIO_HOLD_APPLIER_SECRET_KEY": mediator["SECRET_KEY"],
            "LEGAL_HOLD_APPLIER_CALL_TOKEN": call_token,
            "LEGAL_HOLD_APPLIER_PORT": "8090",
        }
        command = [
            "docker", "run", "-d", "--pull=never", "--name", applier_name,
            "--label", f"{legal.LABEL}={project}", "--network", backend,
            "--network-alias", "legal-hold-applier", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        ]
        for key in mediator_environment:
            command.extend(["--env", key])
        command.append(legal_image)
        legal.run(command, environment=mediator_environment)
        stage = "wait_for_legal_hold_mediator"
        for _ in range(30):
            try:
                ready = legal.http_request(
                    project, backend, retention_image,
                    {"bucket": "unapproved", "object_key": "rd/probe", "version_id": "probe"},
                    call_token=call_token,
                    context="api",
                )
            except legal.AcceptanceFailure:
                time.sleep(1)
                continue
            if ready.get("status") == 400:
                break
            time.sleep(1)
        else:
            raise legal.EnvironmentBlocked("retention mediator did not become ready")

        stage = "verify_network_isolation"
        networks = json.loads(legal.docker(
            "inspect", applier_name, "--format", "{{json .NetworkSettings.Networks}}"
        ).stdout)
        ports = legal.docker(
            "inspect", applier_name, "--format", "{{json .HostConfig.PortBindings}}"
        ).stdout.strip()
        edge_probe = legal.http_request(
            project, edge, retention_image,
            {"bucket": "sc-rd-bronze-originals", "object_key": "rd/x", "version_id": "v"},
            expect_reachable=False,
        )
        if set(networks) != {backend} or ports not in {"null", "{}"} or edge_probe["reachable"]:
            raise legal.AcceptanceFailure("retention mediator escaped backend-only isolation")

        permanent_target = next(
            item for item in targets if item["retention_class"] == "permanent"
        )
        unauthenticated = legal.http_request(
            project,
            backend,
            retention_image,
            {
                "bucket": permanent_target["bucket_name"],
                "object_key": permanent_target["object_key"],
                "version_id": permanent_target["object_version_id"],
            },
            context="backend-peer",
        )
        if unauthenticated.get("status") != 401:
            raise legal.AcceptanceFailure("unauthenticated retention mediation was not denied")

        stage = "enforce_all_classes_and_kinds"
        results = run_python(
            project, backend, retention_image, ENFORCE_PROGRAM,
            {
                **app,
                "CALL_TOKEN": call_token,
                "TARGETS": json.dumps(targets, separators=(",", ":")),
            },
        )
        if not isinstance(results, list) or len(results) != 6:
            raise legal.AcceptanceFailure("class/kind enforcement result count is wrong")
        by_class: dict[str, list[dict[str, Any]]] = {}
        for row in results:
            if row["enforcement_verification_result"] != "SUCCESS":
                raise legal.AcceptanceFailure(
                    "exact-version enforcement failed: "
                    f"class={row['retention_class']} kind={row['object_kind']} "
                    f"error_type={row['error_type']} error_code={row['error_code']}"
                )
            by_class.setdefault(row["retention_class"], []).append(row)
            if (
                row["observed_object_version_id"] != row["object_version_id"]
                or row["observed_retention_mode"] != "COMPLIANCE"
                or row["observed_retain_until_utc"] < row["policy_retain_until_utc"]
            ):
                raise legal.AcceptanceFailure("exact-version retention readback did not match")
        if set(by_class) != {"permanent", "long_term_10y", "short_90d"}:
            raise legal.AcceptanceFailure("canonical class coverage is incomplete")
        if any(row["observed_legal_hold_status"] != "ON" for row in by_class["permanent"]):
            raise legal.AcceptanceFailure("permanent exact version lacks legal hold ON")
        if name == "fresh_volume" and any(
            row["requested_retain_until_utc"] != row["policy_retain_until_utc"]
            for row in results
        ):
            raise legal.AcceptanceFailure("fresh exact class deadline was not applied exactly")

        stage = "run_fail_closed_negatives"
        permanent = next(row for row in targets if row["retention_class"] == "permanent")
        negative_results: dict[str, Any] = {}
        for variant in (
            "unauthorized_bucket", "wrong_bucket", "malformed_metadata",
            "malformed_version", "missing_version", "unknown_policy",
            "mediator_failure", "interrupted_execution",
        ):
            stage = f"run_fail_closed_negative:{variant}"
            result = run_python(
                project, backend, retention_image, NEGATIVE_PROGRAM,
                {
                    **app,
                    "CALL_TOKEN": call_token,
                    "TARGET": json.dumps(permanent),
                    "VARIANT": variant,
                },
            )
            if not isinstance(result, dict) or not result.get("failed_closed"):
                raise legal.AcceptanceFailure(f"negative {variant} did not fail closed")
            negative_results[variant] = result
        stage = "run_fail_closed_negative:retention_api_denial_target"
        denial_target = run_python(
            project, backend, retention_image,
            RETENTION_DENIAL_TARGET_PROGRAM, root,
        )
        if not isinstance(denial_target, dict):
            raise legal.AcceptanceFailure("retention API denial target is invalid")
        stage = "run_fail_closed_negative:retention_api_denial"
        denied_result = run_python(
            project, backend, retention_image, NEGATIVE_PROGRAM,
            {
                **denied,
                "TARGET": json.dumps(denial_target),
                "VARIANT": "retention_api_denial",
            },
        )
        if not isinstance(denied_result, dict) or not denied_result.get("failed_closed"):
            raise legal.AcceptanceFailure("retention API denial did not fail closed")
        negative_results["retention_api_denial"] = denied_result
        stage = "run_fail_closed_negative:readback_mismatch"
        mismatch = run_python(
            project, backend, retention_image, READBACK_MISMATCH_PROGRAM,
            {**app, "CALL_TOKEN": call_token, "TARGET": json.dumps(permanent)},
        )
        if not isinstance(mismatch, dict) or not mismatch.get("failed_closed"):
            raise legal.AcceptanceFailure("readback mismatch did not fail closed")
        negative_results["readback_mismatch"] = mismatch

        stage = "verify_protected_version_delete_denial"
        deletion = legal.mc(
            project, backend, mc_image,
            legal.alias_script(
                "ROOT_USER", "ROOT_PASSWORD",
                'mc rm --force --version-id "$VERSION_ID" '
                'local/sc-rd-bronze-originals/rd/retention/permanent/original.bin >/dev/null 2>&1',
            ),
            {**root, "VERSION_ID": permanent["object_version_id"]},
            check=False,
        )
        if deletion.returncode == 0:
            raise legal.AcceptanceFailure("protected exact-version deletion unexpectedly succeeded")
        stage = "verify_lifecycle_absence"
        lifecycle_rows: list[dict[str, Any]] = []
        for bucket in ("sc-rd-bronze-originals", "sc-rd-bronze-manifests"):
            check = legal.mc(
                project, backend, mc_image,
                legal.alias_script(
                    "ROOT_USER", "ROOT_PASSWORD",
                    f"mc ilm rule ls --json local/{bucket}",
                ),
                root,
                check=False,
            )
            for line in check.stdout.splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if any(
                    key in value
                    for key in (
                        "id", "ruleId", "expiration", "noncurrentVersionExpiration",
                        "transition", "noncurrentVersionTransition",
                    )
                ):
                    lifecycle_rows.append(value)
        if lifecycle_rows:
            raise legal.AcceptanceFailure("an unauthorized lifecycle rule exists")

        evidence.update({
            "network": {
                "mediator_networks": sorted(networks),
                "host_ports": None if ports == "null" else {},
                "edge_reachable": False,
                "backend_reachable": True,
            },
            "caller_authentication": {
                "authenticated_retention_caller": True,
                "unauthenticated_backend_status": unauthenticated["status"],
                "unauthenticated_mutation": False,
            },
            "class_results": {
                name: {
                    "count": len(rows),
                    "kinds": sorted(row["object_kind"] for row in rows),
                    "all_exact_version": True,
                    "all_compliance": True,
                    "legal_hold": sorted({row["observed_legal_hold_status"] for row in rows}),
                    "policy_deadline_exact_on_fresh": (
                        all(row["requested_retain_until_utc"] == row["policy_retain_until_utc"] for row in rows)
                        if evidence["scenario"] == "fresh_volume" else None
                    ),
                    "monotonic_floor": all(
                        row["observed_retain_until_utc"] >= row["policy_retain_until_utc"] for row in rows
                    ),
                }
                for name, rows in by_class.items()
            },
            "negative_results": negative_results,
            "explicit_protected_version_delete": "AccessDenied",
            "lifecycle_rules": 0,
        })
        stage = "scenario_complete"
        return evidence
    except Exception as exc:
        raise legal.AcceptanceFailure(f"stage={stage}: {exc}") from exc
    finally:
        if constructed:
            legal.cleanup(project)
        final = legal.inventory()
        evidence["final_inventory"] = legal.inventory_evidence(final)
        evidence["inventory_equal"] = final == initial
        evidence["cleanup_remaining"] = legal.owned_resources(project)
        if final != initial:
            raise legal.AcceptanceFailure("pre-existing Docker inventory changed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(CONFIRM_FLAG, action="store_true")
    parser.add_argument(RETENTION_IMAGE_FLAG)
    parser.add_argument(LEGAL_IMAGE_FLAG)
    args = parser.parse_args(argv)
    if not getattr(args, CONFIRM_FLAG[2:].replace("-", "_")):
        print(json.dumps({"classification": BLOCKED, "authorized": False}))
        print(BLOCKED)
        return 2
    retention_image = getattr(args, RETENTION_IMAGE_FLAG[2:].replace("-", "_"))
    legal_image = getattr(args, LEGAL_IMAGE_FLAG[2:].replace("-", "_"))
    if not retention_image or not legal_image:
        print(json.dumps({"classification": BLOCKED, "reason": "immutable images required"}))
        print(BLOCKED)
        return 2
    scenarios: list[dict[str, Any]] = []
    try:
        for name in ("fresh_volume", "upgraded_volume"):
            scenarios.append(scenario(name, retention_image, legal_image))
    except Exception as exc:
        print(json.dumps({"classification": FAIL, "reason": str(exc), "scenarios": scenarios}, sort_keys=True))
        print(FAIL)
        return 1
    print(json.dumps({"classification": PASS, "scenarios": scenarios}, sort_keys=True))
    print(PASS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
