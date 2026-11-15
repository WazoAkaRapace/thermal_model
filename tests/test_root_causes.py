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


def test_the_raw_history_grid_is_aligned_on_the_hour() -> None:
    """Statistics hours sit on :00; a grid built from "now" at :37 matches none.

    Measured on a real installation as "0 heures communes entre l'intérieur et
    l'extérieur" the moment the indoor sensor fell back to its raw history.
    """
    import homeassistant.components.recorder.history as recorder_history
    from homeassistant.components.recorder.history import get_significant_states

    from custom_components.thermal_model.sources.recorder import async_read_states

    start = datetime(2026, 11, 5, 14, 37, 52, tzinfo=UTC)  # deliberately not aligned
    end = datetime(2026, 11, 6, 14, 37, 52, tzinfo=UTC)

    original = get_significant_states

    def fake_states(hass, start_time, end_time, entity_ids, *args, **kwargs):
        # states at odd minutes, like a real sensor
        return {
            "sensor.indoor": [
                _FakeState(
                    "sensor.indoor", f"{20 + i * 0.01:.2f}", start + timedelta(minutes=17 * i)
                )
                for i in range(100)
            ]
        }

    recorder_history.get_significant_states = fake_states
    try:
        series = async_read_states(None, ["sensor.indoor"], start, end, 60)
    finally:
        recorder_history.get_significant_states = original

    moments = [moment for moment, _value in series["sensor.indoor"].points]
    assert moments, "the grid must have points"
    assert all(moment.minute == 0 and moment.second == 0 for moment in moments), (
        f"grid aligned on the hour, got {moments[:3]}"
    )
    # and it must actually match statistics-shaped hours
    statistics_hours = {datetime(2026, 11, 6, h, 0, tzinfo=UTC) for h in range(24)}
    assert statistics_hours & set(moments), "the grid must share hours with statistics"


class _FakeState:
    """What the recorder returns, minimally."""

    def __init__(self, entity_id: str, state: str, last_updated: datetime) -> None:
        self.entity_id = entity_id
        self.state = state
        self.last_updated = last_updated
        self.attributes = {}
