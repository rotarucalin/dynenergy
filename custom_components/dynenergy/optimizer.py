"""Domain models and charging plan logic for the battery optimizer."""

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


def create_greedy_charge_plan(
    inputs: OptimizerInputs,
    charge_price_threshold_per_kwh: float,
) -> OptimizationPlan:
    """Charge toward maximum SOC in the cheapest eligible 15-minute slots.

    Each price below the threshold is considered from lowest to highest. A slot
    receives the maximum energy permitted by its charge-power limit unless less
    energy is needed to reach the configured maximum SOC.
    """
    _validate_charge_inputs(inputs, charge_price_threshold_per_kwh)

    battery = inputs.battery
    target_energy_kwh = battery.usable_capacity_kwh * battery.max_soc_percent / 100
    stored_energy_kwh = (
        battery.usable_capacity_kwh * inputs.current_soc_percent / 100
    )
    charge_energy_by_index = [0.0] * len(inputs.timestamps)

    eligible_indices = sorted(
        (
            index
            for index, price in enumerate(inputs.prices_per_kwh)
            if price < charge_price_threshold_per_kwh
        ),
        key=lambda index: (inputs.prices_per_kwh[index], inputs.timestamps[index]),
    )
    max_charge_energy_kwh = battery.max_charge_power_kw * INTERVAL_HOURS

    for index in eligible_indices:
        energy_needed_from_grid_kwh = (
            target_energy_kwh - stored_energy_kwh
        ) / battery.charge_efficiency
        if energy_needed_from_grid_kwh <= 0:
            break

        charge_energy_kwh = min(max_charge_energy_kwh, energy_needed_from_grid_kwh)
        charge_energy_by_index[index] = charge_energy_kwh
        stored_energy_kwh += charge_energy_kwh * battery.charge_efficiency

    intervals: list[PlanInterval] = []
    expected_energy_kwh = (
        battery.usable_capacity_kwh * inputs.current_soc_percent / 100
    )
    for index, timestamp in enumerate(inputs.timestamps):
        charge_energy_kwh = charge_energy_by_index[index]
        expected_energy_kwh += charge_energy_kwh * battery.charge_efficiency
        target_power_kw = -charge_energy_kwh / INTERVAL_HOURS
        intervals.append(
            PlanInterval(
                timestamp=timestamp,
                price_per_kwh=inputs.prices_per_kwh[index],
                consumption_kwh=inputs.consumption_kwh[index],
                target_battery_power_kw=target_power_kw,
                expected_soc_percent=(
                    expected_energy_kwh / battery.usable_capacity_kwh * 100
                ),
                state=(
                    OperatingState.CHARGE
                    if charge_energy_kwh > 0
                    else OperatingState.IDLE
                ),
            )
        )

    planned_charge_intervals = [
        interval
        for interval in intervals
        if interval.state is OperatingState.CHARGE
    ]
    total_charge_kwh = sum(charge_energy_by_index)
    cost_without_battery = sum(
        price * consumption
        for price, consumption in zip(
            inputs.prices_per_kwh, inputs.consumption_kwh, strict=True
        )
    )
    charge_cost = sum(
        price * charge_energy
        for price, charge_energy in zip(
            inputs.prices_per_kwh, charge_energy_by_index, strict=True
        )
    )
    cost_with_battery = cost_without_battery + charge_cost

    return OptimizationPlan(
        intervals=intervals,
        summary=PlanSummary(
            total_charge_kwh=total_charge_kwh,
            total_discharge_kwh=0.0,
            highest_charge_price_per_kwh=(
                max(interval.price_per_kwh for interval in planned_charge_intervals)
                if planned_charge_intervals
                else None
            ),
            lowest_discharge_price_per_kwh=None,
            cost_without_battery=cost_without_battery,
            cost_with_battery=cost_with_battery,
            daily_saving=cost_without_battery - cost_with_battery,
        ),
    )


def _validate_charge_inputs(
    inputs: OptimizerInputs,
    charge_price_threshold_per_kwh: float,
) -> None:
    """Reject invalid physical limits and price-series shapes."""
    lengths = {
        len(inputs.timestamps),
        len(inputs.prices_per_kwh),
        len(inputs.consumption_kwh),
    }
    if len(lengths) != 1 or not inputs.timestamps:
        raise ValueError("Timestamps, prices, and consumption must be non-empty and aligned")

    battery = inputs.battery
    if battery.usable_capacity_kwh <= 0 or battery.max_charge_power_kw <= 0:
        raise ValueError("Battery capacity and charge power must be positive")
    if not 0 < battery.charge_efficiency <= 1:
        raise ValueError("Charge efficiency must be between 0 and 1")
    if not 0 <= battery.min_soc_percent < battery.max_soc_percent <= 100:
        raise ValueError("Battery SOC limits must be ordered percentages between 0 and 100")
    if not battery.min_soc_percent <= inputs.current_soc_percent <= battery.max_soc_percent:
        raise ValueError("Current SOC must be within the configured SOC limits")
    if charge_price_threshold_per_kwh < 0:
        raise ValueError("Charge price threshold must not be negative")