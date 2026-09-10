"""Domain models and charging plan logic for the battery optimizer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from math import floor
from typing import Sequence

INTERVAL_HOURS = 0.25
INTERVALS_PER_HOUR = int(1 / INTERVAL_HOURS)
INTERVAL_MINUTES = int(INTERVAL_HOURS * 60)
INTERVALS_PER_DAY = 24 * INTERVALS_PER_HOUR
INTERVALS_PER_WEEK = 7 * INTERVALS_PER_DAY
ACTIVE_CONSUMPTION_KWH = 1.25 * INTERVAL_HOURS
IDLE_CONSUMPTION_KWH = 0.06 * INTERVAL_HOURS
MAX_CHARGE_PRICE_PER_KWH = 0.10
MIN_DISCHARGE_PRICE_PER_KWH = 0.13
CHARGE_PRICE_BAND = 0.25
PRE_CHARGE_DISCHARGE_PRICE_BAND = 0.50
POST_CHARGE_DISCHARGE_PRICE_BAND = 0.70
CHARGE_PRICE_BUCKET_WIDTH = 0.01
CHARGE_MARGIN_FRACTION = 0.20
# Allocations below this are rounding residue, not a usable battery instruction.
ENERGY_EPSILON_KWH = 1e-9


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
class PriceThresholds:
    """Daily price thresholds derived from the available EPEX price range."""

    charge_per_kwh: float
    pre_charge_discharge_per_kwh: float
    post_charge_discharge_per_kwh: float


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
    full_charge_feasible: bool


@dataclass(frozen=True, slots=True)
class OptimizationPlan:
    """Solver result for one complete EPEX day-ahead price horizon."""

    intervals: Sequence[PlanInterval]
    summary: PlanSummary


@dataclass(frozen=True, slots=True)
class WeeklyConsumptionProfile:
    """Learned consumption averages for every 15-minute slot of a week."""

    values_kwh: tuple[float, ...]
    sample_counts: tuple[int, ...]

    @classmethod
    def default(cls) -> WeeklyConsumptionProfile:
        """Create the initial weekday profile before real samples are available."""
        values = tuple(
            _default_consumption_for_slot(weekday, slot)
            for weekday in range(7)
            for slot in range(INTERVALS_PER_DAY)
        )
        return cls(values, (0,) * INTERVALS_PER_WEEK)

    @classmethod
    def from_dict(
        cls, data: Mapping[str, object] | None
    ) -> WeeklyConsumptionProfile:
        """Restore a profile, falling back to defaults for invalid payloads."""
        if not data:
            return cls.default()
        raw_values = data.get("values_kwh")
        raw_counts = data.get("sample_counts")
        if not isinstance(raw_values, list) or not isinstance(raw_counts, list):
            return cls.default()
        if (
            len(raw_values) != INTERVALS_PER_WEEK
            or len(raw_counts) != INTERVALS_PER_WEEK
        ):
            return cls.default()
        try:
            values = tuple(max(0.0, float(value)) for value in raw_values)
            counts = tuple(max(0, int(count)) for count in raw_counts)
        except (TypeError, ValueError):
            return cls.default()
        return cls(values, counts)

    def as_dict(self) -> dict[str, list[float] | list[int]]:
        """Serialize the learned profile for Home Assistant storage."""
        return {
            "values_kwh": list(self.values_kwh),
            "sample_counts": list(self.sample_counts),
        }

    def consumption_kwh(self, timestamp: datetime) -> float:
        """Return typical energy consumption for the timestamp's weekly slot."""
        return self.values_kwh[weekly_slot_index(timestamp)]

    def record(
        self, timestamp: datetime, consumption_kwh: float
    ) -> WeeklyConsumptionProfile:
        """Return a profile with one real sample added to a weekly slot."""
        index = weekly_slot_index(timestamp)
        sample_count = self.sample_counts[index]
        average_kwh = self.values_kwh[index]
        updated_average_kwh = (
            average_kwh * sample_count + max(0.0, consumption_kwh)
        ) / (sample_count + 1)
        values = list(self.values_kwh)
        counts = list(self.sample_counts)
        values[index] = updated_average_kwh
        counts[index] = sample_count + 1
        return WeeklyConsumptionProfile(tuple(values), tuple(counts))


def weekly_slot_index(timestamp: datetime) -> int:
    """Return the fixed weekly slot index for a local timestamp."""
    slot_of_day = timestamp.hour * INTERVALS_PER_HOUR + (
        timestamp.minute // INTERVAL_MINUTES
    )
    return timestamp.weekday() * INTERVALS_PER_DAY + slot_of_day


