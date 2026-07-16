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
8. surface missing capability honestly instead of simulating it;
9. keep health content out of host-global memory and cross-channel user profiles;
10. pass an opaque inbound message ID as `source_event_id` when available, without embedding sender identity, content, or signed media URLs.

If a host lacks command execution, it may still use the Skill as an explanatory/advice policy, but it cannot claim to have recorded, synced, or read the private ledger.

## Install into the host that receives the message

- For Weixin/WeChat in this reference architecture, install the Skill into **Hermes**. Hermes owns the iLink gateway and is therefore the required host for WeChat messages.
- Install the same Skill into **Codex** only when the user also wants to invoke it from Codex for local maintenance, development, or a separate Codex conversation. A Codex Skill copy does not replace the Hermes gateway or make Codex receive WeChat.
- When both hosts run as the same trusted OS user, point both at the same local command and private data home. Do not create competing ledgers or workbook writers.
- For WorkBuddy, Antigravity, or another host, install into the process that actually receives the message and can execute the local ledger command; then verify each capability.

The repository installer accepts repeated host flags, for example `./install.sh --agent hermes --agent codex`. After installation, restart a running Hermes gateway. Codex normally detects Skill changes automatically; if the Skill does not appear, start a new task or restart Codex.

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
- Audit every other enabled Hermes platform too. If QQ, Feishu, Discord, Telegram, or another channel can reach terminal/file/skills/memory tools, it must not use allow-all or open group access merely because Weixin itself is paired.
- Treat Hermes media caches as local sensitive storage. Installed versions may use `~/.hermes/cache/{images,audio,videos,documents}`; verify and clean them explicitly because retention varies and audio/video may persist.
- Disable health writes to Hermes global memory/USER/MEMORY stores. Health persistence belongs in the private ledger and private `AGENTS.md`; per-sender and per-Skill isolation must be proven before using a shared memory backend.
- Let the OS scheduler run health sync. The chat gateway and the health importer are separate services and must not become two workbook writers.
- Never invoke `execute_code`, a generic spreadsheet writer, or an ad-hoc Python script to save managed health sheets. Use the OHA CLI and its shared lock. During migration, stop the legacy writer before allowing the new scheduler or chat writes.
- Preserve partial-success semantics: when SQLite succeeds but the workbook projection is blocked or pending, report the durable record result and the pending Excel state separately. Do not retry through openpyxl or claim that the record failed; obtain `cloud-workbook` consent or fix export, then run the managed `export` command.
- Prefer an isolated Hermes health profile/iLink bot with a tool allowlist limited to the OHA wrapper and necessary read-only diagnostics. A shared profile with open channels plus terminal/file/code tools is not an acceptable health boundary.

### Model and media routing

Do not infer input capability from a provider name. Verify the exact model and route.

| Provider/model path | Direct image input | Required handling |
|---|---|---|
| OpenAI Codex via ChatGPT OAuth | Supported by current vision-capable Codex models | Test one non-sensitive image through the actual Weixin gateway; Codex OAuth is not an OpenAI API key |
| OpenAI API | Supported only by an image-input-capable GPT model | Check the current model documentation and exact model ID |
| Anthropic Claude vision | Supported | Verify Hermes sends the provider's native image content |
| Google Gemini | Current Gemini models are multimodal | Verify endpoint, model ID, and installed Hermes metadata |
| Nous Portal, OpenRouter, Copilot, or Bedrock | Selected-model dependent | An aggregator contains both vision and text-only choices |
| DeepSeek direct API | Current direct chat schema is text-only | Never claim native vision. Hermes may use `auxiliary.vision` to send the image to a second vision model and inject its text description; disclose that second provider |
| Local/custom endpoint | Model and server dependent | Confirm that both the model and OpenAI-compatible server implement image content; a `*-VL` model is not equivalent to an ordinary text/coding model |

Receiving an image, routing its pixels, and understanding it are separate gates. Voice is separate again: a received audio file is not a transcript. On the locally verified Hermes v0.18.0 path, Weixin audio without an iLink transcript is cached as SILK while the built-in transcription format allowlist does not include SILK. Ask for text unless a tested transcode/custom STT path exists. Re-check this behavior after upgrading Hermes.

Inbound media routing can occur before the Skill is loaded. Do not claim that an explain-first Skill response blocks a first image from reaching the configured model. Make vision consent a gateway/pairing prerequisite and require a text-first opt-in until the gateway can enforce it before media routing. Similarly, a tool call to `onboarding mark-explained` before the outbound reply is only an attempted audit, not delivery proof; use an outbound-success hook or defer marking until the next turn.

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
