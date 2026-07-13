#!/usr/bin/env python3
"""Install Open Health Agent without touching credentials or private data by default."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = PROJECT_ROOT / "skills" / "open-health-agent"
SKILL_NAME = "open-health-agent"
WRAPPER_MARKER = "# Managed by the Open Health Agent installer."
LOCK_TIMEOUT_SECONDS = 30.0


class InstallError(RuntimeError):
    """A safe, user-actionable installation failure."""


@dataclass(frozen=True)
class Target:
    label: str
    skills_dir: Path

    @property
    def destination(self) -> Path:
        return self.skills_dir.expanduser().resolve() / SKILL_NAME


def log(message: str) -> None:
    print(message, flush=True)


def display_command(command: Iterable[object]) -> str:
    return shlex.join(str(part) for part in command)


def run(command: list[str], *, dry_run: bool, cwd: Path | None = None) -> None:
    prefix = f"(in {cwd}) " if cwd else ""
    log(f"+ {prefix}{display_command(command)}")
    if dry_run:
        return
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise InstallError(f"command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"command failed with exit code {exc.returncode}: {command[0]}") from exc


def known_targets() -> dict[str, Target]:
    home = Path.home()
    hermes_home = Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    claude_home = Path(os.environ.get("CLAUDE_HOME", home / ".claude")).expanduser()
    return {
        "hermes": Target("Hermes", hermes_home / "skills"),
        "codex": Target("Codex", codex_home / "skills"),
        "claude": Target("Claude", claude_home / "skills"),
    }


def agent_home(target: Target) -> Path:
    return target.skills_dir.expanduser().parent


def select_targets(agent_names: list[str], custom_dirs: list[Path]) -> list[Target]:
    known = known_targets()
    requested = list(agent_names)
    if not requested and not custom_dirs:
        requested = ["auto"]

    selected: list[Target] = []
    expanded: list[str] = []
    for name in requested:
        if name == "all":
            expanded.extend(known)
        elif name == "auto":
            detected = [name for name, target in known.items() if agent_home(target).exists()]
            expanded.extend(detected)
        else:
            expanded.append(name)

    for name in expanded:
        selected.append(known[name])
    selected.extend(Target(f"custom:{path.expanduser()}", path) for path in custom_dirs)

    unique: list[Target] = []
    seen: set[Path] = set()
    for target in selected:
        canonical = target.skills_dir.expanduser().resolve()
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(target)
    if not unique:
        raise InstallError(
            "no Agent installation was detected; use --agent hermes, --agent codex, "
            "--agent claude, or --skill-dir PATH"
        )
    return unique


def ignored(relative: Path) -> bool:
    return any(part in {"__pycache__", ".DS_Store"} for part in relative.parts) or relative.suffix in {
        ".pyc",
        ".pyo",
    }


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return ""
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ignored(relative):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
            digest.update(b"\0")
            continue
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def copy_filter(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in {"__pycache__", ".DS_Store"} or name.endswith((".pyc", ".pyo"))}


def path_exists(path: Path) -> bool:
    """Return true for ordinary paths and broken symlinks."""

    return path.exists() or path.is_symlink()


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def unique_sibling(parent: Path, prefix: str) -> Path:
    """Choose a collision-resistant sibling name without weakening rollback safety."""

    for _attempt in range(100):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        candidate = parent / f"{prefix}{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
        if not path_exists(candidate):
            return candidate
    raise InstallError(f"could not allocate a temporary sibling path in: {parent}")


def validate_tree_paths(source: Path, destination: Path) -> None:
    """Reject layouts where copying or replacement could recurse into itself."""

    if source == destination:
        return
    if destination.is_relative_to(source):
        raise InstallError(f"installation destination is inside its source tree: {destination}")
    if source.is_relative_to(destination):
        raise InstallError(f"installation source is inside its destination tree: {destination}")


def default_installer_lock_path() -> Path:
    override = os.environ.get("OPEN_HEALTH_AGENT_INSTALL_LOCK")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        state_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "open-health-agent" / "installer.lock"


def _try_lock_file(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size < 1:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_file(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def installer_lock(
    path: Path,
    *,
    dry_run: bool = False,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = 0.1,
) -> Iterator[None]:
    """Serialize all installer mutations for this user.

    Native advisory locks are released by the operating system if the process
    exits, so a stale PID cannot leave the installer permanently wedged.
    """

    path = path.expanduser()
    if dry_run:
        log(f"Would acquire installer lock: {path}")
        yield
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise InstallError(f"cannot open installer lock safely: {path}: {exc}") from exc
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InstallError(f"installer lock is not a regular file: {path}")
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while not acquired:
            acquired = _try_lock_file(descriptor)
            if acquired:
                break
            if time.monotonic() >= deadline:
                os.lseek(descriptor, 0, os.SEEK_SET)
                owner = os.read(descriptor, 512).decode("utf-8", errors="replace").strip()
                detail = f" ({owner})" if owner else ""
                raise InstallError(f"another Open Health Agent installation is still running{detail}")
            time.sleep(max(poll_seconds, 0.01))

        metadata = (
            f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()} "
            f"python={sys.executable}\n"
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, metadata)
        try:
            os.fsync(descriptor)
        except OSError:
            # The lock itself remains authoritative on filesystems that do not fsync.
            pass
        yield
    finally:
        if acquired:
            try:
                _unlock_file(descriptor)
            except OSError:
                pass
        os.close(descriptor)


def install_tree(
    source: Path,
    destination: Path,
    *,
    dry_run: bool,
    force: bool,
    managed_runtime: bool = False,
) -> str:
    source = source.resolve()
    expanded_destination = destination.expanduser()
    destination = expanded_destination.parent.resolve() / expanded_destination.name
    validate_tree_paths(source, destination)
    if source == destination:
        return "source"

    if destination.is_dir() and not destination.is_symlink():
        if tree_digest(source) == tree_digest(destination):
            return "unchanged"
        if not (force or managed_runtime):
            return "preserved"
    elif destination.exists() or destination.is_symlink():
        if not (force or managed_runtime):
            return "preserved"

    log(f"Install Skill: {source} -> {destination}")
    if dry_run:
        return "planned"

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}.install-", dir=str(destination.parent))
    )
    staging = staging_parent / SKILL_NAME
    displaced: Path | None = None
    keep_displaced = False
    replacement_complete = False
    try:
        shutil.copytree(source, staging, ignore=copy_filter)
        if path_exists(destination):
            if managed_runtime:
                displaced = unique_sibling(destination.parent, f".{SKILL_NAME}.old-")
            else:
                displaced = unique_sibling(destination.parent, f"{SKILL_NAME}.backup-")
                keep_displaced = True
            destination.rename(displaced)
        os.replace(staging, destination)
        replacement_complete = True
    except BaseException as exc:
        if displaced is not None and path_exists(displaced) and not replacement_complete:
            try:
                if path_exists(destination):
                    remove_path(destination)
                displaced.rename(destination)
            except BaseException as rollback_exc:
                raise InstallError(
                    f"installation failed and rollback also failed; recover {destination} "
                    f"from {displaced}: {rollback_exc}"
                ) from exc
        raise
    finally:
        if staging_parent.exists():
            try:
                shutil.rmtree(staging_parent)
            except OSError as cleanup_exc:
                log(f"Warning: could not remove installer staging directory {staging_parent}: {cleanup_exc}")

    if displaced is not None and path_exists(displaced):
        if keep_displaced:
            log(f"Preserved previous Skill at: {displaced}")
        else:
            try:
                remove_path(displaced)
            except OSError as cleanup_exc:
                log(f"Warning: could not remove old managed runtime {displaced}: {cleanup_exc}")
    return "installed"


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        raise InstallError(
            f"Python 3.10 or newer is required; found {sys.version_info.major}.{sys.version_info.minor}"
        )


def ensure_venv(venv: Path, *, dry_run: bool) -> Path:
    interpreter = venv_python(venv)
    if not interpreter.exists():
        run([sys.executable, "-m", "venv", str(venv)], dry_run=dry_run)
    requirements = SOURCE_SKILL / "scripts" / "requirements.txt"
    fingerprint = hashlib.sha256()
    fingerprint.update(requirements.read_bytes())
    fingerprint.update(b"\0")
    fingerprint.update(sys.version.encode("utf-8"))
    fingerprint.update(b"\0")
    fingerprint.update(str(Path(sys.executable).resolve()).encode("utf-8"))
    expected_stamp = fingerprint.hexdigest()
    stamp_path = venv / ".open-health-agent-requirements.sha256"
    try:
        installed_stamp = stamp_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        installed_stamp = ""
    if installed_stamp == expected_stamp and interpreter.exists():
        log("Python dependencies: unchanged")
        return interpreter
    run(
        [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--requirement",
            str(requirements),
        ],
        dry_run=dry_run,
    )
    if not dry_run:
        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{stamp_path.name}.", dir=str(venv))
        temporary = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(expected_stamp + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, stamp_path)
        finally:
            temporary.unlink(missing_ok=True)
    return interpreter


def install_wrapper(
    destination: Path,
    interpreter: Path,
    entrypoint: Path,
    *,
    dry_run: bool,
    force: bool,
) -> str:
    expanded = destination.expanduser()
    destination = expanded.parent.resolve() / expanded.name
    content = (
        "#!/usr/bin/env sh\n"
        f"{WRAPPER_MARKER}\n"
        f"exec {shlex.quote(str(interpreter))} {shlex.quote(str(entrypoint))} \"$@\"\n"
    )
    existing_content = ""
    is_managed = False
    if destination.is_file() and not destination.is_symlink():
        try:
            existing_content = destination.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            existing_content = ""
        if existing_content == content:
            return "unchanged"
        is_managed = WRAPPER_MARKER in existing_content
    if (destination.exists() or destination.is_symlink()) and not (force or is_managed):
        return "preserved"

    log(f"Install command wrapper: {destination}")
    if dry_run:
        return "planned"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.install-", dir=str(destination.parent)
    )
    temporary = Path(raw_temp)
    backup: Path | None = None
    replacement_complete = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o755)
        if path_exists(destination) and force and not is_managed:
            backup = unique_sibling(destination.parent, f"{destination.name}.backup-")
            destination.rename(backup)
        os.replace(temporary, destination)
        replacement_complete = True
    except BaseException as exc:
        if backup is not None and path_exists(backup) and not replacement_complete:
            try:
                if path_exists(destination):
                    remove_path(destination)
                backup.rename(destination)
            except BaseException as rollback_exc:
                raise InstallError(
                    f"wrapper installation failed and rollback also failed; recover "
                    f"{destination} from {backup}: {rollback_exc}"
                ) from exc
        raise
    finally:
        temporary.unlink(missing_ok=True)
    if backup is not None:
        log(f"Preserved previous command wrapper at: {backup}")
    return "installed"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the portable Open Health Agent Skill and its isolated Python runtime."
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        choices=("auto", "all", "hermes", "codex", "claude"),
        help="Agent host to install into; repeat for more than one (default: auto-detect).",
    )
    parser.add_argument(
        "--skill-dir",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="Custom directory that contains installed Skills; repeatable.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("OPEN_HEALTH_AGENT_HOME", Path.home() / ".open-health-agent")),
        help="Private health-data directory (default: ~/.open-health-agent).",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path.home() / ".local" / "share" / "open-health-agent",
        help="Managed runtime directory containing the venv and a canonical Skill copy.",
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path(os.environ.get("XDG_BIN_HOME", Path.home() / ".local" / "bin")),
        help="Directory for the stable open-health-agent command (default: ~/.local/bin).",
    )
    parser.add_argument("--workbook", type=Path, help="Optional Excel health-ledger destination.")
    parser.add_argument("--timezone", help="Optional IANA timezone, for example Asia/Shanghai.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Update differing Skill copies or an unrelated command wrapper; private files stay untouched.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show all target paths and commands without changing files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    ensure_python_version()
    if not SOURCE_SKILL.joinpath("SKILL.md").is_file():
        raise InstallError(f"Skill source is incomplete: {SOURCE_SKILL}")
    source_entrypoint = SOURCE_SKILL / "scripts" / "health_agent.py"
    if not args.dry_run and not source_entrypoint.is_file():
        raise InstallError(f"health command entrypoint is missing: {source_entrypoint}")

    targets = select_targets(args.agent, args.skill_dir)
    home = args.home.expanduser().resolve()
    runtime_root = args.runtime_dir.expanduser().resolve()
    runtime_skill = runtime_root / "skill"
    venv = runtime_root / "venv"
    wrapper = args.bin_dir.expanduser().resolve() / SKILL_NAME

    log("Open Health Agent installer")
    log("No model, messaging, Google Health, or other credentials are read or written.")
    if args.dry_run:
        log("Dry run: no files or environments will be changed.")

    lock_path = default_installer_lock_path()
    with installer_lock(lock_path, dry_run=args.dry_run):
        runtime_status = install_tree(
            SOURCE_SKILL,
            runtime_skill,
            dry_run=args.dry_run,
            force=True,
            managed_runtime=True,
        )
        log(f"Managed runtime Skill: {runtime_status}")

        for target in targets:
            status = install_tree(
                SOURCE_SKILL,
                target.destination,
                dry_run=args.dry_run,
                force=args.force,
            )
            log(f"{target.label}: {status} ({target.destination})")
            if status == "preserved":
                log("  Existing differing Skill was left untouched; rerun with --force to update it.")

        interpreter = ensure_venv(venv, dry_run=args.dry_run)
        entrypoint = runtime_skill / "scripts" / "health_agent.py"
        wrapper_status = install_wrapper(
            wrapper,
            interpreter,
            entrypoint,
            dry_run=args.dry_run,
            force=args.force,
        )
        log(f"Command wrapper: {wrapper_status} ({wrapper})")
        if wrapper_status == "preserved":
            log("  Existing unrelated command was left untouched; rerun with --force to replace it.")

        # --force only controls public Skill/runtime/wrapper replacement.  It is
        # deliberately never forwarded to init, which owns private health data.
        init_command = [str(interpreter), str(entrypoint), "--home", str(home), "init"]
        if args.workbook:
            init_command.extend(["--workbook", str(args.workbook.expanduser().resolve())])
        if args.timezone:
            init_command.extend(["--timezone", args.timezone])
        run(init_command, dry_run=args.dry_run)

    log("")
    log("Installation complete." if not args.dry_run else "Dry run complete.")
    log(f"Private data home: {home}")
    log(f"Command: {display_command([wrapper, '--home', home])}")
    log("ghealth is optional and is never installed implicitly; see scripts/install_ghealth.sh.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print(f"Installation stopped safely: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
