#!/usr/bin/env python3
"""Update and verify Open Health Agent's audited ghealth source pin."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_ghealth.sh"
README_ZH = ROOT / "README.md"
CHANGELOG = ROOT / "docs" / "ghealth-upstream-updates.md"
UPSTREAM_REPOSITORY = "https://github.com/Google-Health-API/google-health-cli"
PIN_PATTERN = re.compile(r'^COMMIT="([0-9a-f]{40})"$', re.MULTILINE)
README_PIN_PATTERN = re.compile(
    r"https://github\.com/Google-Health-API/google-health-cli/tree/([0-9a-f]{40})"
)
CHANGELOG_MARKER = "<!-- GHEALTH_UPDATES:NEWEST_FIRST -->"


@dataclass(frozen=True)
class UpstreamCommit:
    sha: str
    committed_at: str
    subject: str


def run_git(repository: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def current_pin() -> str:
    content = INSTALLER.read_text(encoding="utf-8")
    match = PIN_PATTERN.search(content)
    if not match:
        raise RuntimeError(f"could not find one full ghealth commit pin in {INSTALLER}")
    return match.group(1)


def verify_repository() -> str:
    pin = current_pin()
    readme = README_ZH.read_text(encoding="utf-8")
    readme_pins = README_PIN_PATTERN.findall(readme)
    if readme_pins != [pin]:
        raise RuntimeError(
            "README.md must contain exactly one locked-upstream link matching "
            f"scripts/install_ghealth.sh ({pin})"
        )
    changelog = CHANGELOG.read_text(encoding="utf-8")
    if CHANGELOG_MARKER not in changelog:
        raise RuntimeError("ghealth upstream changelog marker is missing")
    if f"`{pin}`" not in changelog:
        raise RuntimeError("ghealth upstream changelog does not describe the current pin")
    print(f"ghealth pin is internally consistent: {pin}")
    return pin


def resolve_update(repository: Path, target_ref: str) -> tuple[str, str]:
    old_pin = current_pin()
    target = run_git(repository, "rev-parse", f"{target_ref}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise RuntimeError(f"target did not resolve to a full commit SHA: {target}")
    run_git(repository, "cat-file", "-e", f"{old_pin}^{{commit}}")
    if old_pin != target:
        ancestry = subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", old_pin, target],
            check=False,
            capture_output=True,
            text=True,
        )
        if ancestry.returncode != 0:
            raise RuntimeError(
                "the new upstream target is not a fast-forward descendant of the audited pin; "
                "manual provenance review is required"
            )
    return old_pin, target


def commits_between(repository: Path, old_pin: str, target: str) -> list[UpstreamCommit]:
    if old_pin == target:
        return []
    payload = run_git(
        repository,
        "log",
        "--reverse",
        "--format=%H%x1f%cI%x1f%s%x1e",
        f"{old_pin}..{target}",
    )
    commits: list[UpstreamCommit] = []
    for record in payload.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        fields = record.split("\x1f", 2)
        if len(fields) != 3:
            raise RuntimeError("could not parse upstream git history")
        commits.append(UpstreamCommit(*fields))
    if not commits:
        raise RuntimeError("the target changed but the upstream commit range is empty")
    return commits


def changed_files(repository: Path, old_pin: str, target: str) -> list[str]:
    if old_pin == target:
        return []
    return [
        line
        for line in run_git(repository, "diff", "--name-only", f"{old_pin}..{target}").splitlines()
        if line
    ]


def impact_summary(paths: list[str]) -> list[str]:
    groups: list[str] = []
    if any(path in {"go.mod", "go.sum"} for path in paths):
        groups.append("Go 依赖或构建基线发生变化")
    if any(path == "main.go" or path.startswith(("cmd/", "pkg/", "internal/")) for path in paths):
        groups.append("CLI 可执行逻辑发生变化")
    if any(path.startswith("skills/") for path in paths):
        groups.append("上游 Agent Skill 指引发生变化")
    if any(path.lower().startswith(("readme", "docs/")) for path in paths):
        groups.append("上游文档发生变化")
    if any(path.startswith((".github/", "scripts/")) for path in paths):
        groups.append("上游自动化或维护脚本发生变化")
    return groups or ["其他上游文件发生变化"]


def update_text(path: Path, pattern: re.Pattern[str], replacement: str) -> None:
    content = path.read_text(encoding="utf-8")
    updated, count = pattern.subn(replacement, content)
    if count != 1:
        raise RuntimeError(f"expected exactly one update target in {path}, found {count}")
    path.write_text(updated, encoding="utf-8")


def render_entry(
    old_pin: str,
    target: str,
    commits: list[UpstreamCommit],
    paths: list[str],
) -> str:
    checked_at = datetime.now(timezone.utc).date().isoformat()
    compare_url = f"{UPSTREAM_REPOSITORY}/compare/{old_pin}...{target}"
    lines = [
        f"## {checked_at} · `{target}`",
        "",
        f"- 上游范围：[`{old_pin[:12]}` → `{target[:12]}`]({compare_url})",
        f"- 提交数：{len(commits)}",
        f"- 变更文件数：{len(paths)}",
        f"- 影响判断：{'；'.join(impact_summary(paths))}",
        "- 合入门槛：上游 Go 测试、ghealth 构建、OHA 命令契约检查、仓库校验和 OHA 全量测试全部通过。",
        "",
        "### 上游提交",
        "",
    ]
    for commit in commits:
        commit_url = f"{UPSTREAM_REPOSITORY}/commit/{commit.sha}"
        lines.append(
            f"- [`{commit.sha[:12]}`]({commit_url}) · {commit.committed_at[:10]} · {commit.subject}"
        )
    lines.extend(["", "### 变更文件", ""])
    lines.extend(f"- `{path}`" for path in paths)
    lines.extend(
        [
            "",
            "### 对 Open Health Agent 的含义",
            "",
            "自动化只更新已审计的源码锁定提交；不会扩大 Google Health OAuth scope，也不会在用户设备上静默替换已安装二进制。后者会改变 scheduler 绑定的运行时指纹，必须走本机升级与重新验收流程。",
        ]
    )
    return "\n".join(lines) + "\n"


def apply_update(repository: Path, target_ref: str, summary_file: Path | None) -> bool:
    old_pin, target = resolve_update(repository, target_ref)
    if old_pin == target:
        print(f"ghealth pin is already current: {target}")
        return False

    commits = commits_between(repository, old_pin, target)
    paths = changed_files(repository, old_pin, target)
    entry = render_entry(old_pin, target, commits, paths)

    update_text(INSTALLER, PIN_PATTERN, f'COMMIT="{target}"')
    update_text(
        README_ZH,
        README_PIN_PATTERN,
        f"https://github.com/Google-Health-API/google-health-cli/tree/{target}",
    )

    changelog = CHANGELOG.read_text(encoding="utf-8")
    if CHANGELOG_MARKER not in changelog:
        raise RuntimeError("ghealth upstream changelog marker is missing")
    CHANGELOG.write_text(
        changelog.replace(CHANGELOG_MARKER, f"{CHANGELOG_MARKER}\n\n{entry}", 1),
        encoding="utf-8",
    )
    if summary_file is not None:
        summary_file.write_text(
            "## ghealth 上游自动同步\n\n"
            + entry
            + "\n此 PR 由固定版本跟踪工作流生成；完整跨平台 CI 通过后自动合入。\n",
            encoding="utf-8",
        )
    verify_repository()
    print(f"updated ghealth pin: {old_pin} -> {target}")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="verify repository pin consistency")
    parser.add_argument("--upstream-dir", type=Path, help="local clone of google-health-cli")
    parser.add_argument("--target-ref", default="HEAD", help="commit-ish in the upstream clone")
    parser.add_argument("--summary-file", type=Path, help="write a generated pull-request body")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.verify:
        if args.upstream_dir is not None or args.summary_file is not None:
            raise SystemExit("--verify cannot be combined with update options")
        verify_repository()
        return 0
    if args.upstream_dir is None:
        raise SystemExit("--upstream-dir is required when applying an update")
    apply_update(args.upstream_dir.resolve(), args.target_ref, args.summary_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
