from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    for schema_path in sorted((ROOT / "packages/contracts").glob("*.schema.json")):
        jsonschema.Draft202012Validator.check_schema(json.loads(schema_path.read_text()))

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    for service_name, service in compose["services"].items():
        for port in service.get("ports", []):
            published = port.get("host_ip") if isinstance(port, dict) else str(port).split(":", 1)[0]
            if published != "127.0.0.1":
                raise SystemExit(f"{service_name} publishes a non-local port: {port}")
    if not compose["networks"]["backend"].get("internal"):
        raise SystemExit("Compose backend network must be internal")
    if compose["services"]["ocr-worker"].get("networks") != ["backend"]:
        raise SystemExit("OCR worker must remain isolated on the internal backend")
    print("Contracts and local-only Compose topology are valid.")


if __name__ == "__main__":
    main()
