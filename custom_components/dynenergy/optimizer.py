"""Domain models and baseline consumption profile for the battery optimizer.

The optimization algorithm is intentionally not implemented yet. These immutable
models define the input and output contract that the Home Assistant coordinator
and any future solver will use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Sequence

INTERVAL_HOURS = 0.25
WORKDAY_CONSUMPTION_KWH = 0.325


class OperatingState(StrEnum):
    """Requested operating mode for a schedule interval."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class BatteryParameters:
    """Physical and economic limits for a battery."""

    usable_capacity_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    min_soc_percent: float
    max_soc_percent: float
    charge_efficiency: float
    discharge_efficiency: float
    degradation_cost_per_kwh: float = 0.0


@dataclass(frozen=True, slots=True)
class OptimizerInputs:
    """Inputs required to produce a chronologically valid day-ahead plan."""

    timestamps: Sequence[datetime]
    prices_per_kwh: Sequence[float]
    consumption_kwh: Sequence[float]
    current_soc_percent: float
    battery: BatteryParameters


@dataclass(frozen=True, slots=True)
class PlanInterval:
    """One 15-minute optimizer decision."""

    timestamp: datetime
    price_per_kwh: float
    consumption_kwh: float
    target_battery_power_kw: float
    expected_soc_percent: float
    state: OperatingState


@dataclass(frozen=True, slots=True)
class PlanSummary:
    """Aggregate values exposed alongside a day-ahead plan."""

    total_charge_kwh: float
    total_discharge_kwh: float
    highest_charge_price_per_kwh: float | None
    lowest_discharge_price_per_kwh: float | None
    cost_without_battery: float
    cost_with_battery: float
    daily_saving: float


@dataclass(frozen=True, slots=True)
class OptimizationPlan:
    """Solver result for one complete EPEX day-ahead price horizon."""

    intervals: Sequence[PlanInterval]
    summary: PlanSummary


def default_consumption_kwh(timestamp: datetime) -> float:
    """Return the supplied weekday consumption profile for one interval."""
    weekday = timestamp.weekday()
    hour = timestamp.hour

    if weekday < 4 and 8 <= hour < 19:
        return WORKDAY_CONSUMPTION_KWH
    if weekday == 4 and 8 <= hour < 15:
        return WORKDAY_CONSUMPTION_KWH
    return 0.0