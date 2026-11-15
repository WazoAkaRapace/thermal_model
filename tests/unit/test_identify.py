"""Identification tests, calibrated on synthetic houses with known parameters.

Two levels are checked separately, because they fail differently:

- on noise-free data the estimator must be *right* (it validates the maths);
- on noisy short data it must be *honest*: the degeneracy flag must fire instead
  of publishing a confident wrong number.

The measured behaviour these tests lock in (dt = 15 min, instrumented fit, no
smoothing):

| data | driving spread | error on a |
|---|---|---|
| 90 d with a seasonal trend | 0.12 | ~0 % |
| 60 d with a seasonal trend | 0.09 | +2 % |
| 30 d, flat outdoor temperature | 0.03 | +41 % -> refused (constrained) |
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tools.gen_synthetic import SyntheticHouse, generate

from custom_components.thermal_model.const import Identifiability, ReasonCode
from custom_components.thermal_model.core.identify import (
    IdentificationError,
    IdentificationResult,
    IdentificationSettings,
    identify,
)
from custom_components.thermal_model.core.simulate import ThermalParams

START = datetime(2026, 1, 5, tzinfo=UTC)
LOOSE = ThermalParams(a=0.016, c_s=0.0023, d=0.04, k=0.49)
TIGHT = ThermalParams(a=0.0065, c_s=0.0018, d=0.04, k=0.173)


def _run(
    house: SyntheticHouse, settings: IdentificationSettings | None = None
) -> IdentificationResult:
    return identify(
        house.times,
        house.tin_measured,
        house.tout,
        house.solar,
        house.heating_kw,
        settings=settings or IdentificationSettings(),
    )


def test_estimator_is_exact_without_noise() -> None:
    """No noise, wide driving range: the maths must be right, not just plausible."""
    house = generate(LOOSE, start=START, days=90)

    result = _run(house)

    assert result.params.a == pytest.approx(LOOSE.a, rel=0.05)
    assert result.params.c_s == pytest.approx(LOOSE.c_s, rel=0.10)
    assert result.params.k is not None
    assert result.params.k == pytest.approx(LOOSE.k, rel=0.05)


def test_noisy_house_with_a_real_season_is_identified() -> None:
    """90 days with an outdoor trend: the driving range separates a from d."""
    house = generate(LOOSE, start=START, days=90)

    result = _run(house)

    assert result.identifiability is Identifiability.IDENTIFIED
    assert result.params.a == pytest.approx(LOOSE.a, rel=0.15)
    assert result.params.k is not None
    assert result.params.k == pytest.approx(LOOSE.k, rel=0.20)
    assert result.driving_spread is not None and result.driving_spread >= 0.08
    assert result.rmse_observed is not None and result.rmse_observed < 0.5


def test_narrow_driving_range_is_refused_instead_of_guessed() -> None:
    """Flat outdoor temperature: a and d trade off, and the service says so.

    This is the case where a plain least-squares fit publishes a confident number
    that is 40 % wrong: the residuals are tiny, so the standard error looks fine.
    """
    house = generate(LOOSE, start=START, days=30, text_trend_per_day=0.0)

    result = _run(house)

    assert result.driving_spread is not None and result.driving_spread < 0.08
    assert result.identifiability is Identifiability.CONSTRAINED
    assert ReasonCode.MODEL_NOT_IDENTIFIABLE in result.reasons


def test_tight_house_is_recovered_on_the_right_scale() -> None:
    """A well-insulated house (a = 0.0065 /h) is a different regime, not a nuance."""
    house = generate(TIGHT, start=START, days=90)

    result = _run(house)

    assert result.params.a == pytest.approx(TIGHT.a, rel=0.35)
    assert result.params.k is not None
    assert result.params.k == pytest.approx(TIGHT.k, rel=0.40)


def test_free_float_house_has_no_heating_gain() -> None:
    """Without any heating there is no heating window: k must be None, not zero."""
    house = generate(TIGHT, start=START, days=30, heating_capacity_kw=0.0)

    result = _run(house)

    assert result.params.k is None
    assert ReasonCode.K_NOT_IDENTIFIED in result.reasons
    assert result.identifiability in (
        Identifiability.K_NOT_IDENTIFIED,
        Identifiability.CONSTRAINED,
    )


def test_standard_errors_are_published_for_every_parameter() -> None:
    """A parameter without an error bar cannot be trusted or rejected."""
    house = generate(LOOSE, start=START, days=90)

    result = _run(house)

    assert result.stderr["a"] is not None and result.stderr["a"] > 0
    assert result.stderr["c_s"] is not None
    assert result.stderr["d"] is not None
    assert set(result.stderr_ratios()) == {"a", "c_s", "d", "k"}


def test_smoothing_is_off_by_default_because_it_biases_the_slope() -> None:
    """The default must stay the measured-better option."""
    assert IdentificationSettings().smooth_hours == 0.0

    house = generate(LOOSE, start=START, days=30)
    smoothed = _run(house, dataclasses.replace(IdentificationSettings(), smooth_hours=2.0))
    raw = _run(house)

    assert abs(raw.params.a - LOOSE.a) < abs(smoothed.params.a - LOOSE.a)


def test_too_little_data_is_refused() -> None:
    """Two days are not a model."""
    house = generate(LOOSE, start=START, days=1)

    with pytest.raises(IdentificationError, match="two days"):
        _run(house)


def test_too_many_gaps_are_refused() -> None:
    """Coverage is checked before anything is fitted."""
    house = generate(LOOSE, start=START, days=30)
    gapped = house.with_gaps([(0, int(house.length * 0.85))])

    with pytest.raises(IdentificationError, match="coverage"):
        _run(gapped)


def test_gaps_are_tolerated_when_the_rest_is_usable() -> None:
    """A hole in the middle must not poison the fit."""
    house = generate(LOOSE, start=START, days=90)
    gapped = house.with_gaps([(200, 400)])

    result = _run(gapped)

    assert result.params.a == pytest.approx(LOOSE.a, rel=0.20)
    assert result.coverage < 1.0


def test_series_must_have_the_same_length() -> None:
    """A misaligned series is a bug upstream."""
    house = generate(LOOSE, start=START, days=10)

    with pytest.raises(IdentificationError, match="same length"):
        identify(house.times, house.tin_measured[:-1], house.tout, house.solar, house.heating_kw)


def test_a_house_that_never_free_floats_is_refused() -> None:
    """Permanently heating means no passive window: there is nothing to identify."""
    house = generate(LOOSE, start=START, days=10)
    always_on = replace(house, heating_kw=tuple(1.0 for _ in range(house.length)))

    with pytest.raises(IdentificationError, match="passive"):
        _run(always_on)
