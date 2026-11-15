"""Pure-Python linear algebra for identification (no numpy, by decision).

The identification fits at most four parameters, so ordinary least squares via
normal equations plus a careful Gaussian elimination is both exact enough and
fast (a few milliseconds for hundreds of thousands of rows). What matters far
more than the solver is the *uncertainty* it reports: every parameter comes with
a standard error, because a parameter without an error bar cannot be trusted or
rejected.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "LeastSquaresResult",
    "SingularMatrixError",
    "least_squares",
    "solve",
]


class SingularMatrixError(ValueError):
    """Raised when the normal matrix cannot be inverted (degenerate design)."""


@dataclass(frozen=True, kw_only=True)
class LeastSquaresResult:
    """Fitted parameters, their uncertainty, and the quality of the fit."""

    params: tuple[float, ...]
    stderr: tuple[float | None, ...]
    n_observations: int
    residual_sum_squares: float
    r_squared: float | None

    def stderr_ratio(self) -> tuple[float | None, ...]:
        """Relative uncertainty per parameter (the identifiability criterion)."""
        ratios: list[float | None] = []
        for value, error in zip(self.params, self.stderr, strict=True):
            if error is None or value == 0.0:
                ratios.append(None)
            else:
                ratios.append(abs(error / value))
        return tuple(ratios)


def solve(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> list[float]:
    """Solve ``A x = b`` by Gaussian elimination with partial pivoting.

    ``matrix`` is square and is not modified. Raises ``SingularMatrixError`` when
    a pivot is numerically zero (rank-deficient design).
    """
    size = len(matrix)
    if size == 0 or len(vector) != size:
        raise ValueError("matrix and vector dimensions do not match")

    augmented = [[*row, float(vector[index])] for index, row in enumerate(matrix)]

    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        pivot = augmented[pivot_row][column]
        if abs(pivot) < 1e-12:
            raise SingularMatrixError("matrix is singular or rank deficient")
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]

        for row in range(column + 1, size):
            factor = augmented[row][column] / pivot
            if factor == 0.0:
                continue
            augmented[row][column] = 0.0
            for index in range(column + 1, size + 1):
                augmented[row][index] -= factor * augmented[column][index]

    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        total = augmented[row][size] - sum(
            augmented[row][column] * solution[column] for column in range(row + 1, size)
        )
        solution[row] = total / augmented[row][row]
    return solution


def invert(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """Invert a small square matrix by solving one column at a time."""
    size = len(matrix)
    columns = [
        solve(matrix, [1.0 if row == column else 0.0 for row in range(size)])
        for column in range(size)
    ]
    return [[columns[column][row] for column in range(size)] for row in range(size)]


def least_squares(
    rows: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    with_stderr: bool = True,
) -> LeastSquaresResult:
    """Fit ``y = X β`` in the least-squares sense, with standard errors.

    ``rows`` holds one regressor vector per observation; an intercept must be an
    explicit column of ones (the caller decides what is fixed). Regressors should
    be centred/scaled by the caller when their magnitudes differ widely: the
    normal equations square the condition number of the design.
    """
    if len(rows) != len(targets):
        raise ValueError("rows and targets must have the same length")
    if not rows:
        raise ValueError("no observations to fit")

    n_observations = len(rows)
    n_params = len(rows[0])
    if n_params == 0:
        raise ValueError("empty regressor rows")
    if n_observations < n_params:
        raise ValueError("fewer observations than parameters")

    xtx = [[0.0] * n_params for _ in range(n_params)]
    xty = [0.0] * n_params
    for row, target in zip(rows, targets, strict=True):
        if len(row) != n_params:
            raise ValueError("inconsistent regressor row length")
        for i in range(n_params):
            value = row[i]
            xty[i] += value * target
            for j in range(i, n_params):
                xtx[i][j] += value * row[j]
    for i in range(n_params):
        for j in range(i):
            xtx[i][j] = xtx[j][i]

    params = solve(xtx, xty)

    residuals = [
        target - sum(coef * value for coef, value in zip(params, row, strict=True))
        for row, target in zip(rows, targets, strict=True)
    ]
    rss = sum(residual * residual for residual in residuals)

    mean = sum(targets) / n_observations
    tss = sum((target - mean) ** 2 for target in targets)
    r_squared = None if tss == 0.0 else 1.0 - rss / tss

    stderr: tuple[float | None, ...]
    if with_stderr and n_observations > n_params:
        variance = rss / (n_observations - n_params)
        try:
            xtx_inverse = invert(xtx)
        except SingularMatrixError:
            stderr = tuple(None for _ in params)
        else:
            stderr = tuple(
                math.sqrt(variance * xtx_inverse[i][i]) if xtx_inverse[i][i] > 0 else None
                for i in range(n_params)
            )
    else:
        stderr = tuple(None for _ in params)

    return LeastSquaresResult(
        params=tuple(params),
        stderr=stderr,
        n_observations=n_observations,
        residual_sum_squares=rss,
        r_squared=r_squared,
    )
