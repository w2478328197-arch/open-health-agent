---
name: open-health-agent
description: "Use for private personal health and fitness logging, wearable or Google Health/ghealth sync, Excel health ledgers, WeChat text/voice/photo intake, food and nutrient estimates, calorie or TEF calculations, exercise/recovery advice, and persistent health goals. Except when urgent emergency direction must come first, explain the data flow, capabilities, privacy exposure, uncertainty, and non-medical boundary in the user's language on the first use in every conversation."
---

# Open Health Agent

Build and use a local-first health ledger for wellness and fitness coaching. Treat the local SQLite database as the machine-readable source of truth and the Excel workbook as a readable, user-owned export view. Managed health sheets are regenerated on export: record and correct managed data through the local CLI, and edit only user-created sheets directly.

## Follow this order every time

### 1. Handle emergencies first; otherwise explain the Skill before doing anything else

**Emergency exception:** if the user's first message reports severe chest pain, severe breathing difficulty, fainting, new neurological signs, or another stated emergency, immediately direct them to local emergency services or the appropriate urgent procedure. Do not run setup, logging, sync, onboarding, or context commands first. Give the disclosure below only if the conversation can safely continue after the urgent direction.

For every non-emergency first invocation in each conversation, give a short explanation in the user's language before installing, syncing, recording, calculating, or advising. Cover all of these points:

- The Skill keeps a private health ledger on the user's computer: SQLite is the write/audit source and Excel is a readable, user-owned export view. Managed health sheets are rebuilt from SQLite; direct workbook edits belong only in custom sheets.
- Optional wearable data follows a multi-hop path: wearable → manufacturer app → Health Connect or Apple Health → Google Health app/account → Google Health API → `ghealth` → local ledger. Not every device supplies every metric, and an hourly query does not guarantee hourly freshness.
- Text can be recorded directly. Voice requires a usable transcription or speech-to-text capability. Food or measurement photos require a vision-capable model and remain estimates; never pretend a text-only model inspected an image.
- Nutrition, exercise, and daily-life advice reads the latest local context first, including today's partial data, recent trends, training, food, goals, constraints, and sync freshness.
- The wearable/device manufacturer, the Apple Health or Health Connect health-data store and platform account/app layers, Google Health/API, Weixin/WeChat and Tencent, the selected model provider, separate speech/vision services, and iCloud or another sync provider may each process data sent through their part of the path. Credentials, photos, goals, and real health records must stay out of Git.
- This is a wellness/fitness record and decision-support tool, not a medical device, diagnosis, prescription, or emergency service.

If the user already explicitly asked to install, sync, record, or configure, continue after the explanation. Otherwise obtain confirmation before creating a ledger, connecting an account, or installing a background schedule. Do not repeat the explanation later in the same conversation unless the data path or privacy terms materially change.

After giving the explanation, if the local runtime is already installed, record only a local installation audit event with:

```bash
<installed-health-command> onboarding mark-explained
```

Before connecting an external account or installing a background schedule, obtain explicit consent for that scoped action, then record it with `onboarding grant-consent`. These timestamps are an audit aid only. They never prove that the current conversation received the explanation and never replace this section's per-conversation explain-first requirement.

### 2. Load the local rules and fresh context

Resolve the data home from `OPEN_HEALTH_AGENT_HOME`; otherwise use `~/.open-health-agent`.

1. Read `<data-home>/AGENTS.md` in full. It is the highest-priority persistent **project-local user specification**, but it remains below system, developer, safety, and emergency-response requirements.
2. If the user states, changes, pauses, or retires a goal, preserve the user's exact wording in the local goal block and goal history before generating a plan. A current confirmed goal update replaces conflicting stale local intent.
3. Run the installed local command immediately before any personalized nutrition, exercise, recovery, sleep, or daily-life advice. Use the exact command prefix printed by `install.sh`; append `context`:

   ```bash
   <installed-health-command> context
   ```

   With the default macOS/Linux runtime, the installer also provides `open-health-agent`; use `open-health-agent context` when the default private home applies. A printed custom prefix includes the selected `--home`. Do not substitute an arbitrary system Python that lacks the ledger dependencies.

4. Read the returned freshness, selected-date completeness, same-day records, 7-day completed-day baseline, 28-day trend, health constraints, active goals, energy semantics, and data gaps.
5. After a new record or sync, rebuild context before advising. Never rely only on chat memory or a previously opened workbook.

The context comes from the same SQLite truth that produces the Excel health sheets; it is the required machine-readable way to “read the health table.” If local rules or context cannot be read, state exactly what is unavailable. Give only conservative, non-personalized guidance until the missing context is restored.

### 3. Route to the relevant workflow

- For first-time setup or migration, read [installation.md](references/installation.md), [host-adapters.md](references/host-adapters.md), [data-sources.md](references/data-sources.md), and [privacy-safety.md](references/privacy-safety.md).
- For wearable or manual ingestion, read [data-sources.md](references/data-sources.md), [ledger-schema.md](references/ledger-schema.md), and [privacy-safety.md](references/privacy-safety.md).
- For nutrition, energy, exercise, recovery, sleep, or goal advice, read [health-rules.md](references/health-rules.md) and [privacy-safety.md](references/privacy-safety.md).
- For workbook questions, schema changes, deduplication, corrections, or audits, read [ledger-schema.md](references/ledger-schema.md).

