"""Constants for the Thermal Model integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "Thermal Model"
DOMAIN_KEY: Final = "thermal_model"

# Platforms forwarded once runtime data exists (grows with the milestones).
PLATFORMS: Final[list[str]] = ["sensor", "number", "button"]

# --------------------------------------------------------------------------
# Configuration keys
# --------------------------------------------------------------------------
CONF_NAME: Final = "name"
CONF_LATITUDE: Final = "latitude"
CONF_LONGITUDE: Final = "longitude"

# Source binding (step 1 - the three mandatory entities)
CONF_HEATING_ENTITY: Final = "heating_entity"
CONF_INDOOR_SENSOR: Final = "indoor_sensor"
CONF_OUTDOOR_SENSOR: Final = "outdoor_sensor"

# Optional accuracy inputs (step 2)
CONF_HEATING_POWER_SENSOR: Final = "heating_power_sensor"
CONF_RATED_POWER: Final = "rated_power"
CONF_COP: Final = "cop"
CONF_IRRADIANCE_SENSOR: Final = "irradiance_sensor"

# Identification window (step 3)
CONF_HISTORY_DAYS: Final = "history_days"
CONF_DT_MINUTES: Final = "dt_minutes"
CONF_DT_STORAGE_MINUTES: Final = "dt_storage_minutes"
CONF_SMOOTH_HOURS: Final = "smooth_hours"
CONF_TAU_GRACE_MINUTES: Final = "tau_grace_minutes"
CONF_RETENTION_DAYS: Final = "retention_days"
CONF_MIN_PASSIVE_COVERAGE: Final = "min_passive_coverage"
CONF_OUTLIER_THRESHOLD: Final = "outlier_threshold"
CONF_AUTO_IDENTIFY_WEEKLY: Final = "auto_identify_weekly"

# Weather sources (step 4) / scenarios (step 5) - consumed from M3 onwards
CONF_WEATHER_MODELS: Final = "weather_models"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_SCENARIOS: Final = "scenarios"

# Forecast tuning (runtime, exposed as numbers)
CONF_HORIZON_HOURS: Final = "horizon_hours"
CONF_SCENARIO_SETPOINT: Final = "scenario_setpoint"
CONF_HYSTERESIS: Final = "hysteresis"
CONF_CAPACITY: Final = "capacity"

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------
DEFAULT_NAME: Final = "Thermal Model"
DEFAULT_RATED_POWER: Final = 1000.0  # W
DEFAULT_COP: Final = 1.0  # direct electric; heat pumps typically 3.0-4.0
DEFAULT_HISTORY_DAYS: Final = 21
DEFAULT_DT_MINUTES: Final = 15
DEFAULT_DT_STORAGE_MINUTES: Final = 5
DEFAULT_SMOOTH_HOURS: Final = 0.0
DEFAULT_TAU_GRACE_MINUTES: Final = 30
DEFAULT_RETENTION_DAYS: Final = 400
DEFAULT_MIN_PASSIVE_COVERAGE: Final = 0.25
DEFAULT_OUTLIER_THRESHOLD: Final = 3.0  # K/h
DEFAULT_AUTO_IDENTIFY_WEEKLY: Final = False
DEFAULT_WEATHER_MODELS: Final[list[str]] = ["best_match", "arome_france"]
DEFAULT_SCENARIOS: Final[list[str]] = ["free_float", "thermostat"]

# Weather provider (Open-Meteo: no key, several comparable models)
WEATHER_API_URL: Final = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT: Final = 20
DEFAULT_WEATHER_MODEL: Final = "best_match"

# Where the archived forecast runs live, relative to the configuration directory
STORAGE_SUBDIR: Final = "thermal_model"
RUNS_RETENTION_DAYS: Final = 14
RUNS_KEPT_PER_DAY: Final = 4

# Forecast defaults
DEFAULT_PARAM_A: Final = 0.016  # /h, calibrated on the reference house
DEFAULT_PARAM_C_S: Final = 0.0023  # kWh/(m²·K)-free internal gain weight
DEFAULT_PARAM_D: Final = 0.04  # K/h, internal + solar gains
DEFAULT_PARAM_K: Final = 0.49  # °C per kWh of heating
MIN_PARAM_A: Final = 0.001
MAX_PARAM_A: Final = 0.200
MIN_PARAM_C_S: Final = 0.0005
MAX_PARAM_C_S: Final = 0.0500
MIN_PARAM_D: Final = -0.50
MAX_PARAM_D: Final = 0.50
MIN_PARAM_K: Final = 0.0
MAX_PARAM_K: Final = 5.0
DEFAULT_HORIZON_HOURS: Final = 48
MIN_HORIZON_HOURS: Final = 6
MAX_HORIZON_HOURS: Final = 120
DEFAULT_SCENARIO_SETPOINT: Final = 20.5
MIN_SETPOINT: Final = 10.0
MAX_SETPOINT: Final = 30.0
DEFAULT_HYSTERESIS: Final = 0.4
MIN_HYSTERESIS: Final = 0.1
MAX_HYSTERESIS: Final = 3.0
DEFAULT_CAPACITY: Final = 2.0  # kW
MIN_CAPACITY: Final = 0.1
MAX_CAPACITY: Final = 30.0
# Commandable heating entities only have an on/off state: the capacity is how
# much heat they deliver while on.
FORECAST_REFRESH_MINUTES: Final = 30

# Bounds
MIN_HISTORY_DAYS: Final = 5
MAX_HISTORY_DAYS: Final = 400
MIN_DT_MINUTES: Final = 5
MAX_DT_MINUTES: Final = 60
MIN_SMOOTH_HOURS: Final = 0.0
MAX_SMOOTH_HOURS: Final = 12.0
MIN_TAU_GRACE_MINUTES: Final = 0
MAX_TAU_GRACE_MINUTES: Final = 360
MIN_RETENTION_DAYS: Final = 7
MAX_RETENTION_DAYS: Final = 3650
MIN_RATED_POWER: Final = 50.0
MAX_RATED_POWER: Final = 100_000.0
MIN_COP: Final = 0.5
MAX_COP: Final = 8.0

# --------------------------------------------------------------------------
# Heating entity families
# --------------------------------------------------------------------------
HEATING_DOMAINS: Final = ["climate", "water_heater", "switch", "input_boolean"]

TEMPERATURE_UNITS: Final = {"°C", "C", "°F", "F", "K", "c", "f", "k"}


class HeatingFamily(StrEnum):
    """How the heating state is read for each supported domain."""

    CLIMATE = "climate"
    WATER_HEATER = "water_heater"
    SWITCH = "switch"
    INPUT_BOOLEAN = "input_boolean"


class Identifiability(StrEnum):
    """How trustworthy an identified parameter set is (published, never hidden)."""

    IDENTIFIED = "identified"
    CONSTRAINED = "constrained"  # degenerate window: parameters constrained by physics
    K_NOT_IDENTIFIED = "k_not_identified"  # heating gain could not be identified
    K_ESTIMATED_FROM_ONOFF = "k_estimated_from_onoff"  # modulating heater, on/off power proxy
    INSUFFICIENT_DATA = "insufficient_data"


class ModelScenario(StrEnum):
    """How the heating is assumed to behave in a forecast."""

    FREE_FLOAT = "free_float"  # heating never runs
    THERMOSTAT = "thermostat"  # hysteresis emulation, bounded by capacity
    OBSERVED_DUTY = "observed_duty"  # replay of the observed duty cycle


class ReasonCode(StrEnum):
    """Machine-readable reason attached to every published result."""

    OK = "ok"
    GUARD_PENDING = "guard_pending"  # presence/absence not confirmed yet (anti-flap)
    ABSENCE = "absence"
    PAC_UNAVAILABLE = "pac_unavailable"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INSUFFICIENT_PASSIVE_COVERAGE = "insufficient_passive_coverage"
    MODEL_NOT_IDENTIFIABLE = "model_not_identifiable"
    K_NOT_IDENTIFIED = "k_not_identified"
    FORECAST_STALE = "forecast_stale"
    MODEL_DIVERGED = "model_diverged"
    RECORDER_MISSING = "recorder_missing"
    UNSUPPORTED_HEATING_ENTITY = "unsupported_heating_entity"
    UNIT_MISMATCH = "unit_mismatch"
