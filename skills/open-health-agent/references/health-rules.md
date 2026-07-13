# Health, nutrition, and exercise rules

This reference defines calculation and decision rules for wellness/fitness coaching. It does not authorize diagnosis, treatment, medication changes, or emergency triage beyond directing the user to appropriate care.

## Decision hierarchy

Apply this order before advice:

1. Acute symptoms, emergency thresholds, and system safety rules.
2. The user's active confirmed goals and constraints from private `AGENTS.md`/profile.
3. Fresh same-day context, explicitly labeled partial when appropriate.
4. Completed-day 7-day baseline and 28-day trend.
5. WHO age/life-stage baseline when goals or data are absent.
6. Preferences, schedule, and convenience.

If the user states a new goal, store the exact wording before planning. If a goal and safety conflict, explain the conflict and offer a safer route toward the goal.

## Resting energy estimate

Use the Cunningham 1991 fat-free-mass equation only with user-confirmed lean/fat-free mass:

```text
estimated REE (kcal/day) = 370 + 21.6 × fat-free mass (kg)
```

The source proposed this as a generalized REE prediction and reported substantial unexplained variation. Label it `cunningham_1991_ffm`, “estimated REE,” or “BMR approximation”; never call it a measurement. See [Cunningham, 1991, PubMed PMID 1957828](https://pubmed.ncbi.nlm.nih.gov/1957828/).

Do not substitute scale weight for lean mass. If lean mass is unknown, explain that this model cannot be applied and use no invented body-fat percentage.

## Activity and total energy

First identify semantics.

The runtime setting is explicit: `active_only` permits the component calculation below; `total_energy` blocks it. A confirmed source change can be recorded with `profile set --key activity_energy_semantics --value '"total_energy"'` (or `active_only`). Do not change this field merely to obtain a preferred calorie target.

### Active-only source

For a planning average based on completed-day active energy:

```text
planned intake = (estimated REE + active energy + goal adjustment) / (1 - planned TEF fraction)
```

Use a negative goal adjustment for a planned deficit and a positive one for a surplus. The default mixed-diet TEF planning fraction may be 0.10 when macro composition is unknown, but must be labeled as an assumption.

For a retrospective day with recorded consumed macros:

```text
estimated TDEE = estimated REE + active energy + estimated TEF
```

### Total-energy or PAL source

If a wearable total, provider estimate, or PAL multiplier already includes resting energy, activity, and/or TEF, do not add those components again. Do not add an individual workout when it is already represented in daily active energy.

Wearable energy is uncertain. Do not eat it back 1:1. Use completed-day averages, present a range, round practical targets to about 50 kcal, and revise from multi-week body-weight/performance trends rather than one day.

## Thermic effect of food

When consumed macros are available, use energy factors of 4 kcal/g for protein, 4 kcal/g for carbohydrate, and 9 kcal/g for fat, then estimate:

| Macro | Low | Midpoint | High |
|---|---:|---:|---:|
| Protein | 20% | 25% | 30% |
| Carbohydrate | 5% | 7.5% | 10% |
| Fat | 0% | 1.5% | 3% |

These are estimation ranges, not person-specific measured TEF. The U.S. National Academies' 2023 energy DRI review summarizes the same approximate ranges; see [Factors Affecting Energy Expenditure and Requirements](https://www.ncbi.nlm.nih.gov/books/NBK591031/).

Do not apply both macro-specific TEF and an additional flat 10%. Do not calculate retrospective TEF from planned food that was not consumed.

## Nutrition recap

For each recap:

1. state recorded calories first;
2. report protein, fat, and carbohydrate;
3. report available fibre and micronutrients;
4. state how many food rows contain each field or otherwise explain coverage;
5. identify uncertainty from portions, recipes, cooking oil, restaurant food, or photos;
6. give one or two specific actions tied to today's goal, training, and remaining meals.

Never equate an unfilled micronutrient cell with zero intake. Never diagnose deficiency from a photo or a short food log. When a clinically meaningful deficiency or eating disorder is suspected, recommend qualified professional assessment without moralizing.

## Training-day adaptation

Use today's recorded workout and recovery context.

- **Strength day:** consider resistance volume/RPE, affected muscle groups, protein distribution, carbohydrate around training when useful, hydration, and next-session recovery. Do not recommend another hard session for the same area merely because generic weekly volume is low.
- **Aerobic day:** consider duration/intensity, heat, fluid, carbohydrate needs for longer work, and lower-body recovery.
- **Rest day:** do not force exercise calories. Favor normal movement, sleep, and goal-consistent intake.
- **Poor-recovery day:** use sleep, resting-heart-rate/HRV trend where available, symptoms, soreness, and user perception. A single HRV value does not decide readiness.
- **Incomplete-data day:** state the cutoff and give conditional options rather than a definitive daily score.

## WHO cold-start baseline

When there is no explicit goal, use “improve health” as the temporary objective and fit the recommendation to age, life stage, current ability, and conditions. WHO's [Guidelines on physical activity and sedentary behaviour](https://pmnch.who.int/resources/publications/i/item/9789240015128) and [physical activity fact sheet](https://www.who.int/news-room/fact-sheets/detail/physical-activity) emphasize that some activity is better than none and all movement counts.

For generally healthy adults, use 150–300 minutes/week of moderate aerobic activity or 75–150 minutes/week vigorous (or an equivalent combination), plus muscle-strengthening activities for major muscle groups on at least 2 days/week. For older adults, also include multicomponent balance/functional activity on at least 3 days/week when appropriate. For children, adolescents, pregnancy/postpartum, disability, or chronic conditions, load the specific WHO life-stage recommendation rather than reusing the generic adult target.

Do not present the upper end as a starting prescription for an inactive person. Start small and progress.

## Blood pressure and urgent safety

If systolic pressure is above 180 mmHg and/or diastolic pressure is above 120 mmHg and there is chest pain, shortness of breath, back pain, numbness, weakness, vision change, difficulty speaking, or another new concerning symptom, direct the user to local emergency services immediately and do not wait for a repeat measurement. Only when none of those symptoms is present, ask the user to wait at least one minute and repeat the measurement. If the repeated value remains above either threshold, advise prompt contact with a healthcare professional and do not recommend exercise. This follows the American Heart Association's [home blood-pressure monitoring guidance](https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings/monitoring-your-blood-pressure-at-home).

For known or possibly uncontrolled hypertension, do not default to maximal lifting, training to failure, breath-holding/Valsalva, or HIIT. A strength goal can remain active, but programming should use safer technique/progression and professional clearance when indicated.

Treat severe chest pain, severe breathing difficulty, fainting, new neurological symptoms, or the user's stated emergency as urgent regardless of device data. A normal-looking watch value cannot rule out an emergency.

## Language and precision

Use “recorded,” “estimated,” “appears,” and “data unavailable” accurately. Separate observed data from inference. Avoid moral labels for foods, deterministic sleep-stage claims, and decimal precision unsupported by the source.
