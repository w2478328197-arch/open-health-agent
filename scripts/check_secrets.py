#!/usr/bin/env python3
"""Fail when repository files appear to contain credentials or private local paths."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "htmlcov",
    "venv",
}
MAX_FILE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: Path
    label: str
    match_length: int


def patterns() -> list[tuple[str, re.Pattern[str]]]:
    slash_users = "/" + "Users/"
    slash_home = "/" + "home/"
    private_fragment = "wang" + "chen"
    token_prefixes = (
        "gh" + "p_",
        "github" + "_pat_",
        "sk" + "-proj-",
        "sk" + "-",
        "ya" + "29.",
        "xo" + "xb-",
        "xo" + "xp-",
    )
    google_oauth_client_secret_prefix = "GOC" + "SPX-"
    google_oauth_refresh_token_prefix = "1" + "//"
    return [
        (
            "credential-shaped token",
            re.compile(
                rf"(?:{'|'.join(re.escape(prefix) for prefix in token_prefixes)})[A-Za-z0-9._-]{{16,}}"
            ),
        ),
        (
            "Google API key",
            re.compile(r"AI" + r"za[0-9A-Za-z_-]{25,}"),
        ),
        (
            "Google OAuth client secret",
            re.compile(re.escape(google_oauth_client_secret_prefix) + r"[A-Za-z0-9_-]{16,}"),
        ),
        (
            "Google OAuth refresh token",
            re.compile(re.escape(google_oauth_refresh_token_prefix) + r"[A-Za-z0-9._~-]{20,}"),
        ),
        (
            "authorization bearer value",
            re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/-]{20,}"),
        ),
        (
            "assigned secret value",
            re.compile(
                r"(?i)['\"]?(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password)"
                r"['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._~+/-]{20,}"
            ),
        ),
        (
            "private key block",
            re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        ),
        (
            "private macOS/Linux health path",
            re.compile(
                rf"(?:{re.escape(slash_users)}[^/\s]+|{re.escape(slash_home)}[^/\s]+)"
                r"/(?:\.hermes|\.open-health-agent|Library/Mobile Documents)"
            ),
        ),
        (
            "private Windows health path",
            re.compile(
                r"[A-Za-z]:\\Users\\[^\\\s]+\\"
                r"(?:\.hermes|\.open-health-agent|AppData\\Local\\open-health-agent)",
                re.IGNORECASE,
            ),
        ),
        (
            "known private workstation path",
            re.compile(
                rf"(?:{re.escape(slash_users)}|[A-Za-z]:\\Users\\){re.escape(private_fragment)}",
                re.IGNORECASE,
            ),
        ),
        (
            "possible mainland China mobile number",
            re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        ),
    ]


def repository_files(root: Path = ROOT) -> list[Path]:
    if (root / ".git").exists():
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if completed.returncode == 0:
            return sorted(
                root / raw.decode("utf-8", errors="surrogateescape")
                for raw in completed.stdout.split(b"\0")
                if raw
            )
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
    )


def text_sections(path: Path) -> Iterable[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return
        if path.suffix.lower() == ".xlsx":
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if member.endswith((".xml", ".rels")):
                        yield archive.read(member).decode("utf-8", errors="ignore")
            return
        raw = path.read_bytes()
    except (OSError, zipfile.BadZipFile):
        return
    if b"\0" in raw[:4096]:
        return
    yield raw.decode("utf-8", errors="ignore")


def scan(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    checks = patterns()
    for path in repository_files(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            findings.append(
                Finding(path.relative_to(root), "file exceeds privacy scan size limit", size)
            )
            continue
        for text in text_sections(path):
            for label, pattern in checks:
                for match in pattern.finditer(text):
                    findings.append(
                        Finding(path.relative_to(root), label, len(match.group(0)))
                    )
                    if len(findings) >= 100:
                        return findings
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a repository or release tree for secrets and private paths")
    parser.add_argument("--root", type=Path, default=ROOT, help="Tree to scan (default: repository root)")
    arguments = parser.parse_args(argv)
    root = arguments.root.expanduser().resolve()
    findings = scan(root)
    if findings:
        print("Secret/privacy scan failed:")
        for finding in findings:
            print(f"- {finding.path}: {finding.label} ({finding.match_length} characters; value redacted)")
        return 1
    print(f"Secret/privacy scan passed ({len(repository_files(root))} repository files checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
