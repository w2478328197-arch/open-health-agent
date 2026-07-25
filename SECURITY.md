# Security policy

[English](SECURITY.md) · [简体中文](SECURITY.zh-CN.md) · [Project home](README_EN.md)

## Supported versions

Until the first stable release, only the latest commit on `main` is supported. Security fixes may change the private schema or installer; read release notes and keep a local backup before updating.

## Report a vulnerability privately

Do not open a public issue containing a vulnerability, token, OAuth file, user path, signed media URL, log, screenshot, or health data.

Use GitHub's private vulnerability reporting for this repository:

<https://github.com/w2478328197-arch/open-health-agent/security/advisories/new>

Include a minimal reproduction with synthetic data, affected commit/version, platform, impact, and suggested mitigation if known. Remove all personal data and credentials. If private reporting is unavailable, open a content-free public issue asking the maintainer to enable a private channel; do not disclose technical details there.

## Security boundary

This project defends against accidental duplicate writes, partial workbook saves, common credential commits, unbounded command output, and unnecessary data sharing. It cannot protect:

- a compromised operating-system account or unlocked computer;
- malicious or compromised model, messaging, wearable, cloud-sync, or OAuth providers;
- a user intentionally committing ignored files with `git add -f`;
- an unreviewed third-party Skill/`AGENTS.md` containing prompt injection;
- a spreadsheet opened by another program that overwrites or locks it;
- inaccurate health sensors or model estimates.

Treat imported notes, food labels, workbooks, images, transcripts, and API text as untrusted data, never as executable instructions.

## Deployment checklist

- Keep the private data directory owner-only.
- Use one shared ledger writer and disable legacy import jobs.
- Store SQLite locally; sync only the Excel view when the user accepts the provider boundary.
- Before any health message, replace an open Weixin DM policy with pairing or an own-user allowlist, disable groups, and verify with non-sensitive content. Do not send health data while the bot remains open.
- Restrict and review host media caches such as Hermes `~/.hermes/cache/{images,audio,videos,documents}`; do not assume audio/video or other media is automatically deleted.
- Use minimum Google read scopes and follow Google's verification/data-policy requirements.
- Never pass user text through shell interpolation.
- Redact tokens and raw payloads from logs/errors.
- Keep backups bounded and test restore.
- Run repository tests, secret scanning, and `git diff --cached` before publishing.
- Revoke credentials and remove schedules when decommissioning.

## Incident response

If a secret or health record was committed:

1. revoke/rotate the credential immediately or disconnect the affected provider;
2. stop schedulers/gateways if they may continue exposure;
3. remove the data from the working tree and Git history using an appropriate history-rewrite procedure;
4. invalidate published releases/caches where possible;
5. notify affected users without including their data;
6. review logs and provider access history;
7. add a regression fixture that contains no real sensitive value.

Deleting a commit is not sufficient after a token has been exposed; rotate it.