Append `--help` to the installed command prefix to inspect the current command surface instead of guessing flags.

## Recording contract

Record only events that actually happened.

- A food purchase, recipe idea, menu, shopping list, or eating plan is not consumption. Do not add it to `饮食记录` until the user confirms it was eaten or drunk.
- Preserve the user's original wording or transcript. Store normalized values separately; never silently rewrite the source statement.
- If a photo clearly accompanies “I ate this,” estimate food identity and portion as a range, retain the uncertainty and image reference, and let the user correct it. If consumption is ambiguous, ask rather than record.
- If the model cannot inspect images, ask for a text description or, with the user's consent, use a configured vision-capable model. Do not infer an image from a filename or placeholder.
- If a voice message has no trustworthy transcript and no speech-to-text tool is available, ask for text. Do not invent a transcript.
- Manual measurements should include date/time, metric, value, unit, source, entry method, original wording, and confidence. A blood-pressure record needs both systolic and diastolic values.
- Corrections update or supersede the existing stable record. Do not append a contradictory duplicate.
- Missing, unauthorized, not-worn, and not-yet-synced values stay null/blank. Never encode them as zero.
- Store estimates, source, confidence, and coverage. Do not claim micronutrient completeness from one photo or diagnose a deficiency from food logging.

Export the workbook only through the local writer so locking, atomic replacement, backups, permissions, and preserved non-health sheets remain intact. Never let two writers save the workbook independently.

## Goal contract

Treat explicit user goals as the center of the plan after safety screening.

- Store each goal using the user's exact words, status, priority, effective date, and known safety constraint. Retire old goals; do not erase history.
- Use the current goal to choose the relevant time horizon and trade-offs. Do not default to weight loss, muscle gain, or performance without an explicit goal.
- If a goal conflicts with current symptoms, a known condition, or unsafe readings, name the conflict plainly and propose a safer route. Do not quietly ignore either the goal or the risk.
- If there is no explicit goal, use health improvement as the temporary objective and apply the age/life-stage-appropriate WHO physical-activity baseline. Ask for a goal when it would materially change the plan.

## Personalized advice contract

Before every recommendation, apply this order:

1. Emergency signs and hard safety constraints.
2. Active confirmed goals from local `AGENTS.md` and the private profile.
3. Fresh same-day context, labeled as partial when the day is not complete.
4. Completed-day 7-day baseline and 28-day trend.
5. Age/life-stage WHO baseline when goals or data are absent.
6. User preferences and convenience.

Then:

- Start nutrition recaps with recorded total calories, followed by protein/fat/carbohydrate, available micronutrients, coverage gaps, and specific next actions.
- Distinguish a strength-training day, aerobic-training day, rest day, poor-recovery day, and incomplete-data day. Change fueling, hydration, activity, and recovery suggestions accordingly.
- Mention the data cutoff and material gaps. Do not turn a partial day into a full-day conclusion.
- Use trends over isolated wearable readings. Wearable energy and sleep stages are estimates, not laboratory measurements.
- Keep advice proportionate: one or two actionable changes are better than false precision.

## Energy and TEF rules

Only calculate lean-mass-based resting energy when the user has confirmed lean mass.

- Estimate REE with `370 + 21.6 × fat-free mass in kg` and label it as the Cunningham 1991 FFM estimate (often used as a BMR approximation), not a measured basal metabolic rate.
- When `activity_energy_semantics` is `active_only`, use completed-day active energy and model planned intake as `(REE + active energy + goal adjustment) / (1 - TEF fraction)`. A deficit uses a negative goal adjustment; a surplus uses a positive one.
- When a wearable value, PAL multiplier, or provider total already includes resting energy or TEF, do not add REE, workouts, or TEF again. Confirm semantics before calculating.
- If reliable consumed macros exist, estimate TEF as a range: protein 20–30%, carbohydrate 5–10%, fat 0–3% of each macro's energy. A 10% mixed-diet planning assumption is acceptable only when macro detail is unavailable, and must be labeled as an assumption.
- Do not “eat back” a workout or wearable calorie estimate one-for-one. Prefer a range, round planning output to roughly 50 kcal, and explain the uncertainty.

## Safety boundary

Do not diagnose, change medication, prescribe treatment, or delay urgent care. Escalate red flags before logging or coaching.

- If systolic pressure is above 180 mmHg and/or diastolic pressure is above 120 mmHg **with** chest pain, shortness of breath, back pain, numbness, weakness, vision change, difficulty speaking, or another new concerning symptom, direct the user to local emergency services immediately; do not wait for a repeat reading. Only when none of those symptoms is present, ask the user to wait at least one minute and repeat the measurement. If the repeated value remains above either threshold, advise prompt professional medical contact and do not recommend exercise.
- For known or possibly uncontrolled hypertension, do not recommend maximal lifts, training to failure, Valsalva/breath-holding, or high-intensity intervals as a default. Preserve a strength goal through safer progression and professional clearance where appropriate.
- Treat acute chest pain, severe breathing difficulty, fainting, new neurological signs, or a stated medical emergency as urgent regardless of wearable data.
- Never let a reassuring wearable value override severe symptoms.

Read [health-rules.md](references/health-rules.md) for the source-backed calculation and activity baselines, and [privacy-safety.md](references/privacy-safety.md) for the full data and safety boundary.