def target_power_w(interval: PlanInterval) -> int:
    """Return the signed Watt recommendation written to the battery helper."""
    return int(interval.target_battery_power_kw * INTERVALS_PER_HOUR * 1000)


def interval_power_limit_kw(hourly_power_limit_kw: float | None) -> float | None:
    """Scale an hourly power-limit setting to one optimizer interval."""
    if hourly_power_limit_kw is None:
        return None
    return hourly_power_limit_kw / INTERVALS_PER_HOUR


def default_consumption_kwh(timestamp: datetime) -> float:
    """Return the default weekday consumption profile for one interval."""
    slot = timestamp.hour * INTERVALS_PER_HOUR + (
        timestamp.minute // INTERVAL_MINUTES
    )
    return _default_consumption_for_slot(timestamp.weekday(), slot)


def _default_consumption_for_slot(weekday: int, slot: int) -> float:
    """Return the seeded energy consumption for one weekday and slot."""
    interval_time = time(
        slot // INTERVALS_PER_HOUR,
        slot % INTERVALS_PER_HOUR * INTERVAL_MINUTES,
    )
    if weekday < 4 and time(7, 45) <= interval_time < time(18, 30):
        return ACTIVE_CONSUMPTION_KWH
    if weekday == 4 and time(7, 45) <= interval_time < time(13, 30):
        return ACTIVE_CONSUMPTION_KWH
    return IDLE_CONSUMPTION_KWH


def calculate_price_thresholds(
    inputs: OptimizerInputs,
    charge_price_threshold_per_kwh: float,
) -> PriceThresholds:
    """Return hard-guarded thresholds from the daily EPEX price range."""
    minimum_price_per_kwh = min(inputs.prices_per_kwh)
    price_range_per_kwh = max(inputs.prices_per_kwh) - minimum_price_per_kwh

    return PriceThresholds(
        charge_per_kwh=min(
            charge_price_threshold_per_kwh,
            MAX_CHARGE_PRICE_PER_KWH,
            minimum_price_per_kwh + CHARGE_PRICE_BAND * price_range_per_kwh,
        ),
        pre_charge_discharge_per_kwh=max(
            MIN_DISCHARGE_PRICE_PER_KWH,
            minimum_price_per_kwh
            + PRE_CHARGE_DISCHARGE_PRICE_BAND * price_range_per_kwh,
        ),
        post_charge_discharge_per_kwh=max(
            MIN_DISCHARGE_PRICE_PER_KWH,
            minimum_price_per_kwh
            + POST_CHARGE_DISCHARGE_PRICE_BAND * price_range_per_kwh,
        ),
    )


def create_greedy_charge_plan(
    inputs: OptimizerInputs,
    charge_price_threshold_per_kwh: float,
) -> OptimizationPlan:
    """Create a capacity-aware charge and discharge plan.

    The horizon is split into contiguous blocks of sub-threshold prices and the
    discharge gaps around them, then walked in chronological order so a day with
    both a cheap night and a cheap midday runs two charge and discharge cycles.
    Every gap is planned against the block that follows it: when that block can
    refill the battery on its own, the gap may empty the battery down to its
    minimum, otherwise stored energy is held back for higher prices.
    """
    _validate_charge_inputs(inputs, charge_price_threshold_per_kwh)
    thresholds = calculate_price_thresholds(
        inputs, charge_price_threshold_per_kwh
    )

    battery = inputs.battery
    minimum_energy_kwh = battery.usable_capacity_kwh * battery.min_soc_percent / 100
    target_energy_kwh = battery.usable_capacity_kwh * battery.max_soc_percent / 100
    refill_needed_kwh = target_energy_kwh - minimum_energy_kwh
    stored_energy_kwh = battery.usable_capacity_kwh * inputs.current_soc_percent / 100
    charge_energy_by_index = [0.0] * len(inputs.timestamps)
    discharge_energy_by_index = [0.0] * len(inputs.timestamps)

    blocks = _charge_blocks(inputs, thresholds.charge_per_kwh)
    for position, gap in enumerate(_discharge_gaps(blocks, len(inputs.timestamps))):
        stored_energy_kwh = minimum_energy_kwh + _allocate_discharge(
            inputs,
            discharge_energy_by_index,
            available_energy_kwh=stored_energy_kwh - minimum_energy_kwh,
            candidate_indices=gap,
            minimum_price_per_kwh=_discharge_floor_per_kwh(
                inputs, thresholds, blocks, position, refill_needed_kwh
            ),
        )
        if position >= len(blocks):
            continue

        block = blocks[position]
        _allocate_block_charge(
            inputs,
            charge_energy_by_index,
            block,
            stored_energy_kwh=stored_energy_kwh,
            target_energy_kwh=target_energy_kwh,
        )
        for index in block:
            stored_energy_kwh += (
                _absorbed_charge_kwh(
                    battery,
                    charge_energy_by_index[index],
                    stored_energy_kwh,
                    target_energy_kwh,
                )
                * battery.charge_efficiency
            )

    intervals, absorbed_charge_by_index = _build_intervals(
        inputs, charge_energy_by_index, discharge_energy_by_index, target_energy_kwh
    )
    return OptimizationPlan(
        intervals=intervals,
        summary=_build_summary(
            inputs,
            intervals,
            absorbed_charge_by_index,
            discharge_energy_by_index,
            full_charge_feasible=any(
                _refill_capability_kwh(inputs, block) >= refill_needed_kwh
                for block in blocks
            ),
        ),
    )


