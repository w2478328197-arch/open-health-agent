#!/usr/bin/env python3
"""Check the credential-free ghealth command contract used by OHA."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0, str(REPOSITORY_ROOT / "skills" / "open-health-agent" / "scripts")
)

from oha.ghealth_adapter import (  # noqa: E402
    CAPTURE_SPECS,
    QUERY_SPECS as SEMANTIC_QUERY_SPECS,
    SUPPORTED_CAPTURE_DATA_TYPES,
)


def command_specs() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    required: dict[tuple[str, str], set[str]] = {}
    for spec in (*SEMANTIC_QUERY_SPECS, *CAPTURE_SPECS):
        flags = required.setdefault((spec.data_type, spec.operation), set())
        if spec.use_date_range:
            flags.add("--from")
            if spec.include_to:
                flags.add("--to")
        if spec.operation == "list":
            flags.update(("--limit", "--page-token"))
        if spec.detail:
            flags.add("--detail")
    return tuple(
        (data_type, operation, tuple(sorted(flags)))
        for (data_type, operation), flags in sorted(required.items())
    )


COMMAND_SPECS = command_specs()


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

        for data_type, operation, flags in COMMAND_SPECS:
            output = run(selected, environment, "data", data_type, operation, "--help")
            require_flags(output, f"data {data_type} {operation}", flags)

    print(
        "ghealth command contract is compatible with "
        f"{len(SUPPORTED_CAPTURE_DATA_TYPES)} data types and "
        f"{len(COMMAND_SPECS)} command shapes"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    check(args.binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
