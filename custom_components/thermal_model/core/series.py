"""Time-series preparation: units, fixed-step resampling, gap marking, masks.

Everything here is pure: datetimes are injected and always timezone-aware, so
the module can be tested without Home Assistant and without a clock. The rules
that matter, and that a generic service cannot get away with:

- a missing sample becomes ``None`` (visible), never a filled or carried value
  silently invented;
- resampling is aligned on the *UTC* clock, so a DST night is 23 or 25 hourly
  samples, not a duplicated or skipped hour;
- unit conversion happens once, at the boundary, and is explicit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = [
    "Series",
    "celsius_from",
    "fill_gaps_hold",
    "hold_last",
    "resample_mean",
    "smart_round",
]

TEMPERATURE_TO_CELSIUS: dict[str, tuple[float, float]] = {
    # unit -> (scale, offset) : value_c = value * scale + offset
    "°c": (1.0, 0.0),
    "c": (1.0, 0.0),
    "°f": (5.0 / 9.0, -32.0 * 5.0 / 9.0),
    "f": (5.0 / 9.0, -32.0 * 5.0 / 9.0),
    "k": (1.0, -273.15),
}


def celsius_from(value: float, unit: str | None) -> float:
    """Convert a temperature to degrees Celsius.

    Unknown or missing units are refused rather than assumed: guessing between
    Celsius and Fahrenheit silently corrupts a whole model.
    """
    key = (unit or "").strip().lower()
    try:
        scale, offset = TEMPERATURE_TO_CELSIUS[key]
    except KeyError:
        raise ValueError(f"unsupported temperature unit: {unit!r}") from None
    return value * scale + offset


def smart_round(value: float, digits: int = 6) -> float:
    """Round without broadcasting floating-point noise into the fit."""
    return round(value, digits)


@dataclass(frozen=True, kw_only=True)
class Series:
    """A fixed-step time series; ``None`` marks a sample that does not exist."""

    times: tuple[datetime, ...]
    values: tuple[float | None, ...]

    def __post_init__(self) -> None:
        if len(self.times) != len(self.values):
            raise ValueError("times and values must have the same length")
        for moment in self.times:
            if moment.tzinfo is None:
                raise ValueError("times must be timezone-aware")

    @property
    def step(self) -> timedelta:
        """Sampling step (derived, always positive)."""
        if len(self.times) < 2:
            return timedelta(0)
        return self.times[1] - self.times[0]

    @property
    def length(self) -> int:
        """Number of samples, including the missing ones."""
        return len(self.times)

    @property
    def present(self) -> int:
        """Number of samples that actually carry a value."""
        return sum(1 for value in self.values if value is not None)

    def coverage(self) -> float:
        """Fraction of samples with a value, in [0, 1]."""
        return 0.0 if not self.times else self.present / self.length

    def gaps(self) -> list[tuple[datetime, datetime]]:
        """Contiguous ranges of missing samples, as ``(first, last)`` moments."""
        found: list[tuple[datetime, datetime]] = []
        start: datetime | None = None
        previous: datetime | None = None
        for moment, value in zip(self.times, self.values, strict=True):
            if value is None:
                if start is None:
                    start = moment
                previous = moment
            elif start is not None:
                found.append((start, previous or start))
                start = None
        if start is not None:
            found.append((start, previous or start))
        return found

    def mask_outliers(self, threshold_per_hour: float) -> Series:
        """Blank the samples whose rate of change exceeds ``threshold`` K/h.

        A window opening, a stove, a boiler test: they are not building physics
        and must not enter the identification.
        """
        if self.length < 2:
            return self
        step_hours = self.step.total_seconds() / 3600.0
        if step_hours <= 0:
            return self
        limit = threshold_per_hour * step_hours
        values = list(self.values)
        for index in range(1, self.length):
            current, previous = values[index], values[index - 1]
            if current is None or previous is None:
                continue
            if abs(current - previous) > limit:
                values[index] = None
        return Series(times=self.times, values=tuple(values))

    def fraction_true(self) -> float:
        """Fraction of samples above zero (how often the heating actually ran)."""
        if not self.values:
            return 0.0
        return sum(1 for value in self.values if value is not None and value > 0) / self.length

    def slice(self, start: datetime, end: datetime) -> Series:
        """Samples in ``[start, end)``."""
        selected = [
            (moment, value)
            for moment, value in zip(self.times, self.values, strict=True)
            if start <= moment < end
        ]
        return Series(
            times=tuple(moment for moment, _ in selected),
            values=tuple(value for _, value in selected),
        )


def _aligned(origin: datetime, step: timedelta) -> datetime:
    """Round a moment down to a UTC step boundary."""
    utc = origin.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    steps = int((utc - epoch).total_seconds() // step.total_seconds())
    return epoch + steps * step


def _bucket_index(moment: datetime, origin: datetime, step: timedelta) -> int:
    return int((moment.astimezone(UTC) - origin).total_seconds() // step.total_seconds())


def resample_mean(
    points: Iterable[tuple[datetime, float]],
    *,
    step: timedelta,
    start: datetime,
    end: datetime,
) -> Series:
    """Average the points falling in each bucket of ``step`` between start and end.

    Buckets are aligned on UTC boundaries; an empty bucket is ``None`` — never
    carried over from the previous one.
    """
    if step.total_seconds() <= 0:
        raise ValueError("step must be positive")
    if end <= start:
        raise ValueError("end must be after start")

    origin = _aligned(start, step)
    slot_count = _bucket_index(end, origin, step) + (1 if (end - origin) % step else 0)
    buckets: list[list[float]] = [[] for _ in range(max(slot_count, 0))]

    for moment, value in points:
        index = _bucket_index(moment, origin, step)
        if 0 <= index < len(buckets):
            buckets[index].append(value)

    times = tuple(origin + index * step for index in range(len(buckets)))
    values = tuple(smart_round(sum(bucket) / len(bucket)) if bucket else None for bucket in buckets)
    return Series(times=times, values=values)


def hold_last(
    points: Iterable[tuple[datetime, float]],
    *,
    step: timedelta,
    start: datetime,
    end: datetime,
) -> Series:
    """Sample a piecewise-constant signal (a heating state) on a fixed grid.

    The value at each grid point is the last known one *before* that point,
    which is how a thermostat's on/off state actually behaves. Nothing before
    ``start`` is known, so those samples stay missing.
    """
    if step.total_seconds() <= 0:
        raise ValueError("step must be positive")
    if end <= start:
        raise ValueError("end must be after start")

    ordered = sorted((moment.astimezone(UTC), value) for moment, value in points)
    origin = _aligned(start, step)
    slot_count = _bucket_index(end, origin, step) + (1 if (end - origin) % step else 0)

    times: list[datetime] = []
    values: list[float | None] = []
    cursor = 0
    current: float | None = None
    for index in range(max(slot_count, 0)):
        moment = origin + index * step
        while cursor < len(ordered) and ordered[cursor][0] <= moment:
            current = ordered[cursor][1]
            cursor += 1
        times.append(moment)
        values.append(current)
    return Series(times=tuple(times), values=tuple(values))


def fill_gaps_hold(series: Series, *, max_gap_step: int = 0) -> Series:
    """Carry the last value across *short* gaps only, and say so by construction.

    ``max_gap_step`` is the maximum number of consecutive missing samples that
    may be bridged (0 disables the behaviour). This is for a control signal that
    is known to be constant between events, never for a measurement.
    """
    if max_gap_step <= 0:
        return series
    values = list(series.values)
    last_known: float | None = None
    run = 0
    for index, value in enumerate(values):
        if value is None:
            run += 1
            if last_known is not None and run <= max_gap_step:
                values[index] = last_known
        else:
            last_known = value
            run = 0
    return Series(times=series.times, values=tuple(values))
