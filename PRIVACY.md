# Privacy

[English](PRIVACY.md) · [简体中文](PRIVACY.zh-CN.md) · [Project home](README_EN.md)

Open Health Agent is local-first: the canonical SQLite ledger, private goals, configuration, context, backups, and Excel view are created under a user-controlled local data directory. The repository does not operate a hosted health-data service.

Local-first does **not** mean all upstream processing is local. Depending on what the user enables:

- the wearable/device manufacturer and its app or cloud may process device data;
- the Apple Health or Health Connect health-data store, plus the associated Apple/Google operating-system account and health-app layers, may process synchronized health data;
- Google Health and Google Health API may process imported wearable data;
- Tencent's Weixin iLink Bot API processes WeChat messages and media;
- the selected language-model provider, and any separate speech-to-text or vision provider, processes submitted content;
- iCloud or another sync provider processes a workbook placed in its folder.

Users should review each provider's terms and retention controls before connecting it.

## Data stored locally

The application may store measurements, workouts, sleep/activity aggregates, consumed-food entries, nutrient estimates, exact goal wording, health constraints supplied by the user, sync metadata, source/confidence notes, bounded audit events/backups, configuration, and non-payload diagnostic summaries. It does not persist full `ghealth` responses in normal operation.

Credentials and real records are not part of the public repository. The `.gitignore` excludes common databases, workbooks, media, environment files, logs, and tokens. That is a safeguard, not a substitute for checking staged files before commit.

## Purpose and minimization

Data is used to maintain the user's private ledger, export a readable workbook, and build a compact context for wellness/fitness suggestions. The project should:

- request only user-selected Google Health read scopes;
- send a minimal context to a model instead of the full workbook;
- avoid collecting exact identity or location data that the task does not need;
- keep missing data blank rather than manufacture values;
- avoid retaining raw provider payloads; inspect and remove host/provider media caches according to the user's choice;
- redact credentials and sensitive URLs from logs.

Google Health integrations must also follow the current [Google Health API Developer and User Data Policy](https://developers.google.com/health/policies/health-api-developer-user-data-policy), [OAuth setup requirements](https://developers.google.com/health/setup), and [scope documentation](https://developers.google.com/health/scopes).

## User control

The user controls the local data directory and may:

- inspect or export SQLite/Excel data;
- correct a record while retaining an audit event;
- disable the hourly schedule;
- disconnect Google, Weixin, or model providers;
- inspect and clean local messaging-media caches according to the installed host version;
- move the Excel view out of a sync folder;
- revoke provider credentials;
- delete the local data home and provider-side data according to provider controls.

Deleting this repository does not delete data held by external providers. Deleting the data home does not revoke OAuth tokens stored by another application. Both actions may be required.

Hermes versions may download message media under `~/.hermes/cache/images`, `audio`, `videos`, and `documents`. Retention is version- and media-type-dependent; this project does not promise automatic deletion, and audio/video may remain. Keep the host cache owner-only where possible, inspect the installed version's behavior, and remove media no longer needed. Provider-side copies require the provider's own deletion controls.

## Images, voice, and auxiliary models

A successfully delivered WeChat image or voice message does not prove that the selected main model can understand it. An image may be processed by the main vision model; when the main endpoint is text-only, such as the current direct DeepSeek chat API, Hermes may instead send it to a separately configured auxiliary vision model and pass the resulting description to the main model. Voice transcription may likewise use a separate speech-to-text service. Before enabling either route, identify the actual provider: one message can cross more than one model service.

Food-photo estimates, instrument readings, and speech transcripts can all be wrong. Retain provenance, confidence, and uncertainty, and do not use these estimates for diagnostic imaging, medical conclusions, or other high-risk decisions.

## Sharing and publication

The software does not authorize sharing health data with employers, insurers, advertisers, other users, or public repositories. An instruction to log or analyze data is not consent to publish it. Use synthetic data in issues, tests, screenshots, and pull requests.

For implementation-specific safeguards, see [the Skill privacy and safety reference](skills/open-health-agent/references/privacy-safety.md). For a vulnerability or accidental exposure, follow [SECURITY.md](SECURITY.md).

## Non-medical notice

Open Health Agent is not a medical device, healthcare provider, diagnosis, treatment, prescription, or emergency-monitoring service. Wearable and image estimates may be inaccurate. Do not delay professional or emergency care based on this software.