def _charge_blocks(
    inputs: OptimizerInputs, charge_price_per_kwh: float
) -> list[tuple[int, ...]]:
    """Return the contiguous runs of intervals priced below the charge threshold."""
    blocks: list[tuple[int, ...]] = []
    current: list[int] = []
    for index, price in enumerate(inputs.prices_per_kwh):
        if price < charge_price_per_kwh:
            current.append(index)
        elif current:
            blocks.append(tuple(current))
            current = []
    if current:
        blocks.append(tuple(current))
    return blocks


def _discharge_gaps(
    blocks: Sequence[tuple[int, ...]], interval_count: int
) -> list[range]:
    """Return the discharge windows around the charge blocks.

    The result always holds one more entry than ``blocks``: entry ``k`` is the
    window immediately preceding block ``k``, and the final entry is the tail of
    the horizon after the last block. Entries may be empty.
    """
    gaps: list[range] = []
    cursor = 0
    for block in blocks:
        gaps.append(range(cursor, block[0]))
        cursor = block[-1] + 1
    gaps.append(range(cursor, interval_count))
    return gaps


def _refill_capability_kwh(
    inputs: OptimizerInputs, block: Sequence[int]
) -> float:
    """Return the energy one charge block can store when run at full power."""
    battery = inputs.battery
    return (
        len(block)
        * battery.max_charge_power_kw
        * INTERVAL_HOURS
        * battery.charge_efficiency
    )


def _discharge_floor_per_kwh(
    inputs: OptimizerInputs,
    thresholds: PriceThresholds,
    blocks: Sequence[tuple[int, ...]],
    position: int,
    refill_needed_kwh: float,
) -> float:
    """Return the lowest price at which one gap may release stored energy."""
    # Nothing is bought cheaply today, so the stored energy was never bought
    # cheaply either and is released on the looser pre-charge threshold.
    if not blocks:
        return thresholds.pre_charge_discharge_per_kwh
    # No block follows the final gap, so what is left has to last the horizon.
    if position >= len(blocks):
        return thresholds.post_charge_discharge_per_kwh

    next_block = blocks[position]
    if _refill_capability_kwh(inputs, next_block) >= refill_needed_kwh:
        return _break_even_price_per_kwh(inputs, next_block, refill_needed_kwh)
    return thresholds.pre_charge_discharge_per_kwh


def _break_even_price_per_kwh(
    inputs: OptimizerInputs,
    block: Sequence[int],
    refill_needed_kwh: float,
) -> float:
    """Return the price at which discharging beats refilling from one block.

    A kWh delivered to the load costs one round trip of the refill price plus
    the degradation of the cycle, so releasing energy below this price loses
    money even though the battery is guaranteed to be refilled.
    """
    battery = inputs.battery
    max_charge_energy_kwh = battery.max_charge_power_kw * INTERVAL_HOURS
    stored_kwh = 0.0
    grid_kwh = 0.0
    refill_cost = 0.0
    for price in sorted(inputs.prices_per_kwh[index] for index in block):
        if stored_kwh >= refill_needed_kwh:
            break
        charge_energy_kwh = min(
            max_charge_energy_kwh,
            (refill_needed_kwh - stored_kwh) / battery.charge_efficiency,
        )
        stored_kwh += charge_energy_kwh * battery.charge_efficiency
        grid_kwh += charge_energy_kwh
        refill_cost += charge_energy_kwh * price

    refill_price_per_kwh = refill_cost / grid_kwh if grid_kwh > 0 else 0.0
    return (
        refill_price_per_kwh
        / (battery.charge_efficiency * battery.discharge_efficiency)
        + battery.degradation_cost_per_kwh
    )


