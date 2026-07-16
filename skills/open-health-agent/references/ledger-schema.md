# Ledger schema and integrity contract

## Contents

- [Storage roles](#storage-roles)
- [Copyable manual-entry examples](#copyable-manual-entry-examples)
- [Managed workbook sheets](#managed-workbook-sheets)
- [Record identity and deduplication](#record-identity-and-deduplication)
- [Missing, quality, and confidence](#missing-quality-and-confidence)
- [Workbook write discipline](#workbook-write-discipline)
- [Time and aggregation rules](#time-and-aggregation-rules)

## Storage roles

- **SQLite is the source of truth.** It provides stable identities, idempotent upserts, sync-run state, and an audit trail.
- **Excel is a generated user view.** It is readable, editable outside managed health sheets, easy to back up, and optionally placed in a user-selected sync folder.
- **Sync summaries are diagnostic evidence.** Normal operation stores bounded, non-payload status/count/error summaries, not full `ghealth` responses. Keep summaries local, permission-restricted, and free of credentials.
- **`AGENTS.md` and `profile.json` are private control data.** They contain goals, constraints, and calculation inputs and must never be committed.

Do not make Excel and SQLite independent writable masters. Manual records enter through the CLI/database, then export to Excel. Preserve user-created sheets that are not part of the managed health schema.

A workbook in a recognized cloud-sync folder requires explicit exact-bound `cloud-workbook` consent before health-bearing projection. Without it, the SQLite mutation remains canonical and the workbook projection is reported blocked/pending; granting consent and running `open-health-agent export` repairs the view without replaying the health write. A synchronized private home or canonical database is a separate legacy risk and requires exact-bound `cloud-private-home` consent before health writes can continue; prefer migrating it back to local storage.

For manual entries, prefer JSON on stdin or an owner-only `--file`. For goals/profile values, prefer stdin or their `--file` forms; literal `--json`, `--text`, and `--value` arguments can be visible in shell history, local process listings, and retained messaging-host tool calls. An Agent must not use the literal forms for real health content. Delete a temporary payload file immediately after the command succeeds or fails.

## Copyable manual-entry examples

Use the exact command prefix printed by the installer in place of `open-health-agent`. Replace the synthetic date, time, wording, and estimates with user-confirmed values. JSON goes over stdin so health content does not become a command-line argument.

Blood pressure uses `value` for systolic and `second_value` for diastolic:

```bash
open-health-agent record --kind measurement <<'JSON'
{
  "date": "2026-07-13",
  "time": "08:20",
  "metric": "血压",
  "value": 128,
  "second_value": 82,
  "unit": "mmHg",
  "source": "上臂式袖带血压计",
  "method": "wechat-text",
  "confidence": "user-reported",
  "original_text": "早上血压 128/82"
}
JSON
```

Food must contain literal JSON `"consumed": true`. A photo-derived result must retain a portion range, uncertainty, and image/message reference; unknown nutrients stay absent rather than becoming zero:

```bash
open-health-agent record --kind food <<'JSON'
{
  "date": "2026-07-13",
  "time": "12:35",
  "meal": "午餐",
  "food_name": "鸡胸肉、米饭和西兰花",
  "consumed": true,
  "estimated_grams": 430,
  "grams_low": 380,
  "grams_high": 480,
  "summary_nutrients": {
    "energy_kcal": 610,
    "protein_g": 48,
    "fat_g": 16,
    "carbohydrate_g": 68,
    "fiber_g": 8,
    "sodium_mg": 760,
    "potassium_mg": 980,
    "calcium_mg": 95,
    "iron_mg": 4.2,
    "magnesium_mg": 110,
    "vitamin_c_mg": 70
  },
  "source": "vision estimate; user confirmed consumption",
  "estimation_notes": "照片估算；烹调油和酱汁不确定",
  "confidence": "medium",
  "image_reference": "wechat-message-local-reference",
  "original_text": "午饭吃了这盘"
}
JSON
```

Workout fields use minutes, kilometres, kcal, bpm, and a 0–10 RPE scale:

```bash
open-health-agent record --kind workout <<'JSON'
{
  "date": "2026-07-13",
  "start_time": "18:30",
  "workout_type": "抗阻训练",
  "duration_minutes": 55,
  "calories_kcal": 320,
  "average_heart_rate_bpm": 118,
  "max_heart_rate_bpm": 151,
  "intensity_rpe": 7,
  "muscle_groups": "下肢、背部",
  "training_source": "user",
  "method": "wechat-voice-transcript",
  "confidence": "user-reported",
  "original_text": "晚上练了 55 分钟腿和背，RPE 7"
}
JSON
```

Persist exact goal wording and confirmed lean mass through stdin as well. These values are private; the literals below are synthetic examples:

```bash
printf '%s' '提升力量，同时把血压安全放在第一位' | \
  open-health-agent goal set --effective-date 2026-07-13 --priority 1

printf '%s\n' '58.4' | \
  open-health-agent profile set --key lean_mass_kg --source user-confirmed
```

After any record or goal change, run `open-health-agent context` before advice. To correct a parsed event without creating a contradiction, submit the corrected payload with the same `record_id` returned by the first command.

## Managed workbook sheets

The canonical headers live in `scripts/oha/constants.py`. Do not rename or reorder managed columns silently.

| Sheet | Grain | Purpose |
|---|---|---|
| `健康日报` | one row per local date | steps, distance, active energy, actual time asleep, resting vitals, body measures, source, cutoff, quality |
| `健康测量` | one measurement event | manual/imported vitals and measurements, including a second value for blood pressure |
| `训练记录` | one workout/session | type, timing, duration, distance, energy, heart rate, RPE, muscle group, source |
| `饮食记录` | one consumed food/drink item | portion range, macro/micro estimates, source, confidence, image reference, original wording |
| `饮食营养明细` | one nutrient fact per food | extensible nutrients beyond the fixed summary columns |
| `每日营养汇总` | one local date | recorded totals, macro-based TEF range, field coverage, update time |
| `目标历史` | one goal version/event | exact wording, status, priority, effective date, safety constraint |
| `同步日志` | one import run | query interval, counts, status, data cutoff, error summary |
| `健康说明` | one rule/note | definitions, missing-value behavior, estimates, and privacy reminders |

### `健康日报`

Required column order:

```text
日期, 步数, 距离_km, 活跃消耗_kcal, 活跃分钟,
睡眠开始, 睡眠结束, 实际睡眠时长_h, 深睡_min, 核心/浅睡_min, REM_min, 清醒_min,
静息心率_bpm, HRV_ms, 血氧_%, 呼吸率_次/分, VO2max,
体重_kg, 体脂率_%, 身高_cm, 睡眠来源, 健康数据来源,
数据截止时间, 数据质量, 导入批次ID, 导入时间, 备注
```

### `健康测量`

```text
记录ID, 日期, 时间, 指标, 数值, 第二数值, 单位, 来源,
记录方式, 置信度, 原话, 备注, 导入时间
```

Use `第二数值` for a paired value such as diastolic blood pressure. Keep the metric name and unit explicit.

### `训练记录`

```text
记录ID, 日期, 开始时间, 训练类型, 时长_min, 距离_km, 热量_kcal,
平均心率_bpm, 最大心率_bpm, 强度/RPE, 训练肌群, 训练来源,
外部ID, 记录方式, 置信度, 原话, 备注, 导入时间
```

### `饮食记录`

Fixed nutrients include energy, protein, fat, saturated fat, carbohydrate, fibre, sugar, alcohol, sodium, potassium, calcium, iron, magnesium, phosphorus, zinc, vitamins A/C/D/E/B1/B2, folate, B12, cholesterol, and caffeine. Each row also carries:

```text
记录ID, 日期, 时间, 餐次, 食物,
食用量估计_g, 食用量下限_g, 食用量上限_g,
数据来源, 估算说明, 置信度, 图片引用, 原话, 备注, 录入时间
```

Blank nutrient cells mean “not verified,” not zero. `每日营养汇总` must report field coverage so a partial sum is not mistaken for a complete intake.

## Record identity and deduplication

Use a provider-issued immutable ID when available. For measurements, namespace it by metric; do not include a mutable source label, value, duration, or import time, so provider corrections update the same row. This assumes the provider-issued ID is unique within its record type; if a future connector cannot guarantee that, its adapter must add a documented stable provider namespace. When no immutable ID exists, derive a deterministic ID from the normalized record kind, source, date/time, metric/type, and stable original content.

An overlapping sync must upsert the same record. Repeating the same import with unchanged source data must not increase row counts. A user correction should update/supersede the existing ID and emit an audit event, not append a conflicting row.

For messaging hosts, `source_event_id` identifies the inbound message. If the same message yields multiple records with the same metric, workout type, or food name, add a stable opaque `source_event_item_id` (`item-1`, `item-2`, and so on) to each item. Reuse both IDs on redelivery; do not derive either from health wording or a signed media URL.

The automatic ghealth path is upsert-only for measurement and workout events: it does not interpret a record missing from a later query as a provider-side deletion. Source deletions require explicit local reconciliation. When the provider omits an immutable external ID, timestamp-based fallback identity can update a value only while its identifying timestamp and type remain stable; correcting an untimed value/type/duration may create a second deterministic row that must be reviewed and explicitly superseded or removed.

For daily aggregates, the local date is the logical identity. Recompute the date from the selected source rather than summing prior imports.

## Missing, quality, and confidence

- Store missing as SQL null / Excel blank.
- Never translate authorization failure, device not worn, sync delay, unsupported metric, or empty API response into zero.
- Separate `source`, `entry_method`, `confidence`, `quality`, `data_until`, and `imported_at`.
- On a partial composite-day import, retain only the prior fields owned by failed query keys. Keep the prior source label when retaining a value, and mark the row `partial` with the exact stale fields in `quality`; do not merge all previous fields merely because any query failed.
- Preserve raw user wording and normalized values in different fields.
- Use explicit confidence labels such as confirmed, high, medium, low; do not generate spurious decimal precision.
- An empty import is `empty` or `partial`, not a successful import of zero health.

## Workbook write discipline

Before a SQLite-changing operation can become durable, persist a workbook-projection outbox marker under the shared lock. Commit SQLite first, project Excel second, and clear the marker only after the atomic workbook replacement succeeds. If consent is absent, export fails, or the process stops between those stages, return truthful `*_export_pending`/blocked status and leave the marker for `doctor`, `context`, and a later `export`; never roll back or conceal the successful canonical write merely because its readable projection is stale.

All managed-sheet exports must:

1. acquire the shared ledger lock;
2. start from the current destination workbook or template;
3. regenerate only managed health sheets;
4. preserve unrelated user sheets;
5. validate required headers and formula/error state;
6. save to a sibling temporary file;
7. atomically replace the destination;
8. create a bounded local backup and enforce owner-only permissions when supported.

Do not let an hourly sync, a manual message handler, and a legacy importer save the workbook separately. Route them through one database and writer.

Do not hold the writer lock while running external `ghealth` work. Use short locked phases only for local consent, configuration, date range, and local runtime snapshot checks. Resolve and recheck the selected `ghealth` profile, account identity, timezone, account-bound fingerprint, and network fetch outside the lock; then reacquire the lock and revalidate the local snapshot before opening the transaction. Reject the whole batch if anything changed.

## Time and aggregation rules

- Store timestamps with offsets when the source provides them.
- Aggregate using the configured local timezone.
- Attribute sleep to its end/wake date.
- `实际睡眠时长_h` is time asleep, not time in bed: exclude awake minutes. Prefer the source's explicit asleep duration; otherwise derive it only from compatible asleep stages or a total whose awake component is known.
- Prefer one coherent sleep source/session; do not add overlapping stages from multiple devices.
- Build active-energy baselines from completed days. Label current-day totals as partial.
- Store data cutoff separately from local import/export time.
- Calculate nutrition summaries only from confirmed consumed items.
