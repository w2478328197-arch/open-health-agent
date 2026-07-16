# Installation and onboarding

Use this guide for a new machine or a fresh agent host. For a legacy spreadsheet or Hermes workflow, use the separate [one-writer migration checklist](../../../docs/migration.md) as well. Keep host installation, model credentials, Google authorization, the scheduler, and the private ledger as separate steps so each boundary is visible to the user.

## Contents

- [1. Prerequisites](#1-prerequisites)
- [2. Install the project and Skill](#2-install-the-project-and-skill)
- [3. Initialize private storage](#3-initialize-private-storage)
- [4. Capture the user's goals and constraints](#4-capture-the-users-goals-and-constraints)
- [5. Add optional Google Health import](#5-add-optional-google-health-import)
- [6. Add an optional hourly schedule](#6-add-an-optional-hourly-schedule)
- [7. Verify before handoff](#7-verify-before-handoff)

## 1. Prerequisites

- macOS or Linux for the included local scheduler adapters. The core Python ledger can run anywhere Python 3.10+ and `openpyxl` are available. iPhone and Android are data-ingress or synced-workbook viewing devices in this architecture, not hosts for the repository's SQLite service or scheduler.
- One supported agent host that can load a standard `SKILL.md` and run a local command. Hermes is the documented reference host; other hosts need the capabilities in [host-adapters.md](host-adapters.md).
- Git for cloning this repository.
- Optional: a Google account, Google Health app, Google Cloud OAuth client, and the `ghealth` CLI for wearable import.
- Optional: a vision-capable model for photos and a speech-to-text path for voice messages.

Do not require Google, Weixin, a multimodal model, or iCloud for manual text logging.

## 2. Install the project and Skill

Clone the public repository and inspect the installer. For the first initialization, choose exactly one of the two commands below; do not run the default command and then the iCloud command.

Choose the host by message ownership: Weixin/WeChat requires `--agent hermes` because Hermes owns the iLink gateway. Add `--agent codex` in the same run only when the user also wants Codex to invoke the same local ledger. Installing only into Codex does not provide a WeChat channel. Read [host-adapters.md](host-adapters.md) before selecting another host.

```bash
git clone https://github.com/w2478328197-arch/open-health-agent.git
cd open-health-agent
./install.sh --help
```

Default local workbook:

```bash
./install.sh --agent hermes --timezone Asia/Shanghai
```

Same WeChat installation plus optional Codex access:

```bash
./install.sh --agent hermes --agent codex --timezone Asia/Shanghai
```

Or, on macOS with iCloud Drive already enabled, place only the Excel view in iCloud:

```bash
./install.sh \
  --agent hermes \
  --timezone Asia/Shanghai \
  --workbook "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Open Health Agent/健康档案.xlsx"
```

The installer creates an isolated runtime, installs the ledger dependency, places or links the portable Skill into Hermes, and initializes a private data home. It does not install Hermes or `ghealth`, copy example health data into the user's ledger, collect credentials, grant consent, run a real Google sync, or install the scheduler automatically.

The external sync boundary applies only to the selected workbook path; keep SQLite and credentials in the default private local home. Installer `--force` only updates differing public Skill copies or the command wrapper. It does not forward force to private initialization, so rerunning the installer does not change an existing workbook path or timezone. To change an existing installation, use the CLI's explicit private-config update:

```bash
open-health-agent init --force \
  --timezone Asia/Shanghai \
  --workbook "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Open Health Agent/健康档案.xlsx"
```

That command backs up configuration and changes only explicitly supplied fields while preserving private `AGENTS.md`, profile, state, and SQLite. Selecting a new empty workbook path does not migrate custom sheets from the old workbook; selecting an existing workbook preserves ordinary non-managed sheets subject to `openpyxl` limitations. Back up complex workbooks first.

For hosts that support the Agent Skills installer, the portable Skill can also be installed from GitHub:

```bash
npx --yes skills add w2478328197-arch/open-health-agent --agent '*'
```

That command installs the Skill instructions; run the repository installer as well when the host needs the local ledger runtime, workbook template, or background sync.
It also requires Node.js/npm; the repository's Python runtime itself does not require Node.js.

## 3. Initialize private storage

The default home is `~/.open-health-agent`. Override it only with an explicit private path. The installer initializes it, creates a stable `open-health-agent` wrapper, and prints a reusable `Command:` prefix. With the default home and a `~/.local/bin` directory on `PATH`, run:

```bash
open-health-agent doctor
```

The doctor validates the local database, profile JSON, managed workbook schema, and the goal projections in private `AGENTS.md`; a missing optional `ghealth` executable is reported but does not fail a manual-only installation. If the goal projection check fails after an interrupted write, run `open-health-agent goal repair` to rebuild profile/AGENTS goal views from the canonical SQLite records, then run doctor again.

If the wrapper directory is not on `PATH`, or `--bin-dir`/`--home` was customized, use the exact command the installer prints. Do not replace the managed interpreter with an arbitrary system Python unless its dependencies were installed intentionally.

Initialization creates private configuration, SQLite, `AGENTS.md`, profile/state files, backups, logs, raw import data, and an Excel workbook. The installer should set the data directory to owner-only access where the operating system supports it.

If the workbook is stored in iCloud, OneDrive, Dropbox, or another sync folder, explain that the provider receives and retains a copy under its own terms. Keep SQLite and credentials local unless the user knowingly chooses otherwise. Never put two live writers on the same workbook.

In each new conversation, give the first-use explanation before action, then record only an installation audit timestamp:

```bash
open-health-agent onboarding mark-explained --delivery-confirmed
```

Run this only after an outbound-success hook or a later user turn confirms delivery. Before connecting an external account or installing a background schedule, obtain explicit consent for that scoped action, then run `open-health-agent onboarding grant-consent --scope <scope>`. Use `--help` for the fixed scope list. These timestamps are not conversation memory: never use an old status to skip the per-conversation explanation or infer consent to a new provider, scope, or schedule.

When the configured workbook is in a recognized cloud-synchronization folder, grant `cloud-workbook` separately before its first health-bearing projection:

```bash
open-health-agent onboarding grant-consent --scope cloud-workbook
```

Without it, canonical SQLite writes remain allowed but Excel projection is blocked and reported pending. Grant consent and run `open-health-agent export` to recover the view. A blocked Google sync does not retroactively become scheduler-eligible after export; run a new real sync with `workbook_export=succeeded`.

`cloud-workbook` and `cloud-private-home` are independent, exact-bound scopes. Do not create a new private home in a synchronized folder. If a legacy private home or canonical SQLite database is already synchronized, health writes are paused until it is migrated to ordinary local storage or the user grants `cloud-private-home` for that exact current provider/path. Moving the private home, database, or workbook invalidates the old binding; revocation does not delete provider-side copies.

The runtime persists a workbook-projection outbox marker before a SQLite mutation can become durable and clears it only after a successful atomic Excel export. A crash, export error, or consent block therefore remains discoverable by `doctor`/`context` and recoverable with `export`; never overwrite SQLite from a stale workbook.

## 4. Capture the user's goals and constraints

Before giving a plan, record:

- the user's exact goal wording, status, priority, and effective date;
- confirmed lean mass only if the user wants an FFM-based REE estimate;
- age group and relevant life stage, not a guessed exact age;
- health constraints the user explicitly asks the agent to consider;
- data-source semantics, especially whether energy is active-only or total energy.

Private goals belong in `<data-home>/AGENTS.md` and `profile.json`, both excluded from Git. The public repository contains only templates.

Pass sensitive goal/profile content over stdin, or from an owner-only UTF-8 file with `goal set --file ...` / `profile set --file ...`. The convenience flags `--text` and `--value` can appear in shell history or another local process's argument listing, so do not use them for sensitive wording on a shared machine.

## 5. Add optional Google Health import

Use the `ghealth` project maintained in the [Google-Health-API organization](https://github.com/Google-Health-API/google-health-cli). Do not describe it as a direct Health Connect reader or promise that it supports every device/metric.

After the first-use explanation has actually been delivered and recorded, obtain the user's explicit permission for the Google Health data path before account authorization or a real sync:

```bash
open-health-agent onboarding grant-consent --scope google-health
```

This is a local OHA permission record. It does not configure Google OAuth or authorize a scheduler, and it is rejected when explanation delivery has not been confirmed.

This repository provides an optional pinned-source builder. Review it before running; it requires Git and Go 1.23+ and does not authorize Google:

```bash
./scripts/install_ghealth.sh --dry-run
./scripts/install_ghealth.sh
```

The default binary is `~/.local/bin/ghealth`. Open Health Agent also checks that location when it is not on `PATH`; for a different install directory, persist the absolute executable with `open-health-agent init --force --ghealth-command /absolute/path/to/ghealth`.

1. Make the wearable sync to its manufacturer app.
2. Connect the manufacturer app to Health Connect on Android or Apple Health on iPhone when supported.
3. Connect that store to the Google Health app/account and verify that the desired metric appears there.
4. Run `ghealth setup --instructions` and create the **Desktop application** OAuth client it describes. The generic [Google Health API setup page](https://developers.google.com/health/setup) currently documents a Web Server client for applications that integrate with the API directly; that client and its `https://www.google.com` redirect are not the client flow used by this CLI.
5. In the Google Cloud OAuth consent screen, add the required scopes under **Data Access → Add or remove scopes**, and add the synchronizing Google account under **Audience → Test users** while the project is in Testing. Local CLI scope flags do not register scopes in Cloud.
6. For OHA's 14 mapped query families, request only `activity_and_fitness.readonly`, `health_metrics_and_measurements.readonly`, and `sleep.readonly` from the [Google Health scope reference](https://developers.google.com/health/scopes).
7. Authenticate `ghealth`, run a small manual query, and only then enable automated import.

### Configure the Desktop OAuth client

Keep the downloaded client JSON in a private local directory outside the repository, then use the current `ghealth` release to guide configuration:

```bash
ghealth setup --instructions
ghealth setup --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly
```

On a computer with a usable browser, authenticate with the Desktop client's temporary loopback/PKCE callback:

```bash
ghealth auth login --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly
ghealth auth status --validate
```

For a headless shell, use the same **Desktop application** client. Start the non-interactive flow, privately open the emitted URL, then copy only the `code` query parameter from the redirected browser address into the completion command:

```bash
ghealth auth login --non-interactive --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly
ghealth auth login --complete '<code>'
ghealth auth status --validate
```

Follow the URL and completion instructions emitted by the installed `ghealth` version. Never paste the client JSON, client secret, authorization URL, redirect URL, authorization code, refresh token, or command output containing them into chat, logs, or the repository. Do not create a Web application client with a `https://www.google.com` redirect for `ghealth`; Google's generic Web Server instructions apply when writing a direct API client, not when authorizing this CLI.

Google may impose testing-user, verification, restricted-scope, or production-review requirements. In OAuth **Testing** status, a refresh token may expire after about seven days, so test schedules can later become unauthorized and require a new login. This project does not send an authorization-expiry alert: during Testing, inspect `ghealth auth status --validate` and `open-health-agent scheduler status` at least weekly, and investigate when the last successful synchronization is older than two configured intervals. Follow the current [Google Health API user-data policy](https://developers.google.com/health/policies/health-api-developer-user-data-policy) and publishing/verification requirements for longer-lived use.

### Make both tools use the same local day

Set an explicit IANA timezone in the **same active ghealth profile** used by Open Health Agent. It must exactly match the value supplied to `open-health-agent init --timezone`:

```bash
# Example: OHA was initialized with --timezone Asia/Shanghai
ghealth config set timezone Asia/Shanghai
open-health-agent doctor
```

For a named profile, keep its `GHEALTH_PROFILE` environment variable active or use ghealth's `--profile <name>` flag when running the `config set` command. `doctor` reads and compares the active profile but never edits it; a real `sync` stops before creating a sync run when the values differ or ghealth is still using implicit machine-local time. Do not paste the full output of `ghealth config show` into chat because it may contain project configuration.

## 6. Add an optional hourly schedule

Only install a background schedule after all of these steps, in order:

1. Confirm that the first-use disclosure was delivered, then record active `google-health` consent.
2. As the same operating-system user and with the same ghealth profile, authenticated account, Cloud project, IANA timezone, and OHA runtime, run a **real** manual `open-health-agent sync` whose JSON says both `status=success` and `workbook_export=succeeded`. A synchronized workbook therefore needs active `cloud-workbook` consent first.
3. Within 30 minutes of that success, obtain fresh one-shot `scheduler` consent and immediately run `scheduler install`.

`--fixture` is only for synthetic tests. A fixture sync, a scheduled run, a zero exit code, or an overall `doctor` result cannot replace the real foreground success evidence. The installed job should:

- take one shared lock;
- query an overlapping lookback window so late-arriving data can be corrected;
- upsert stable records instead of appending duplicates;
- write SQLite first and export Excel atomically;
- retain a bounded number of local backups;
- log status without tokens, raw media, or unnecessary health details;
- mark empty, unauthorized, or stale results distinctly instead of reporting false success.

External imports use two short locked validation phases with unlocked external checks between them. Consent, configuration, date range, and local runtime are pinned under the writer lock; `ghealth` profile/account identity, timezone, account-bound fingerprint, and network reads happen outside it; the local snapshot is revalidated before commit. Drift rejects the batch. Do not hold the single writer lock throughout external waiting.

Keep exactly one scheduled writer. Disable legacy cron/launchd/systemd jobs, duplicate importers, and any Hermes/openpyxl path that writes the workbook directly before enabling it. The [one-writer migration checklist](../../../docs/migration.md) defines the required inventory, stop-write window, backup, conflict, validation, and rollback process. Use the installed command prefix when customized:

```bash
open-health-agent onboarding grant-consent --scope scheduler
open-health-agent scheduler install --interval-seconds 3600
open-health-agent scheduler status
open-health-agent scheduler uninstall
```

Scheduler consent is one-shot and is consumed before the operating-system job mutation begins. If that later installation step fails, a retry still requires fresh consent. If the 30-minute window expires, SQLite evidence no longer matches, or the runtime changes, complete another real manual sync and obtain a new grant. The project installer never performs these steps automatically.

At install time, the scheduler binds the `ghealth` executable, Python runtime, OHA entry point and code, active profile, authenticated account, Cloud project, scopes/authentication method, and IANA timezone. After successful installation, ongoing authorization is separately bound to the qualifying manual sync's static-runtime and account-bound runtime fingerprints. Before every background health query, active consent, installed authorization, and both fingerprints are checked. Revocation invalidates authorization before job removal, so an orphan definition cannot run; re-grant alone cannot resurrect it. Older definitions must be uninstalled and successfully reinstalled after a new qualifying sync.

When the verified foreground sync requires a local proxy, add `--inherit-proxy-env` during install or supply an owner-only `--proxy-env-file`. Only HTTP/HTTPS/ALL/NO_PROXY keys are accepted. On macOS/Linux, the file must belong to the current user, have no group/other permissions, and contain only those keys apart from comments or blank lines. HTTP/HTTPS/ALL URLs must target `localhost` or a loopback IP, include a valid port, and contain no credentials, path, query, or fragment. Remote proxies and general-purpose `.env` files are rejected. Status reports only key names, never addresses or values.

Immediately after installation, `scheduler status` proves only definition/service state. Accept the first automatic run from `last_scheduled_sync_status`, `last_scheduled_sync_at`, `last_successful_scheduled_sync_at`, and `last_scheduled_sync_error_code`; do not use `last_any_*`, which may refer to a manual run. `scheduled_trigger_pinned`, `runtime_fingerprint_matches`, and the remaining definition checks must pass. `recent_manual_ghealth_sync_eligible` only means that current foreground evidence is still eligible for one install.

On Linux this is a systemd user timer. Check the `linger` detail reported by `scheduler status`: without linger, the user's timer may stop after logout. Enabling linger is a system-level persistence decision and should be done deliberately by an administrator, not silently by this installer.

On macOS, an hourly job cannot run while the computer is fully asleep. When the user wants plugged-in availability, enable **System Settings → Battery → Options → Prevent automatic sleeping on power adapter when the display is off**. Apple notes that this applies while connected to power; “Wake for network access” is a different feature. See [Apple's Battery settings guide](https://support.apple.com/en-ca/guide/mac-help/-mchlfc3b7879/mac). Closing a Mac laptop's lid normally puts it to sleep; do not promise otherwise.

The optional AC-only adapter is independently reversible:

```bash
open-health-agent onboarding grant-consent --scope keep-awake
open-health-agent keep-awake-on-ac install
open-health-agent keep-awake-on-ac status
open-health-agent keep-awake-on-ac uninstall
```

Both scheduler and `keep-awake` grants are one-shot. Each must be used for one installation attempt within 30 minutes after it is granted; expiry, prior consumption, or a failed install requires fresh consent.

Withdraw a local permission by naming its scope. Revoking scheduler consent also attempts to remove the managed job:

```bash
open-health-agent onboarding revoke-consent --scope scheduler
open-health-agent onboarding revoke-consent --scope google-health
```

Local revocation blocks OHA use but does not revoke a Google-side token or disable Weixin, vision, speech, cloud sharing, or another host/provider. Remove or revoke each external capability separately. `open-health-agent scheduler uninstall` also revokes scheduler consent.

## 7. Verify before handoff

Run the doctor, one manual measurement, one food correction, one context build, and one workbook export. A synthetic fixture sync may test parser/idempotency behavior, but it is test-only and cannot prove Google connectivity or authorize scheduler installation. When Google import or scheduling is in scope, run a real manual sync as well. Confirm:

- repeated import does not increase record counts;
- today's data is labeled partial;
- missing values remain blank, not zero;
- a non-health workbook sheet survives export;
- the workbook and data files are not tracked by Git;
- no token, OAuth client secret, user path, photo, transcript, or real health value appears in repository files or logs;
- the agent explains the Skill before its first action in a new conversation;
- the agent reads both local goals and fresh context before personalized advice.