def _price_buckets(
    inputs: OptimizerInputs, block: Sequence[int]
) -> list[tuple[int, ...]]:
    """Group one block's intervals into ascending fixed-width price buckets."""
    buckets: dict[int, list[int]] = {}
    for index in block:
        key = floor(inputs.prices_per_kwh[index] / CHARGE_PRICE_BUCKET_WIDTH)
        buckets.setdefault(key, []).append(index)
    return [tuple(buckets[key]) for key in sorted(buckets)]


def _allocate_block_charge(
    inputs: OptimizerInputs,
    charge_energy_by_index: list[float],
    block: Sequence[int],
    stored_energy_kwh: float,
    target_energy_kwh: float,
) -> None:
    """Fill one charge block, spreading power evenly inside each price bucket.

    Buckets are taken cheapest first. A bucket that can supply everything still
    needed carries an equal share in each of its intervals; a bucket that cannot
    runs at full power and the remainder descends to the next bucket up. The
    request is deliberately larger than the deficit so that a battery charging
    more slowly than commanded still reaches its target.
    """
    battery = inputs.battery
    max_charge_energy_kwh = battery.max_charge_power_kw * INTERVAL_HOURS
    deficit_kwh = (target_energy_kwh - stored_energy_kwh) / battery.charge_efficiency
    if deficit_kwh <= ENERGY_EPSILON_KWH:
        return

    margin_kwh = (
        CHARGE_MARGIN_FRACTION
        * battery.usable_capacity_kwh
        / battery.charge_efficiency
    )
    remaining_kwh = min(
        deficit_kwh + margin_kwh, len(block) * max_charge_energy_kwh
    )
    for bucket in _price_buckets(inputs, block):
        capability_kwh = len(bucket) * max_charge_energy_kwh
        if capability_kwh >= remaining_kwh:
            for index in bucket:
                charge_energy_by_index[index] = remaining_kwh / len(bucket)
            return
        for index in bucket:
            charge_energy_by_index[index] = max_charge_energy_kwh
        remaining_kwh -= capability_kwh


def _absorbed_charge_kwh(
    battery: BatteryParameters,
    commanded_charge_kwh: float,
    stored_energy_kwh: float,
    target_energy_kwh: float,
) -> float:
    """Return how much of one interval's commanded charge the battery can take.

    Commanded charge carries the slow-charging margin, so the battery reaches
    its target before the block ends and refuses the rest. Expected state of
    charge and every cost figure follow this absorbed energy, not the command.
    """
    room_kwh = max(0.0, target_energy_kwh - stored_energy_kwh)
    return min(commanded_charge_kwh, room_kwh / battery.charge_efficiency)


def _build_intervals(
    inputs: OptimizerInputs,
    charge_energy_by_index: Sequence[float],
    discharge_energy_by_index: Sequence[float],
    target_energy_kwh: float,
) -> tuple[list[PlanInterval], list[float]]:
    """Render the allocations as plan intervals and the energy actually stored."""
    battery = inputs.battery
    intervals: list[PlanInterval] = []
    absorbed_charge_by_index: list[float] = []
    expected_energy_kwh = battery.usable_capacity_kwh * inputs.current_soc_percent / 100

    for index, timestamp in enumerate(inputs.timestamps):
        commanded_charge_kwh = charge_energy_by_index[index]
        discharge_energy_kwh = discharge_energy_by_index[index]
        absorbed_charge_kwh = _absorbed_charge_kwh(
            battery, commanded_charge_kwh, expected_energy_kwh, target_energy_kwh
        )
        absorbed_charge_by_index.append(absorbed_charge_kwh)
        expected_energy_kwh += absorbed_charge_kwh * battery.charge_efficiency
        expected_energy_kwh -= discharge_energy_kwh / battery.discharge_efficiency
        intervals.append(
            PlanInterval(
                timestamp=timestamp,
                price_per_kwh=inputs.prices_per_kwh[index],
                consumption_kwh=inputs.consumption_kwh[index],
                target_battery_power_kw=(
                    -commanded_charge_kwh / INTERVAL_HOURS
                    if commanded_charge_kwh > 0
                    else discharge_energy_kwh / INTERVAL_HOURS
                ),
                expected_soc_percent=(
                    expected_energy_kwh / battery.usable_capacity_kwh * 100
                ),
                state=(
                    OperatingState.CHARGE
                    if commanded_charge_kwh > 0
                    else OperatingState.DISCHARGE
                    if discharge_energy_kwh > 0
                    else OperatingState.IDLE
                ),
            )
        )

    return intervals, absorbed_charge_by_index


