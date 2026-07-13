from __future__ import annotations

import pytest

from oha.energy import (
    estimated_ree,
    formula_metadata,
    planning_intake,
    retrospective_tdee,
    tef_from_macros,
)


def test_cunningham_1991_ree_and_metadata() -> None:
    assert estimated_ree(60) == pytest.approx(1666)
    assert formula_metadata()["formula_id"] == "cunningham_1991_ffm"
    assert "370 + 21.6" in formula_metadata()["formula"]


@pytest.mark.parametrize("lean_mass", [0, 19.9, 200.1, 250])
def test_ree_rejects_implausible_lean_mass(lean_mass: float) -> None:
    with pytest.raises(ValueError):
        estimated_ree(lean_mass)


def test_macro_specific_tef_range() -> None:
    result = tef_from_macros(protein_g=100, carbohydrate_g=200, fat_g=50)
    assert result.low == pytest.approx(120)
    assert result.midpoint == pytest.approx(166.75)
    assert result.high == pytest.approx(213.5)
    assert result.low < result.midpoint < result.high


def test_tef_rejects_negative_macros() -> None:
    with pytest.raises(ValueError):
        tef_from_macros(10, -1, 5)


def test_planning_intake_solves_tef_without_double_counting() -> None:
    planned = planning_intake(ree_kcal=1600, active_energy_kcal=500, goal_delta_kcal=-300, tef_fraction=0.10)
    assert planned == pytest.approx(2000)
    assert planned * 0.90 == pytest.approx(1600 + 500 - 300)
    assert retrospective_tdee(1600, 500, 175) == pytest.approx(2275)


@pytest.mark.parametrize(
    "args",
    [
        (0, 100, 0, 0.1),
        (1600, -1, 0, 0.1),
        (1600, 100, 0, -0.1),
        (1600, 100, 0, 0.5),
        (100, 0, -1000, 0.1),
    ],
)
def test_planning_intake_rejects_invalid_inputs(args: tuple[float, float, float, float]) -> None:
    with pytest.raises(ValueError):
        planning_intake(*args)
