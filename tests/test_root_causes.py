"""Non-regression tests for the two real-installation root causes.

1. The in-process recorder returns timestamps in seconds; the websocket API returns
   milliseconds. One conversion must handle both — dividing seconds by 1000 put the
   whole series in 1970 and silently killed the daily balance and the solar input.
2. A sensor without ``state_class`` has raw history but no statistics: the bound
   sensor must be read from its history, not replaced by a house-wide mean.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.thermal_model.sources.statistics import _to_moment

PAC = "sensor.pac_power"
JUNK_POWER = "sensor.lamp_power"

# 2026-11-15T21:00:00Z, as the two callers actually send it
SECONDS = 1794805200.0
MILLISECONDS = SECONDS * 1000


def test_a_seconds_timestamp_stays_in_2026() -> None:
    """The in-process recorder: `start` is the raw start_ts column, in seconds."""
    moment = _to_moment(SECONDS)
    assert isinstance(moment, datetime)
    assert moment.year == 2026, "seconds divided by 1000 land in January 1970"


def test_a_milliseconds_timestamp_stays_in_2026() -> None:
    """The websocket API converts to milliseconds before answering."""
    moment = _to_moment(MILLISECONDS)
    assert isinstance(moment, datetime)
    assert moment.year == 2026
    assert _to_moment(SECONDS) == _to_moment(MILLISECONDS)


def test_a_datetime_passes_through() -> None:
    """Some code paths already hand over datetimes."""
    now = datetime.now(UTC)
    assert _to_moment(now) is now
    assert _to_moment(None) is None
    assert _to_moment("nonsense") is None


def test_rows_become_points_whatever_the_unit() -> None:
    """Both row shapes must produce the same points."""
    from custom_components.thermal_model.sources.statistics import _rows_to_points

    rows = [{"start": MILLISECONDS, "mean": 20.5}, {"start": SECONDS + 3600, "mean": 20.6}]
    points = _rows_to_points(rows, prefer="mean")
    assert [p.value for p in points] == [20.5, 20.6]
    assert points[0].moment.year == 2026
    assert points[1].moment - points[0].moment == timedelta(hours=1)
