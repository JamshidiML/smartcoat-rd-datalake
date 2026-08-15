from __future__ import annotations

import re
import subprocess
from pathlib import Path


BINARY_DATA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".pdf", ".xlsx", ".xls"}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS-style access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}


def repository_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    if not result:
        result = subprocess.run(
            ["rg", "--files", "-uu", "-g", "!.git/**"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    return [root / item for item in result]


def scan_repository(root: Path) -> list[str]:
    findings: list[str] = []
    for path in repository_files(root):
        relative = path.relative_to(root)
        if path.suffix.lower() in BINARY_DATA_EXTENSIONS:
            if relative.parts[:2] != ("fixtures", "synthetic"):
                findings.append(f"real-data file type is tracked outside synthetic fixtures: {relative}")
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError as exc:
            findings.append(f"cannot scan {relative}: {exc}")
            continue
        if relative == Path(".env.example"):
            content = "\n".join(line for line in content.splitlines() if "change-me" not in line)
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{label} detected in {relative}")
    return findings


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[3]
    problems = scan_repository(repo_root)
    if problems:
        raise SystemExit("Repository scan failed:\n" + "\n".join(problems))
    print("AT-14 repository scan passed: no secret values or real-data files detected.")
