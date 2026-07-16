from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_secret_checker():
    path = ROOT / "scripts" / "check_secrets.py"
    spec = importlib.util.spec_from_file_location("open_health_agent_secret_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_secret_patterns_detect_common_credentials_and_private_paths() -> None:
    checker = load_secret_checker()
    checks = [pattern for _, pattern in checker.patterns()]
    samples = [
        "gh" + "p_" + "A" * 36,
        "sk" + "-proj-" + "B" * 32,
        "AI" + "za" + "C" * 32,
        "GOC" + "SPX-" + "D" * 28,
        "1" + "//" + "E" * 48,
        "/" + "Users/" + "someone/.hermes/config.yaml",
        "-----BEGIN " + "PRIVATE KEY-----",
    ]
    for sample in samples:
        assert any(pattern.search(sample) for pattern in checks), sample


def test_secret_scan_detects_standard_google_oauth_json(tmp_path: Path) -> None:
    checker = load_secret_checker()
    client_secret = "GOC" + "SPX-" + "A" * 28
    refresh_token = "1" + "//" + "B" * 64
    (tmp_path / "client_secret.json").write_text(
        "{\n"
        f'  "client_secret": "{client_secret}",\n'
        f'  "refresh_token": "{refresh_token}"\n'
        "}\n",
        encoding="utf-8",
    )

    findings = checker.scan(tmp_path)
    labels = {finding.label for finding in findings}
    assert "Google OAuth client secret" in labels
    assert "Google OAuth refresh token" in labels
    assert all(finding.match_length > 0 for finding in findings)


def test_secret_scan_rejects_private_artifacts_even_when_binary_content_is_opaque(
    tmp_path: Path,
) -> None:
    samples = {
        "health.sqlite3": b"\x00SQLite synthetic",
        "voice.silk": b"\x00synthetic audio",
        "report.pdf": b"%PDF-synthetic",
        "AGENTS.md": b"synthetic private rules",
        "extra.xlsx": b"PK\x03\x04synthetic workbook",
        "meal.jpg": b"\xff\xd8synthetic image",
        "health.csv": b"synthetic,health,data\n",
        "events.jsonl": b'{"synthetic": true}\n',
        "PROFILE.JSON": b"{}",
    }
    for name, content in samples.items():
        (tmp_path / name).write_bytes(content)

    checker = load_secret_checker()
    findings = checker.scan(tmp_path)
    found_paths = {str(finding.path) for finding in findings}
    assert found_paths == set(samples)


def test_secret_scan_rejects_symbolic_links_without_following_targets(
    tmp_path: Path,
) -> None:
    target = tmp_path.parent / "synthetic-private-target.bin"
    target.write_bytes(b"\x00opaque content")
    link = tmp_path / "linked-artifact"
    try:
        link.symlink_to(target)
        checker = load_secret_checker()
        findings = checker.scan(tmp_path)
        assert [(str(item.path), item.label) for item in findings] == [
            ("linked-artifact", "symbolic link is not allowed in the release tree")
        ]
    finally:
        target.unlink(missing_ok=True)


def test_current_repository_passes_secret_scan() -> None:
    checker = load_secret_checker()
    assert checker.scan() == []
