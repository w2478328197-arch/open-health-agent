# Agent host adapters

The portable core is `SKILL.md`; the ledger runtime is host-independent Python. A host name alone does not guarantee file access, command execution, image understanding, voice transcription, background scheduling, or persistent instruction priority.

## Minimum host contract

A full installation needs the host to:

1. load the `open-health-agent` Skill when health logging/advice is requested;
2. give the first-use explanation before action;
3. read `<data-home>/AGENTS.md` and execute the local context command;
4. run the ledger CLI with structured input without shell-interpolating user text;
5. return images only to a confirmed vision-capable model and audio only to a confirmed STT path;
6. respect the user's local timezone and data-home path;
7. keep background scheduling outside the chat session when the host cannot stay alive;
8. surface missing capability honestly instead of simulating it.

If a host lacks command execution, it may still use the Skill as an explanatory/advice policy, but it cannot claim to have recorded, synced, or read the private ledger.

## Compatibility matrix

| Capability | Hermes | WorkBuddy | Antigravity | Generic Agent Skills host |
|---|---|---|---|---|
| Load standard `SKILL.md` | documented Skill support | verify installed version | verify installed version | required |
| Local command/file access | supported in normal local deployment | verify runtime policy | verify runtime policy | host-specific |
| Weixin/WeChat channel | iLink Bot adapter | not assumed | not assumed | not assumed |
| Vision | selected model/tool dependent | model/tool dependent | model/tool dependent | model/tool dependent |
| Voice transcription | channel/STT dependent | channel/STT dependent | channel/STT dependent | channel/STT dependent |
| Hourly background job | OS scheduler recommended | OS scheduler recommended | OS scheduler recommended | OS scheduler recommended |

Do not publish an unverified “works everywhere” claim. The Skill's reasoning contract is portable; operational capabilities require host verification.

## Hermes adapter

Hermes is the reference implementation for this repository.

- Install/configure Hermes using its [official installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation).
- Select a model with `hermes model`. “Login with ChatGPT” means choosing the **OpenAI Codex** provider and completing its OAuth device-code flow; direct `OPENAI_API_KEY` use is the separate `openai-api` provider. See [Hermes provider docs](https://hermes-agent.nousresearch.com/docs/integrations/providers).
- Connect personal WeChat with `hermes gateway setup` → Weixin. Hermes uses Tencent's iLink Bot API and creates a separate bot identity. Before any health message, replace an open DM policy with pairing/own-user allowlist, disable groups, and verify with non-sensitive content. See [Hermes Weixin docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin).
- Treat Hermes media caches as local sensitive storage. Installed versions may use `~/.hermes/cache/{images,audio,videos,documents}`; verify and clean them explicitly because retention varies and audio/video may persist.
- Let the OS scheduler run health sync. The chat gateway and the health importer are separate services and must not become two workbook writers.

The repository scheduler is installed, inspected, and removed with `open-health-agent scheduler install|status|uninstall`. On Linux, inspect the status-reported systemd user `linger` value; without linger, logout may stop the user timer. On macOS, the optional AC-only process uses `open-health-agent keep-awake-on-ac install|status|uninstall` and does not guarantee operation during lid-closed/full sleep.

Read the repository's [Hermes setup guide](https://github.com/w2478328197-arch/open-health-agent/blob/main/docs/hermes.md) for the complete worked example. The linked official Hermes documentation remains authoritative when the installed version changes.

## WorkBuddy and Antigravity adapters

Treat these as capability-based integrations:

1. Install/copy the `skills/open-health-agent` folder using that host's current Skill mechanism.
2. Point the host to the same private `OPEN_HEALTH_AGENT_HOME` only when it runs as the same trusted OS user.
3. Add a host instruction that invokes the Skill for health, food, exercise, wearable, and goal messages.
4. Test that the host actually reads local `AGENTS.md` and the CLI context output before advice.
5. Test image and audio separately; text-only success does not prove multimodal support.
6. Use the OS scheduler for hourly sync unless the host documents a durable, single-instance scheduler.

If either host changes its Skill format or security model, write a thin adapter without weakening the core rules. Never hard-code credentials or personal paths into an adapter.

## Instruction priority

The private `<data-home>/AGENTS.md` is the highest persistent **project-local user specification** for this Skill. The Skill must read it directly even if the host has its own project-instruction discovery order. It does not override system/developer instructions, emergency handling, legal/safety restrictions, or a newly confirmed user goal that must be persisted immediately.

Do not claim that a repository `AGENTS.md` is universally the host's absolute highest instruction layer. Host platform rules still apply.
