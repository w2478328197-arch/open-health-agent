# Data sources and wearable chain

## Contents

- [Canonical wearable path](#canonical-wearable-path)
- [What supported means](#what-supported-means)
- [Upstream types, OHA mappings, and device paths](#upstream-types-oha-mappings-and-device-paths)
- [Freshness and dates](#freshness-and-dates)
- [Source semantics](#source-semantics)
- [Manual text, voice, and photo](#manual-text-voice-and-photo)
- [Measurements outside Google Health](#measurements-outside-google-health)
- [OAuth and scope hygiene](#oauth-and-scope-hygiene)

## Canonical wearable path

`ghealth` is a command-line client under the [Google-Health-API GitHub organization](https://github.com/Google-Health-API/google-health-cli). This project uses it to query the cloud [Google Health API](https://developers.google.com/health). It is not a direct API for Health Connect, Apple Health, Garmin Connect, Mi Fitness, or a watch.

The normal path is:

```text
wearable
  → manufacturer app
  → Health Connect (Android) or Apple Health (iPhone)
  → Google Health app/account
  → Google Health API
  → ghealth CLI
  → local SQLite
  → Excel view
```

Each arrow is an independent compatibility, permission, account, and freshness boundary. Diagnose the earliest missing hop instead of repeatedly querying the final API.

## What “supported” means

Do not say “all wearables work.” Say: **a device can contribute the metrics that its manufacturer path writes, Google Health accepts, the user authorizes, and the API returns**.

Google's [device and app connection guide](https://support.google.com/googlehealth/answer/14236613?hl=en-GB) lists examples such as Apple Watch, Garmin, Samsung Galaxy Watch, Whoop, Oura, and third-party apps. The same guide documents metric-specific gaps. For example, Google Health does not connect directly to Garmin hardware; Garmin must first sync to Garmin Connect, then through Health Connect or Apple Health. Apple Watch metrics also depend on the watch model and Apple Health data.

For Xiaomi and other manufacturers, verify the current Google matrix and the manufacturer's Health Connect/Apple Health export behavior. A brand name alone is not proof that HRV, sleep stages, SpO₂, VO₂ max, workout routes, or active energy will arrive.

## Upstream types, capture layer, and device paths

The pinned `ghealth` registry documents 40 verified API data types. OHA checks all 40 and writes returned JSON data points into the SQLite `wearable` capture layer. Steps, distance, active energy, and swim lengths use both granular and daily queries, so the registry contains 44 query streams.

The existing 14 semantic mappings remain the only automatic inputs to the stable daily, measurement, and workout views:

`steps`, `distance`, `active-energy-burned`, `active-minutes`, `daily-resting-heart-rate`, `daily-heart-rate-variability`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `daily-vo2-max`, `weight`, `body-fat`, `height`, `sleep --detail`, and `exercise`.

Other samples, waveforms, alerts, and reference catalogs are stored but not placed in advice context. Excel and context expose per-type coverage metadata only. A registered type can still be empty or fail because its device path, account, region, scope, or API eligibility is unavailable.

The following table was verified against Google's official device page on 2026-07-14. It describes paths into Google Health, not a permanent hardware certification.

| Source | Path | Representative data Google documents | Explicit gaps or conditions |
|---|---|---|---|
| Fitbit / Pixel Watch | Google first-party path | Activity, sleep, exercise, resting heart rate, and supported overnight vitals | Device-, region-, and eligibility-dependent; capture support does not prove that an account has skin-temperature, ECG, or rhythm-alert access |
| Apple Watch | Apple Health → Google Health | Activity, sleep, exercise/routes, body data, VO₂ max, heart rate, overnight HRV/SpO₂/respiratory rate/resting heart rate | No exercise minutes, stand hours, ECG/rhythm alerts, or all-day vitals |
| Garmin | Garmin Connect → Health Connect/Apple Health → Google Health | Activity, sleep, exercise summaries, heart/resting heart rate, weight | No HRV, respiratory rate, SpO₂, VO₂ max, skin temperature, routes, or lap details |
| Mi Fitness / Xiaomi | Health Connect → Google Health; Android only | Exercise heart rate, activity, sleep, exercise/maps, weight | No HRV, respiratory rate, SpO₂, VO₂ max, skin temperature, or heart rate outside exercise |
| Samsung Galaxy Watch | Samsung Health → Health Connect → Google Health; Android only | Activity, sleep, exercise, heart rate, SpO₂, VO₂ max, weight | No resting heart rate, HRV, respiratory rate, skin temperature, routes/laps; extra Samsung health-data-processing consent required |
| Oura | Oura App → Health Connect/Apple Health → Google Health | Activity, sleep, exercise summaries, heart rate, HRV, weight | No resting heart rate, respiratory rate, SpO₂, VO₂ max, skin temperature, routes/laps; OHA does not query ordinary heart rate |
| Whoop | Whoop App → Health Connect → Google Health; Android only | Activity, sleep, exercise, resting heart rate, respiratory rate, SpO₂, weight | No HRV, VO₂ max, skin temperature, out-of-exercise heart rate, routes/laps |
| Withings | Withings App → Health Connect/Apple Health → Google Health | Verify each metric | Google explicitly says Withings blood pressure is not yet supported; record it manually |
| Zepp / Amazfit | Zepp → Health Connect/Apple Health → Google Health | Activity, sleep, exercise, resting heart rate, weight, respiratory rate, VO₂ max, SpO₂, routes | No HRV, skin temperature, floors, lap details, or rhythm alerts |

Always re-check [Google's current device compatibility page](https://support.google.com/googlehealth/answer/14236613?hl=en) and the installed `ghealth schema types` output after an upstream change.

## Freshness and dates

- “The job ran this hour” does not mean “the device uploaded this hour.” Phones may be offline, manufacturer apps may wait for foreground sync, and cloud processing may lag.
- If HRV, sleep, or another metric is missing, open the manufacturer app and Google Health app, trigger/check their sync, and confirm the metric is visible in Google Health first. An hourly `ghealth` job cannot fetch data that never reached the cloud account.
- Query an overlapping window, normally 14 days, and upsert by stable identity so late data can amend prior dates.
- Measurement and workout imports are **upsert-only**. If a source record is later deleted upstream, an ordinary sync does not infer that deletion and does not automatically remove the local row. Reconcile/delete the local record explicitly after confirming the provider-side deletion; never treat an absent page as deletion during a partial or failed query.
- Provider corrections are reliable in place only when the API supplies a stable external ID (or the unchanged source timestamp is a sufficient fallback identity). Without an external ID, changing an untimed measurement's value or an untimed workout's type/duration can produce a new deterministic ID and leave the earlier row for explicit reconciliation.
- Use the user's configured timezone for day boundaries.
- Assign sleep to the wake/end date. `实际睡眠时长_h` means time asleep and excludes awake minutes; prefer an explicit `minutesAsleep`, otherwise sum compatible asleep stages, or subtract known awake time from a compatible in-bed total. Do not add overlapping sleep sessions from multiple sources. Select one coherent session/source according to configured priority and retain the chosen source.
- Label current-day health and energy as “截至目前/partial.” Use completed days for baselines.
- Record the source's data cutoff separately from the local import time. Compare real timestamps as timezone-aware instants, and calculate the cutoff independently for each daily row; a partial import must identify stale retained fields rather than applying one global cutoff to every date.
- Treat a real import as a two-phase operation around external `ghealth` work. Under the writer lock, pin only local consent, configuration, date range, and the local runtime snapshot; resolve and recheck selected profile/account identity, timezone, account-bound fingerprint, and the network fetch outside the lock; then reacquire it and revalidate the local snapshot before applying rows. If consent is withdrawn or identity/configuration drifts, reject the fetched batch before persistence. This keeps slow external reads and identity checks from blocking unrelated manual writes without weakening authorization checks.

## Source semantics

Before calculating energy, classify the imported field:

- `active_only`: energy above resting expenditure. REE and TEF may be added under the documented model.
- `total_energy`: provider value already includes resting energy and possibly other components. Do not add REE or exercise calories again.
- `unknown`: do not calculate a composite total until semantics are verified.

Do not infer semantics from a localized display label. Check the API field definition or source documentation.

## Manual text, voice, and photo

Manual input is a first-class source, not a fallback of lower dignity.

### Text

Preserve the original statement, parse date/time and unit explicitly, and ask only for information needed to prevent a materially wrong record. If the user says “血压 128/82,” store two values and `mmHg`; do not collapse it into one number.

### Voice

Use a transcription supplied by Weixin or a configured speech-to-text service. Store the transcript as the source statement and mark transcription provenance. If there is no transcript and no STT tool, ask for text. The Hermes Weixin adapter can deliver voice media, but a received audio file is not itself a trustworthy transcription; see the [Hermes Weixin media documentation](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin).

### Photo

A photo requires a vision-capable model or vision tool. Store:

- the user's confirmation that the item was actually consumed or measured, including the dedicated-chat convention below when it applies;
- recognized item(s);
- estimated portion with low/high range;
- nutrient or measurement source;
- confidence and uncertainty note;
- a local media reference if the user allows retention.

A standalone meal photo is not proof of consumption by default. In a dedicated health or diet conversation it can become a consumed-food log without repeated confirmation only after the user explicitly adopts that convention and it is saved in private `AGENTS.md`. Even with that opt-in, purchase, menu, recipe, unopened-product, eating-plan, leftover-only, or background-object cues block automatic logging. Briefly echo the items and portion range before writing. Without the saved opt-in, or when identity/context is materially ambiguous, ask one targeted question; do not ask the user to weigh the food. A photo cannot reliably establish every ingredient, cooking oil, portion weight, sodium, or micronutrient; keep missing fields blank and report coverage.

## Measurements outside Google Health

Blood pressure cuffs, scales, laboratory reports, symptoms, and other user-provided information can be recorded manually even if they do not connect to Google Health. This does not turn the system into a clinical record or diagnostic tool.

For an image of a report or device screen:

1. extract only visible values with a vision-capable model;
2. show the parsed values to the user when ambiguity is material;
3. preserve date, unit, device/source, original wording, and confidence;
4. never infer a diagnosis from the image;
5. follow urgent-safety rules before normal logging when a dangerous value or symptom is present.

## OAuth and scope hygiene

Use only required read scopes from Google's [scope reference](https://developers.google.com/health/scopes). The 40-type capture registry uses `activity_and_fitness.readonly`, `health_metrics_and_measurements.readonly`, `sleep.readonly`, `nutrition.readonly`, `ecg.readonly`, and `irn.readonly`. ECG and IRN are separate sensitive scopes and may remain unavailable for an account or project. The upstream `readonly` preset is broader because it also includes profile, settings, and location, which OHA does not read. Keep OAuth client JSON, client secrets, authorization URLs and codes, refresh tokens, pending-auth files, and raw responses outside the repository, chat, and ordinary logs.

This project's pinned `ghealth` flow uses a **Desktop application** OAuth client with loopback/PKCE. First read the instructions emitted by the installed version, then configure it:

```bash
ghealth setup --instructions
ghealth setup --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly,nutrition.readonly,ecg.readonly,irn.readonly
```

Add the same six full Google Health scopes in Google Cloud under **OAuth consent screen → Data Access → Add or remove scopes**. If the project is in Testing, add the synchronizing account under **Audience → Test users**. The CLI flag limits the local authorization request; it cannot edit the Cloud consent screen.

On a computer with a browser, authenticate and validate with:

```bash
ghealth auth login --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly,nutrition.readonly,ecg.readonly,irn.readonly
ghealth auth status --validate
```

For a headless shell, use the same Desktop client and complete the exact flow emitted by `ghealth`:

```bash
ghealth auth login --non-interactive --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly,nutrition.readonly,ecg.readonly,irn.readonly
ghealth auth login --complete '<code>'
ghealth auth status --validate
```

Google's generic [Health API setup page](https://developers.google.com/health/setup) currently describes a **Web Server** OAuth client and a `https://www.google.com` redirect for developers writing direct API clients. Do not use that Web client as a substitute for `ghealth`'s Desktop client: the callback model is different, including in headless mode. OAuth consent screens left in **Testing** may issue refresh tokens that expire after about seven days; distinguish that expiration from an empty health dataset. This project does not send an expiry alert, so during Testing inspect authorization and scheduler status at least weekly and investigate a last-success time older than two configured intervals. Treat restricted scopes and production verification as deployment requirements, not optional polish. The [Google Health API data policy](https://developers.google.com/health/policies/health-api-developer-user-data-policy) applies in addition to this project's privacy rules.
