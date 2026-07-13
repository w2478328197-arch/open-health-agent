# Installation and onboarding

Use this guide for a new machine, a fresh agent host, or migration from a personal spreadsheet. Keep host installation, model credentials, Google authorization, and the private ledger as separate steps so each boundary is visible to the user.

## 1. Prerequisites

- macOS or Linux for the included local scheduler adapters. The core Python ledger can run anywhere Python 3.10+ and `openpyxl` are available. iPhone and Android are data-ingress or synced-workbook viewing devices in this architecture, not hosts for the repository's SQLite service or scheduler.
- One supported agent host that can load a standard `SKILL.md` and run a local command. Hermes is the documented reference host; other hosts need the capabilities in [host-adapters.md](host-adapters.md).
- Git for cloning this repository.
- Optional: a Google account, Google Health app, Google Cloud OAuth client, and the `ghealth` CLI for wearable import.
- Optional: a vision-capable model for photos and a speech-to-text path for voice messages.

Do not require Google, Weixin, a multimodal model, or iCloud for manual text logging.

## 2. Install the project and Skill

Clone the public repository, inspect it, and run the installer from the repository root:

```bash
git clone https://github.com/w2478328197-arch/open-health-agent.git
cd open-health-agent
./install.sh --help
./install.sh
```

The installer should create an isolated runtime, install the ledger dependency, place or link the portable Skill into detected hosts, and initialize a private data home. It must not copy example health data into the user's ledger or collect credentials.

For a first Hermes install in China Standard Time with only the Excel view in iCloud Drive, an explicit example is:

```bash
./install.sh \
  --agent hermes \
  --timezone Asia/Shanghai \
  --workbook "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Open Health Agent/健康档案.xlsx"
```

The external sync boundary applies only to the selected workbook path; keep SQLite and credentials in the default private local home. Installer `--force` only updates differing public Skill copies or the command wrapper. Private `AGENTS.md`, profile, state, database, and workbook remain untouched.

For hosts that support the Agent Skills installer, the portable Skill can also be installed from GitHub:

```bash
npx --yes skills add w2478328197-arch/open-health-agent --agent '*'
```

That command installs the Skill instructions; run the repository installer as well when the host needs the local ledger runtime, workbook template, or background sync.

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
open-health-agent onboarding mark-explained
```

Before connecting an external account or installing a background schedule, obtain explicit consent for that scoped action, then run `open-health-agent onboarding grant-consent`. These timestamps are not conversation memory: never use an old status to skip the per-conversation explanation or infer consent to a new provider, scope, or schedule.

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

This repository provides an optional pinned-source builder. Review it before running; it requires Git and Go 1.23+ and does not authorize Google:

```bash
scripts/install_ghealth.sh --dry-run
scripts/install_ghealth.sh
```

The default binary is `~/.local/bin/ghealth`. Open Health Agent also checks that location when it is not on `PATH`; for a different install directory, persist the absolute executable with `open-health-agent init --force --ghealth-command /absolute/path/to/ghealth`.

1. Make the wearable sync to its manufacturer app.
2. Connect the manufacturer app to Health Connect on Android or Apple Health on iPhone when supported.
3. Connect that store to the Google Health app/account and verify that the desired metric appears there.
4. Follow the [Google Health API setup guide](https://developers.google.com/health/setup) to create the OAuth client, choosing exactly one flow below.
5. Request only the necessary read scopes listed in the [Google Health scope reference](https://developers.google.com/health/scopes).
6. Authenticate `ghealth`, run a small manual query, and only then enable automated import.

### OAuth path A: Desktop client, interactive on the ledger computer

Create a **Desktop application** client, download its JSON, and use `ghealth setup` or `ghealth auth login --scopes-preset readonly`. This flow opens a browser and uses a temporary loopback callback on the same computer. Finish with `ghealth auth status --validate`.

### OAuth path B: current Google Web Server client, non-interactive completion

Create a **Web application / Web Server** client as directed by Google's current setup page and register `https://www.google.com` exactly as the authorized redirect URI. Configure `ghealth` with that JSON, then run:

```bash
ghealth setup --project-id '<project-id>' --client-secret '/private/path/client_secret.json' \
  --scopes-preset readonly --non-interactive-auth
# If setup created pending auth, use its emitted auth_url/complete_command;
# otherwise start it explicitly:
ghealth auth login --non-interactive --scopes-preset readonly
# Open the returned auth_url privately and copy only the code query parameter.
ghealth auth login --complete '<code>'
ghealth auth status --validate
```

Follow the emitted `complete_command`; never paste the authorization code into chat or logs. Do not use the Web client's `https://www.google.com` redirect with the Desktop loopback flow, or a Desktop client with the Web callback instructions.

Google may impose testing-user, verification, restricted-scope, or production-review requirements. In OAuth **Testing** status, a refresh token may expire after about seven days, so test schedules can later become unauthorized and require a new login. Follow the current [Google Health API user-data policy](https://developers.google.com/health/policies/health-api-developer-user-data-policy) and publishing/verification requirements for longer-lived use.

### Make both tools use the same local day

Set an explicit IANA timezone in the **same active ghealth profile** used by Open Health Agent. It must exactly match the value supplied to `open-health-agent init --timezone`:

```bash
# Example: OHA was initialized with --timezone Asia/Shanghai
ghealth config set timezone Asia/Shanghai
open-health-agent doctor
```

For a named profile, keep its `GHEALTH_PROFILE` environment variable active or use ghealth's `--profile <name>` flag when running the `config set` command. `doctor` reads and compares the active profile but never edits it; a real `sync` stops before creating a sync run when the values differ or ghealth is still using implicit machine-local time. Do not paste the full output of `ghealth config show` into chat because it may contain project configuration.

## 6. Add an optional hourly schedule

Only install a background schedule after a successful manual sync and explicit user approval. The job should:

- take one shared lock;
- query an overlapping lookback window so late-arriving data can be corrected;
- upsert stable records instead of appending duplicates;
- write SQLite first and export Excel atomically;
- retain a bounded number of local backups;
- log status without tokens, raw media, or unnecessary health details;
- mark empty, unauthorized, or stale results distinctly instead of reporting false success.

Keep exactly one scheduled writer; disable legacy cron jobs or duplicate importers before enabling it. Use the installed command prefix when customized:

```bash
open-health-agent scheduler install
open-health-agent scheduler status
open-health-agent scheduler uninstall
```

At install time, the scheduler records the currently active ghealth profile and forces machine-readable JSON output. This prevents a background shell from silently falling back to the default profile or a table/CSV format. If you intentionally switch profiles later, verify that profile's timezone and reinstall the scheduler so the pinned definition changes transactionally.

On Linux this is a systemd user timer. Check the `linger` detail reported by `scheduler status`: without linger, the user's timer may stop after logout. Enabling linger is a system-level persistence decision and should be done deliberately by an administrator, not silently by this installer.

On macOS, an hourly job cannot run while the computer is fully asleep. When the user wants plugged-in availability, enable **System Settings → Battery → Options → Prevent automatic sleeping on power adapter when the display is off**. Apple notes that this applies while connected to power; “Wake for network access” is a different feature. See [Apple's Battery settings guide](https://support.apple.com/en-ca/guide/mac-help/-mchlfc3b7879/mac). Closing a Mac laptop's lid normally puts it to sleep; do not promise otherwise.

The optional AC-only adapter is independently reversible:

```bash
open-health-agent keep-awake-on-ac install
open-health-agent keep-awake-on-ac status
open-health-agent keep-awake-on-ac uninstall
```

## 7. Verify before handoff

Run the doctor, a manual sync or fixture sync, one manual measurement, one food correction, one context build, and one workbook export. Confirm:

- repeated import does not increase record counts;
- today's data is labeled partial;
- missing values remain blank, not zero;
- a non-health workbook sheet survives export;
- the workbook and data files are not tracked by Git;
- no token, OAuth client secret, user path, photo, transcript, or real health value appears in repository files or logs;
- the agent explains the Skill before its first action in a new conversation;
- the agent reads both local goals and fresh context before personalized advice.
