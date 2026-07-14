# Data sources and wearable chain

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

In a dedicated health or diet conversation, a standalone close-up meal photo can default to a consumed-food log when there are no cues that it is a purchase, menu, recipe, unopened product, eating plan, leftover-only image, or background object. Briefly echo the items that will be counted before writing. Ask one targeted question only when identity or consumption is materially ambiguous; do not ask the user to weigh the food. Outside that narrow convention, a photo alone is not proof of consumption. A photo cannot reliably establish every ingredient, cooking oil, portion weight, sodium, or micronutrient; keep missing fields blank and report coverage.

## Measurements outside Google Health

Blood pressure cuffs, scales, laboratory reports, symptoms, and other user-provided information can be recorded manually even if they do not connect to Google Health. This does not turn the system into a clinical record or diagnostic tool.

For an image of a report or device screen:

1. extract only visible values with a vision-capable model;
2. show the parsed values to the user when ambiguity is material;
3. preserve date, unit, device/source, original wording, and confidence;
4. never infer a diagnosis from the image;
5. follow urgent-safety rules before normal logging when a dangerous value or symptom is present.

## OAuth and scope hygiene

Use only required read scopes from Google's [scope reference](https://developers.google.com/health/scopes). Keep OAuth client JSON, client secrets, authorization URLs and codes, refresh tokens, pending-auth files, and raw responses outside the repository, chat, and ordinary logs.

This project's pinned `ghealth` flow uses a **Desktop application** OAuth client with loopback/PKCE. First read the instructions emitted by the installed version, then configure it:

```bash
ghealth setup --instructions
ghealth setup --scopes-preset readonly
```

On a computer with a browser, authenticate and validate with:

```bash
ghealth auth login --scopes-preset readonly
ghealth auth status --validate
```

For a headless shell, use the same Desktop client and complete the exact flow emitted by `ghealth`:

```bash
ghealth auth login --non-interactive --scopes-preset readonly
ghealth auth login --complete '<code>'
ghealth auth status --validate
```

Google's generic [Health API setup page](https://developers.google.com/health/setup) currently describes a **Web Server** OAuth client and a `https://www.google.com` redirect for developers writing direct API clients. Do not use that Web client as a substitute for `ghealth`'s Desktop client: the callback model is different, including in headless mode. OAuth consent screens left in **Testing** may issue refresh tokens that expire after about seven days; distinguish that expiration from an empty health dataset. Treat restricted scopes and production verification as deployment requirements, not optional polish. The [Google Health API data policy](https://developers.google.com/health/policies/health-api-developer-user-data-policy) applies in addition to this project's privacy rules.