def _build_summary(
    inputs: OptimizerInputs,
    intervals: Sequence[PlanInterval],
    absorbed_charge_by_index: Sequence[float],
    discharge_energy_by_index: Sequence[float],
    full_charge_feasible: bool,
) -> PlanSummary:
    """Aggregate the plan using the energy the battery is expected to absorb."""
    charge_prices = [
        interval.price_per_kwh
        for interval in intervals
        if interval.state is OperatingState.CHARGE
    ]
    discharge_prices = [
        interval.price_per_kwh
        for interval in intervals
        if interval.state is OperatingState.DISCHARGE
    ]
    total_discharge_kwh = sum(discharge_energy_by_index)
    cost_without_battery = sum(
        price * consumption
        for price, consumption in zip(
            inputs.prices_per_kwh, inputs.consumption_kwh, strict=True
        )
    )
    charge_cost = sum(
        price * charge_energy
        for price, charge_energy in zip(
            inputs.prices_per_kwh, absorbed_charge_by_index, strict=True
        )
    )
    avoided_grid_cost = sum(
        price * discharge_energy
        for price, discharge_energy in zip(
            inputs.prices_per_kwh, discharge_energy_by_index, strict=True
        )
    )
    degradation_cost = total_discharge_kwh * inputs.battery.degradation_cost_per_kwh
    cost_with_battery = (
        cost_without_battery + charge_cost - avoided_grid_cost + degradation_cost
    )

    return PlanSummary(
        total_charge_kwh=sum(absorbed_charge_by_index),
        total_discharge_kwh=total_discharge_kwh,
        highest_charge_price_per_kwh=max(charge_prices) if charge_prices else None,
        lowest_discharge_price_per_kwh=(
            min(discharge_prices) if discharge_prices else None
        ),
        cost_without_battery=cost_without_battery,
        cost_with_battery=cost_with_battery,
        daily_saving=cost_without_battery - cost_with_battery,
        full_charge_feasible=full_charge_feasible,
    )


def _allocate_discharge(
    inputs: OptimizerInputs,
    discharge_energy_by_index: list[float],
    available_energy_kwh: float,
    candidate_indices: Sequence[int],
    minimum_price_per_kwh: float,
) -> float:
    """Allocate stored energy to the highest-priced eligible demand intervals."""
    max_discharge_kwh = inputs.battery.max_discharge_power_kw * INTERVAL_HOURS
    eligible_indices = sorted(
        (
            index
            for index in candidate_indices
            if inputs.prices_per_kwh[index] >= minimum_price_per_kwh
            and inputs.consumption_kwh[index] > 0
        ),
        key=lambda index: (-inputs.prices_per_kwh[index], inputs.timestamps[index]),
    )

    remaining_energy_kwh = available_energy_kwh
    for index in eligible_indices:
        if remaining_energy_kwh <= ENERGY_EPSILON_KWH:
            break
        load_energy_kwh = min(
            inputs.consumption_kwh[index],
            max_discharge_kwh,
            remaining_energy_kwh * inputs.battery.discharge_efficiency,
        )
        if load_energy_kwh <= ENERGY_EPSILON_KWH:
            continue
        discharge_energy_by_index[index] = load_energy_kwh
        remaining_energy_kwh -= load_energy_kwh / inputs.battery.discharge_efficiency

    return remaining_energy_kwh


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
    if (
        battery.usable_capacity_kwh <= 0
        or battery.max_charge_power_kw <= 0
        or battery.max_discharge_power_kw <= 0
    ):
        raise ValueError("Battery capacity and charge/discharge power must be positive")
    if not 0 < battery.charge_efficiency <= 1 or not 0 < battery.discharge_efficiency <= 1:
        raise ValueError("Charge and discharge efficiency must be between 0 and 1")
    if not 0 <= battery.min_soc_percent < battery.max_soc_percent <= 100:
        raise ValueError("Battery SOC limits must be ordered percentages between 0 and 100")
    if not 0 <= inputs.current_soc_percent <= 100:
        raise ValueError("Current SOC must be a percentage between 0 and 100")
    if charge_price_threshold_per_kwh < 0:
        raise ValueError("Charge price threshold must not be negative")
