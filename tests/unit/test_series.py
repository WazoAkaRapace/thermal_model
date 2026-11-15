"""Tests for the pure time-series helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.thermal_model.core.series import (
    Series,
    celsius_from,
    fill_gaps_hold,
    hold_last,
    resample_mean,
)

PARIS = ZoneInfo("Europe/Paris")
STEP = timedelta(minutes=15)


def test_celsius_conversions() -> None:
    """Every supported unit converts exactly; anything else is refused."""
    assert celsius_from(21.5, "°C") == 21.5
    assert celsius_from(32.0, "°F") == pytest.approx(0.0)
    assert celsius_from(212.0, "F") == pytest.approx(100.0)
    assert celsius_from(273.15, "K") == pytest.approx(0.0)
    with pytest.raises(ValueError):
        celsius_from(20.0, None)
    with pytest.raises(ValueError):
        celsius_from(20.0, "furlongs")


def test_resample_mean_averages_each_bucket() -> None:
    """Irregular samples are averaged into their UTC-aligned bucket."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    points = [
        (start + timedelta(minutes=1), 20.0),
        (start + timedelta(minutes=14), 22.0),
        (start + timedelta(minutes=20), 10.0),
        (start + timedelta(minutes=50), 14.0),
    ]

    series = resample_mean(points, step=STEP, start=start, end=end)

    assert series.length == 4
    assert series.values[0] == 21.0  # 20, 22
    assert series.values[1] == 10.0
    assert series.values[3] == 14.0
    assert series.step == STEP


def test_empty_bucket_is_missing_not_carried() -> None:
    """A hole stays a hole: coverage and gaps must be visible downstream."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    points = [(start + timedelta(minutes=1), 20.0), (start + timedelta(minutes=50), 18.0)]

    series = resample_mean(points, step=STEP, start=start, end=end)

    assert series.values[1] is None
    assert series.values[2] is None
    assert series.present == 2
    assert series.coverage() == pytest.approx(0.5)
    assert series.gaps() == [
        (start + STEP, start + 2 * STEP),
    ]


def test_resample_keeps_points_outside_the_window_out() -> None:
    """Only the requested window is returned."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    points = [
        (start - timedelta(hours=1), 99.0),
        (start + timedelta(minutes=5), 20.0),
        (end + timedelta(hours=1), 99.0),
    ]

    series = resample_mean(points, step=STEP, start=start, end=end)

    assert series.values == (20.0, None)
    assert 99.0 not in [value for value in series.values if value is not None]


def test_resample_rejects_bad_windows() -> None:
    """Programming errors surface immediately."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        resample_mean([], step=timedelta(0), start=moment, end=moment + STEP)
    with pytest.raises(ValueError):
        resample_mean([], step=STEP, start=moment, end=moment)


@pytest.mark.parametrize(
    ("year", "month", "day", "expected_hours"),
    [
        (2026, 10, 25, 25),  # autumn: the day is one hour longer
        (2026, 3, 29, 23),  # spring: one hour shorter
    ],
)
def test_local_day_is_23_or_25_hours_across_dst(
    year: int, month: int, day: int, expected_hours: int
) -> None:
    """The DST night has 23 or 25 hourly samples: no duplicated or skipped hour."""
    start_local = datetime(year, month, day, tzinfo=PARIS)
    end_local = datetime(year, month, day, tzinfo=PARIS) + timedelta(days=1)
    points = [
        (start_local + timedelta(minutes=10 * index), 20.0)
        for index in range(int((end_local - start_local).total_seconds() // 600))
    ]

    hourly = resample_mean(points, step=timedelta(hours=1), start=start_local, end=end_local)

    assert hourly.length == expected_hours
    assert hourly.step == timedelta(hours=1)
    assert len(set(hourly.times)) == hourly.length  # no duplicated instant
    consecutive = zip(hourly.times, hourly.times[1:], strict=False)
    assert all(later - earlier == timedelta(hours=1) for earlier, later in consecutive)


def test_hold_last_uses_the_last_known_state() -> None:
    """A thermostat state is piecewise constant: the last event wins."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    points = [
        (start, 1.0),
        (start + timedelta(minutes=20), 0.0),
    ]

    series = hold_last(points, step=STEP, start=start, end=end)

    assert series.values == (1.0, 1.0, 0.0, 0.0)


def test_hold_last_leaves_unknown_before_the_first_event() -> None:
    """Nothing is known before the first state event."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    points = [(start + timedelta(minutes=15), 1.0)]

    series = hold_last(points, step=STEP, start=start, end=end)

    assert series.values == (None, 1.0)


def test_mask_outliers_blanks_impossible_rates() -> None:
    """A window opening is not building physics."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    series = Series(
        times=tuple(start + index * STEP for index in range(5)),
        values=(20.0, 20.1, 25.0, 25.1, 25.2),
    )

    masked = series.mask_outliers(3.0)

    assert masked.values == (20.0, 20.1, None, 25.1, 25.2)


def test_fraction_true_and_slice() -> None:
    """Heating duty cycle and window selection."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    series = Series(
        times=tuple(start + index * STEP for index in range(4)),
        values=(1.0, 0.0, 1.0, None),
    )

    assert series.fraction_true() == pytest.approx(0.5)
    window = series.slice(start + STEP, start + 3 * STEP)
    assert window.values == (0.0, 1.0)


def test_series_requires_aware_datetimes() -> None:
    """A naive timestamp is a bug, not a detail."""
    with pytest.raises(ValueError):
        Series(times=(datetime(2026, 1, 1),), values=(20.0,))
    with pytest.raises(ValueError):
        Series(times=(datetime(2026, 1, 1, tzinfo=UTC),), values=(20.0, 21.0))


def test_fill_gaps_hold_is_bounded() -> None:
    """A control signal may be bridged, but only over a bounded gap."""
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    series = Series(
        times=tuple(start + index * STEP for index in range(6)),
        values=(1.0, None, None, None, None, 0.0),
    )

    bridged = fill_gaps_hold(series, max_gap_step=2)

    assert bridged.values == (1.0, 1.0, 1.0, None, None, 0.0)
    assert fill_gaps_hold(series).values == series.values
