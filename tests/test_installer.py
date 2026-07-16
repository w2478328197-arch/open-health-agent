from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


INSTALLER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
SPEC = importlib.util.spec_from_file_location("open_health_agent_installer", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def make_tree(path: Path, content: str) -> Path:
    path.mkdir(parents=True)
    path.joinpath("payload.txt").write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("relation", ["child", "parent"])
def test_install_tree_rejects_recursive_source_destination(tmp_path: Path, relation: str) -> None:
    if relation == "child":
        source = make_tree(tmp_path / "source", "source")
        destination = source / "nested" / installer.SKILL_NAME
    else:
        destination = tmp_path / "destination"
        source = make_tree(destination / "source", "source")

    with pytest.raises(installer.InstallError, match="inside"):
        installer.install_tree(source, destination, dry_run=False, force=True)

    assert source.joinpath("payload.txt").read_text(encoding="utf-8") == "source"


def test_install_tree_same_path_is_a_noop(tmp_path: Path) -> None:
    source = make_tree(tmp_path / "source", "source")

    assert installer.install_tree(source, source, dry_run=False, force=True) == "source"


@pytest.mark.parametrize("managed_runtime", [False, True])
def test_install_tree_rolls_back_after_atomic_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_runtime: bool,
) -> None:
    source = make_tree(tmp_path / "source", "new")
    destination = make_tree(tmp_path / "skills" / installer.SKILL_NAME, "old")
    real_replace = installer.os.replace

    def fail_staging_replace(source_path: os.PathLike[str], destination_path: os.PathLike[str]) -> None:
        source_candidate = Path(source_path)
        if (
            source_candidate.name == installer.SKILL_NAME
            and source_candidate.parent.name.startswith(f".{installer.SKILL_NAME}.install-")
        ):
            raise OSError("simulated atomic replacement failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(installer.os, "replace", fail_staging_replace)

    with pytest.raises(OSError, match="simulated"):
        installer.install_tree(
            source,
            destination,
            dry_run=False,
            force=True,
            managed_runtime=managed_runtime,
        )

    assert destination.joinpath("payload.txt").read_text(encoding="utf-8") == "old"
    assert not list(destination.parent.glob(f"{installer.SKILL_NAME}.backup-*"))
    assert not list(destination.parent.glob(f".{installer.SKILL_NAME}.old-*"))
    assert not list(destination.parent.glob(f".{installer.SKILL_NAME}.install-*"))


def test_install_tree_keeps_distinct_backups_for_rapid_updates(tmp_path: Path) -> None:
    source = make_tree(tmp_path / "source", "v1")
    destination = make_tree(tmp_path / "skills" / installer.SKILL_NAME, "v0")

    assert installer.install_tree(source, destination, dry_run=False, force=True) == "installed"
    source.joinpath("payload.txt").write_text("v2", encoding="utf-8")
    assert installer.install_tree(source, destination, dry_run=False, force=True) == "installed"

    backups = list(
        installer.skill_backup_directory(destination.parent).glob(
            f"{installer.SKILL_NAME}.backup-*"
        )
    )
    assert len(backups) == 2
    assert not installer.skill_backup_directory(destination.parent).is_relative_to(
        destination.parent
    )
    assert len({backup.name for backup in backups}) == 2
    assert sorted(backup.joinpath("payload.txt").read_text(encoding="utf-8") for backup in backups) == [
        "v0",
        "v1",
    ]


def test_discoverable_skill_backups_are_quarantined(tmp_path: Path) -> None:
    destination = tmp_path / "skills" / installer.SKILL_NAME
    legacy = make_tree(
        destination.parent / f"{installer.SKILL_NAME}.backup-legacy",
        "legacy",
    )
    legacy.joinpath("SKILL.md").write_text(
        "---\nname: open-health-agent\n---\n", encoding="utf-8"
    )

    moved = installer.quarantine_discoverable_backups(destination, dry_run=False)

    assert not legacy.exists()
    assert len(moved) == 1
    assert moved[0].is_dir()
    assert moved[0].parent == installer.skill_backup_directory(destination.parent)
    assert not moved[0].is_relative_to(destination.parent)
    assert not list(destination.parent.glob(f"{installer.SKILL_NAME}.backup-*"))


def test_prior_hidden_backup_directory_is_moved_out_of_skill_discovery_root(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "host" / "skills" / installer.SKILL_NAME
    old_hidden = destination.parent / f".{installer.SKILL_NAME}-backups"
    legacy = make_tree(old_hidden / "legacy-copy", "legacy")
    legacy.joinpath("SKILL.md").write_text(
        "---\nname: open-health-agent\n---\n", encoding="utf-8"
    )

    moved = installer.quarantine_discoverable_backups(destination, dry_run=False)

    assert len(moved) == 1
    assert not old_hidden.exists()
    assert moved[0].is_dir()
    assert not moved[0].is_relative_to(destination.parent)


def test_install_tree_copy_failure_does_not_move_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_tree(tmp_path / "source", "new")
    destination = make_tree(tmp_path / "skills" / installer.SKILL_NAME, "old")

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(installer.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        installer.install_tree(source, destination, dry_run=False, force=True)

    assert destination.joinpath("payload.txt").read_text(encoding="utf-8") == "old"
    assert not list(destination.parent.glob(f"{installer.SKILL_NAME}.backup-*"))


def test_wrapper_rolls_back_if_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "bin" / installer.SKILL_NAME
    destination.parent.mkdir(parents=True)
    destination.write_text("original user command\n", encoding="utf-8")
    real_replace = installer.os.replace

    def fail_wrapper_replace(source_path: os.PathLike[str], destination_path: os.PathLike[str]) -> None:
        if Path(source_path).name.startswith(f".{installer.SKILL_NAME}.install-"):
            raise OSError("simulated wrapper replacement failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(installer.os, "replace", fail_wrapper_replace)

    with pytest.raises(OSError, match="simulated wrapper"):
        installer.install_wrapper(
            destination,
            tmp_path / "venv" / "bin" / "python",
            tmp_path / "runtime" / "health_agent.py",
            dry_run=False,
            force=True,
        )

    assert destination.read_text(encoding="utf-8") == "original user command\n"
    assert not list(destination.parent.glob(f"{installer.SKILL_NAME}.backup-*"))


def test_installer_lock_times_out_then_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "state" / "installer.lock"

    with installer.installer_lock(lock_path, timeout_seconds=0.1, poll_seconds=0.01):
        with pytest.raises(installer.InstallError, match="still running"):
            with installer.installer_lock(lock_path, timeout_seconds=0.03, poll_seconds=0.01):
                pytest.fail("a second installer acquired the same user lock")

    with installer.installer_lock(lock_path, timeout_seconds=0.1, poll_seconds=0.01):
        assert lock_path.is_file()


def test_ensure_venv_skips_unchanged_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_skill = tmp_path / "skill"
    requirements = source_skill / "scripts" / "requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("example-package==1.0\n", encoding="utf-8")
    venv = tmp_path / "venv"
    interpreter = installer.venv_python(venv)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(installer, "SOURCE_SKILL", source_skill)
    monkeypatch.setattr(
        installer,
        "run",
        lambda command, *, dry_run, cwd=None: commands.append(command),
    )

    assert installer.ensure_venv(venv, dry_run=False) == interpreter
    assert len(commands) == 1
    assert installer.ensure_venv(venv, dry_run=False) == interpreter
    assert len(commands) == 1


def test_force_is_never_forwarded_to_private_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_skill = tmp_path / "source-skill"
    source_skill.mkdir()
    source_skill.joinpath("SKILL.md").write_text("skill\n", encoding="utf-8")
    source_entrypoint = source_skill / "scripts" / "health_agent.py"
    source_entrypoint.parent.mkdir()
    source_entrypoint.write_text("", encoding="utf-8")
    target = installer.Target("test", tmp_path / "agent" / "skills")
    interpreter = tmp_path / "runtime" / "venv" / "bin" / "python"
    commands: list[list[str]] = []

    monkeypatch.setattr(installer, "SOURCE_SKILL", source_skill)
    monkeypatch.setattr(installer, "select_targets", lambda *_args: [target])
    monkeypatch.setattr(installer, "install_tree", lambda *_args, **_kwargs: "installed")
    monkeypatch.setattr(installer, "ensure_venv", lambda *_args, **_kwargs: interpreter)
    monkeypatch.setattr(installer, "install_wrapper", lambda *_args, **_kwargs: "installed")
    monkeypatch.setattr(
        installer,
        "run",
        lambda command, *, dry_run, cwd=None: commands.append(command),
    )
    monkeypatch.setenv("OPEN_HEALTH_AGENT_INSTALL_LOCK", str(tmp_path / "lock" / "installer.lock"))

    result = installer.main(
        [
            "--agent",
            "hermes",
            "--force",
            "--home",
            str(tmp_path / "private"),
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--bin-dir",
            str(tmp_path / "bin"),
        ]
    )

    assert result == 0
    assert commands[-1][-1] == "init"
    assert "--force" not in commands[-1]
