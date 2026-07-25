# Food correction and reconciliation patterns

## Product label overrides a generic estimate

When a user follows a meal photo with a package nutrition label:

1. Link it to the existing meal by timing, stated weight, and the user's correction wording.
2. Recalculate from the label's per-100-g values and the amount actually consumed.
3. Update the original stable food record ID; do not create a second meal.
4. Remove speculative ingredients (for example, assumed cooking oil) unless the user reported them or the evidence supports them. Preserve any remaining uncertainty separately.
5. Read back the corrected record and rebuilt daily aggregate before replying.

## Before/after and leftovers

- A leftover-only photo is not a new consumption event.
- If the original meal is already recorded, update its component record IDs using `original amount - visible remainder`.
- If the original meal was pending because identity was ambiguous, combine the before/after photos and create the consumed components once; never create both a full-meal record and a leftovers record.
- Exclude visible bones, peel, cores, discarded skin, sauce left in a cup, and uneaten food.
- Do not infer that a moved container means a food was eaten. Visible remainder and explicit user wording control.
- A user's explicit correction (for example, “面包我吃完了”) overrides the visual estimate and updates the same stable ID.

## Repeated servings

Words such as “还有三个蛋清” usually indicate an additional serving when they follow an earlier logged serving and occur with a later meal. Preserve timing and provenance, create a separate event, and verify the daily aggregate. If the wording could instead be a correction to the earlier count, ask one short question.
