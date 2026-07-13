from __future__ import annotations

from dataclasses import dataclass

from .constants import FORMULA_ID


@dataclass(frozen=True)
class TefRange:
    low: float
    midpoint: float
    high: float


def estimated_ree(lean_mass_kg: float) -> float:
    """Estimate resting energy expenditure with Cunningham 1991 FFM equation."""
    if not 20 <= lean_mass_kg <= 200:
        raise ValueError("lean mass must be between 20 and 200 kg")
    return 370.0 + 21.6 * lean_mass_kg


def tef_from_macros(protein_g: float, carbohydrate_g: float, fat_g: float) -> TefRange:
    for label, value in (
        ("protein", protein_g),
        ("carbohydrate", carbohydrate_g),
        ("fat", fat_g),
    ):
        if value < 0:
            raise ValueError(f"{label} cannot be negative")
    protein_kcal = protein_g * 4.0
    carbohydrate_kcal = carbohydrate_g * 4.0
    fat_kcal = fat_g * 9.0
    return TefRange(
        low=protein_kcal * 0.20 + carbohydrate_kcal * 0.05,
        midpoint=protein_kcal * 0.25 + carbohydrate_kcal * 0.075 + fat_kcal * 0.015,
        high=protein_kcal * 0.30 + carbohydrate_kcal * 0.10 + fat_kcal * 0.03,
    )


def planning_intake(
    ree_kcal: float,
    active_energy_kcal: float,
    goal_delta_kcal: float = 0,
    tef_fraction: float = 0.10,
) -> float:
    """Solve intake when TEF is modeled as a fraction of planned intake.

    goal_delta_kcal is negative for a deficit and positive for a surplus.
    This is valid only when active_energy_kcal excludes resting energy.
    """
    if ree_kcal <= 0 or active_energy_kcal < 0:
        raise ValueError("REE must be positive and active energy non-negative")
    if not 0 <= tef_fraction < 0.5:
        raise ValueError("TEF fraction must be between 0 and 0.5")
    numerator = ree_kcal + active_energy_kcal + goal_delta_kcal
    if numerator <= 0:
        raise ValueError("goal adjustment makes planned intake non-positive")
    return numerator / (1.0 - tef_fraction)


def retrospective_tdee(ree_kcal: float, active_energy_kcal: float, tef_kcal: float) -> float:
    if min(ree_kcal, active_energy_kcal, tef_kcal) < 0:
        raise ValueError("energy components cannot be negative")
    return ree_kcal + active_energy_kcal + tef_kcal


def formula_metadata() -> dict[str, str]:
    return {
        "formula_id": FORMULA_ID,
        "label": "estimated resting energy expenditure (REE; common BMR approximation)",
        "formula": "370 + 21.6 × lean_mass_kg",
    }
