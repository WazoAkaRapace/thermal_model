"""Tests for the pure-Python least-squares solver."""

from __future__ import annotations

import math
import random
import time

import pytest

from custom_components.thermal_model.core.linalg import (
    LeastSquaresResult,
    SingularMatrixError,
    least_squares,
    solve,
)


def test_solve_matches_analytic_solution() -> None:
    """2x + y = 3 and x + 2y = 4 give x = 2/3, y = 5/3."""
    result = solve([[2.0, 1.0], [1.0, 2.0]], [3.0, 4.0])
    assert result == pytest.approx([2.0 / 3.0, 5.0 / 3.0])


def test_solve_swaps_rows_when_pivot_is_zero() -> None:
    """A zero on the diagonal must not make the solver fail."""
    # 0·x + 1·y = 2 ; 1·x + 0·y = 3  ->  x = 3, y = 2
    assert solve([[0.0, 1.0], [1.0, 0.0]], [2.0, 3.0]) == pytest.approx([3.0, 2.0])


def test_solve_ill_conditioned_matches_constructed_solution() -> None:
    """A near-singular but solvable 4x4 system recovers its constructed solution."""
    matrix = [
        [1.0, 1.0 / 2.0, 1.0 / 3.0, 1.0 / 4.0],
        [1.0 / 2.0, 1.0 / 3.0, 1.0 / 4.0, 1.0 / 5.0],
        [1.0 / 3.0, 1.0 / 4.0, 1.0 / 5.0, 1.0 / 6.0],
        [1.0 / 4.0, 1.0 / 5.0, 1.0 / 6.0, 1.0 / 7.0],
    ]
    expected = [1.0, -2.0, 3.0, -4.0]
    vector = [
        sum(coef * value for coef, value in zip(row, expected, strict=True)) for row in matrix
    ]

    assert solve(matrix, vector) == pytest.approx(expected, rel=1e-6)


def test_solve_raises_on_rank_deficient_matrix() -> None:
    """A duplicated row is rank deficiency, not a small pivot."""
    with pytest.raises(SingularMatrixError):
        solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])


def test_solve_rejects_mismatched_dimensions() -> None:
    """Programming errors surface immediately."""
    with pytest.raises(ValueError):
        solve([[1.0, 2.0]], [1.0, 2.0])


def test_least_squares_recovers_exact_plane() -> None:
    """With no noise, a plane y = 2 + 3·x1 − x2 is recovered exactly."""
    rows = [[1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [1.0, 0.0, 1.0], [1.0, 2.0, 3.0], [1.0, -1.0, 2.0]]
    targets = [2.0 + 3.0 * row[1] - row[2] for row in rows]

    fit = least_squares(rows, targets)
    assert isinstance(fit, LeastSquaresResult)
    assert fit.params == pytest.approx([2.0, 3.0, -1.0])
    assert fit.r_squared == pytest.approx(1.0)
    assert fit.residual_sum_squares == pytest.approx(0.0, abs=1e-18)
    assert fit.n_observations == 5


def test_least_squares_noise_gives_finite_standard_errors() -> None:
    """Standard errors must exist whenever the fit has spare degrees of freedom."""
    rng = random.Random(4)
    rows: list[list[float]] = []
    targets: list[float] = []
    for _ in range(400):
        x1 = rng.uniform(-10.0, 10.0)
        x2 = rng.uniform(0.0, 5.0)
        rows.append([1.0, x1, x2])
        targets.append(2.0 + 3.0 * x1 - x2 + rng.gauss(0.0, 0.1))

    fit = least_squares(rows, targets)
    assert fit.params == pytest.approx([2.0, 3.0, -1.0], abs=0.05)
    assert all(error is not None and error > 0 for error in fit.stderr)
    assert fit.r_squared is not None and fit.r_squared > 0.99


def test_stderr_ratio_is_the_identifiability_criterion() -> None:
    """Collinear regressors make both parameters unidentifiable: SE/|β| explodes."""
    rng = random.Random(11)
    rows: list[list[float]] = []
    targets: list[float] = []
    for _ in range(200):
        x = rng.uniform(0.0, 20.0)
        near_duplicate = x + rng.gauss(0.0, 1e-3)
        rows.append([1.0, x, near_duplicate])
        targets.append(2.0 + 0.5 * x + rng.gauss(0.0, 0.05))

    fit = least_squares(rows, targets)
    ratios = fit.stderr_ratio()

    assert ratios[1] is not None and ratios[1] > 0.5
    assert ratios[2] is not None and ratios[2] > 0.5
    # The fit itself is still good: only the split between the two columns is unknown.
    assert fit.r_squared is not None and fit.r_squared > 0.99


def test_stderr_disabled_on_request() -> None:
    """Callers that only need the point estimate can skip the covariance work."""
    fit = least_squares(
        [[1.0, float(i)] for i in range(10)], [float(i) for i in range(10)], with_stderr=False
    )
    assert fit.stderr == (None, None)


def test_least_squares_rejects_too_few_observations() -> None:
    """You cannot fit three parameters from two points."""
    with pytest.raises(ValueError):
        least_squares([[1.0, 0.0, 0.0], [1.0, 1.0, 1.0]], [1.0, 2.0])


def test_least_squares_is_fast_enough_for_years_of_data() -> None:
    """Budget from the plan: 400 days at 5-min resolution in well under a second."""
    rng = random.Random(7)
    rows = [
        [1.0, rng.uniform(-20.0, 5.0), rng.uniform(0.0, 800.0), rng.uniform(0.0, 3.0)]
        for _ in range(115_200)
    ]
    targets = [rng.gauss(0.0, 1.0) for _ in range(115_200)]

    started = time.perf_counter()
    fit = least_squares(rows, targets)
    elapsed = time.perf_counter() - started

    assert len(fit.params) == 4
    assert math.isfinite(fit.residual_sum_squares)
    assert elapsed < 2.0, f"least_squares too slow: {elapsed:.2f}s"
