#!/usr/bin/env python3
"""Check the credential-free ghealth command contract used by OHA."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


QUERY_SPECS = (
    ("steps", "daily-rollup", ("--from", "--to")),
    ("distance", "daily-rollup", ("--from", "--to")),
    ("active-energy-burned", "daily-rollup", ("--from", "--to")),
    ("active-minutes", "daily-rollup", ("--from", "--to")),
    ("daily-resting-heart-rate", "list", ("--from", "--to", "--limit", "--page-token")),
    ("daily-heart-rate-variability", "list", ("--from", "--to", "--limit", "--page-token")),
    ("daily-oxygen-saturation", "list", ("--from", "--to", "--limit", "--page-token")),
    ("daily-respiratory-rate", "list", ("--from", "--to", "--limit", "--page-token")),
    ("daily-vo2-max", "list", ("--from", "--to", "--limit", "--page-token")),
    ("weight", "list", ("--from", "--to", "--limit", "--page-token")),
    ("body-fat", "list", ("--from", "--to", "--limit", "--page-token")),
    ("height", "list", ("--from", "--to", "--limit", "--page-token")),
    ("sleep", "list", ("--from", "--to", "--limit", "--page-token", "--detail")),
    ("exercise", "list", ("--from", "--to", "--limit", "--page-token")),
)


def run(binary: Path, environment: dict[str, str], *arguments: str) -> str:
    completed = subprocess.run(
        [str(binary), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"ghealth {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def require_flags(output: str, command: str, flags: tuple[str, ...]) -> None:
    missing = [flag for flag in flags if flag not in output]
    if missing:
        raise RuntimeError(f"ghealth {command} is missing required flags: {', '.join(missing)}")


def check(binary: Path) -> None:
    selected = binary.expanduser().resolve()
    if not selected.is_file():
        raise FileNotFoundError(f"ghealth binary is unavailable: {selected}")
    with tempfile.TemporaryDirectory(prefix="oha-ghealth-contract-") as config_home:
        environment = os.environ.copy()
        environment["GHEALTH_CONFIG_DIR"] = config_home
        environment["GHEALTH_FORMAT"] = "json"

        auth_help = run(selected, environment, "auth", "status", "--help")
        require_flags(auth_help, "auth status", ("--validate",))

        profiles_help = run(selected, environment, "config", "profiles", "list", "--help")
        require_flags(profiles_help, "config profiles list", ("--format",))
        profiles = json.loads(
            run(selected, environment, "config", "profiles", "list", "--format", "json")
        )
        if not isinstance(profiles, dict) or not isinstance(profiles.get("profiles"), list):
            raise RuntimeError("ghealth config profiles list no longer returns a profiles array")

        config_help = run(selected, environment, "config", "show", "--help")
        require_flags(config_help, "config show", ("--format",))
        config = json.loads(run(selected, environment, "config", "show", "--format", "json"))
        if not isinstance(config, dict):
            raise RuntimeError("ghealth config show no longer returns a JSON object")

        for data_type, operation, flags in QUERY_SPECS:
            output = run(selected, environment, "data", data_type, operation, "--help")
            require_flags(output, f"data {data_type} {operation}", flags)

    print(f"ghealth command contract is compatible with all {len(QUERY_SPECS)} OHA query families")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    check(args.binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
