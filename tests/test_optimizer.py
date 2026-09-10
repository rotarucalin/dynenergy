"""Regression tests for DynEnergy optimizer output conversions."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.dynenergy.optimizer import (
    ACTIVE_CONSUMPTION_KWH,
    CHARGE_MARGIN_FRACTION,
    IDLE_CONSUMPTION_KWH,
    INTERVAL_HOURS,
    INTERVALS_PER_DAY,
    INTERVALS_PER_WEEK,
    BatteryParameters,
    OperatingState,
    OptimizerInputs,
    PlanInterval,
    WeeklyConsumptionProfile,
    calculate_price_thresholds,
    create_greedy_charge_plan,
    default_consumption_kwh,
    interval_power_limit_kw,
    target_power_w,
)


def _interval(target_battery_power_kw: float) -> PlanInterval:
    state = (
        OperatingState.CHARGE
        if target_battery_power_kw < 0
        else OperatingState.DISCHARGE
        if target_battery_power_kw > 0
        else OperatingState.IDLE
    )
    return PlanInterval(
        timestamp=datetime(2026, 9, 9, tzinfo=UTC),
        price_per_kwh=0.05,
        consumption_kwh=0.2,
        target_battery_power_kw=target_battery_power_kw,
        expected_soc_percent=50.0,
        state=state,
    )


def test_hourly_limit_is_scaled_before_interval_output() -> None:
    """A 1.5 kW hourly limit remains 1500 W through interval scaling."""
    interval_limit_kw = interval_power_limit_kw(1.5)

    assert interval_limit_kw == 0.375
    assert target_power_w(_interval(interval_limit_kw)) == 1500


def test_target_power_preserves_charge_sign() -> None:
    """Signed charging recommendations remain signed after conversion."""
    assert target_power_w(_interval(-0.375)) == -1500


def test_missing_power_limit_remains_missing() -> None:
    """An unavailable configured limit is not converted into a numeric value."""
    assert interval_power_limit_kw(None) is None


def test_default_weekly_consumption_profile() -> None:
    """The initial profile uses the requested weekday working hours."""
    monday = datetime(2026, 9, 7, tzinfo=UTC)
    friday = datetime(2026, 9, 11, tzinfo=UTC)
    saturday = datetime(2026, 9, 12, tzinfo=UTC)

    assert (
        default_consumption_kwh(monday.replace(hour=7, minute=30))
        == IDLE_CONSUMPTION_KWH
    )
    assert (
        default_consumption_kwh(monday.replace(hour=7, minute=45))
        == ACTIVE_CONSUMPTION_KWH
    )
    assert (
        default_consumption_kwh(monday.replace(hour=18, minute=15))
        == ACTIVE_CONSUMPTION_KWH
    )
    assert (
        default_consumption_kwh(monday.replace(hour=18, minute=30))
        == IDLE_CONSUMPTION_KWH
    )
    assert (
        default_consumption_kwh(friday.replace(hour=13, minute=15))
        == ACTIVE_CONSUMPTION_KWH
    )
    assert (
        default_consumption_kwh(friday.replace(hour=13, minute=30))
        == IDLE_CONSUMPTION_KWH
    )
    assert (
        default_consumption_kwh(saturday.replace(hour=10))
        == IDLE_CONSUMPTION_KWH
    )


def test_weekly_profile_learns_a_running_average() -> None:
    """Real interval samples replace defaults and then form a running average."""
    timestamp = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
    profile = WeeklyConsumptionProfile.default()

    assert len(profile.values_kwh) == INTERVALS_PER_WEEK
    assert profile.consumption_kwh(timestamp) == ACTIVE_CONSUMPTION_KWH

    profile = profile.record(timestamp, 0.5)
    assert profile.consumption_kwh(timestamp) == 0.5

    profile = profile.record(timestamp, 0.25)
    assert profile.consumption_kwh(timestamp) == 0.375
    assert WeeklyConsumptionProfile.from_dict(profile.as_dict()) == profile


CAPACITY_KWH = 10.0
MIN_SOC_PERCENT = 10.0
MAX_SOC_PERCENT = 100.0
MAX_POWER_KW = 4.0  # one full kWh per 15-minute interval
CHARGE_THRESHOLD_PER_KWH = 0.10
CONSUMPTION_KWH = 0.6
DAY_START = datetime(2026, 9, 14, tzinfo=UTC)

# 00:00 cheap night, 06:00 morning peak, 10:00 cheap midday, 15:00 shoulder,
# 17:00 evening peak, 21:00 shoulder. The morning peak sits deliberately between
# the break-even floor and the pre-charge threshold so the D1 gate is decisive.
_NIGHT = slice(0, 24)
_MORNING_PEAK = slice(24, 40)
_MIDDAY = slice(40, 60)
_EVENING_PEAK = slice(68, 84)


def _battery(**overrides: float) -> BatteryParameters:
    return BatteryParameters(
        **{
            "usable_capacity_kwh": CAPACITY_KWH,
            "max_charge_power_kw": MAX_POWER_KW,
            "max_discharge_power_kw": MAX_POWER_KW,
            "min_soc_percent": MIN_SOC_PERCENT,
            "max_soc_percent": MAX_SOC_PERCENT,
            "charge_efficiency": 1.0,
            "discharge_efficiency": 1.0,
            **overrides,
        }
    )


def _prices(*segments: tuple[int, float]) -> list[float]:
    """Expand (interval count, price) segments into one day of prices."""
    prices: list[float] = []
    for count, price in segments:
        prices.extend([price] * count)
    assert len(prices) == INTERVALS_PER_DAY
    return prices


def _solar_day_prices() -> list[float]:
    """Return a day whose cheap hours fall both at night and at midday."""
    return _prices(
        (24, 0.05), (16, 0.15), (20, 0.04), (8, 0.20), (16, 0.35), (12, 0.15)
    )


def _short_midday_prices() -> list[float]:
    """Return the same day with a midday block too short to refill the battery."""
    return _prices(
        (24, 0.05), (16, 0.15), (4, 0.04), (24, 0.20), (16, 0.35), (12, 0.15)
    )


def _inputs(
    prices: list[float],
    current_soc_percent: float = MIN_SOC_PERCENT,
    battery: BatteryParameters | None = None,
) -> OptimizerInputs:
    return OptimizerInputs(
        timestamps=[
            DAY_START + timedelta(hours=INTERVAL_HOURS * index)
            for index in range(len(prices))
        ],
        prices_per_kwh=prices,
        consumption_kwh=[CONSUMPTION_KWH] * len(prices),
        current_soc_percent=current_soc_percent,
        battery=battery or _battery(),
    )


def _charge_power_kw(interval: PlanInterval) -> float:
    return round(-interval.target_battery_power_kw, 9)


def test_solar_day_runs_two_charge_and_discharge_cycles() -> None:
    """Cheap night and cheap midday are both used, with a peak discharged after each."""
    plan = create_greedy_charge_plan(
        _inputs(_solar_day_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    states = [interval.state for interval in plan.intervals]

    assert OperatingState.CHARGE in states[_NIGHT]
    assert OperatingState.DISCHARGE in states[_MORNING_PEAK]
    assert OperatingState.CHARGE in states[_MIDDAY]
    assert OperatingState.DISCHARGE in states[_EVENING_PEAK]
    assert plan.summary.full_charge_feasible


def test_morning_empties_the_battery_when_midday_can_refill() -> None:
    """D1: a guaranteed midday refill lets the morning peak run down to min SOC."""
    plan = create_greedy_charge_plan(
        _inputs(_solar_day_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    morning = plan.intervals[_MORNING_PEAK]

    assert min(interval.expected_soc_percent for interval in morning) == pytest.approx(
        MIN_SOC_PERCENT
    )


def test_morning_holds_its_charge_when_midday_cannot_refill() -> None:
    """Without a guaranteed refill the morning peak is below the stricter floor."""
    plan = create_greedy_charge_plan(
        _inputs(_short_midday_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    morning = plan.intervals[_MORNING_PEAK]

    assert all(interval.state is not OperatingState.DISCHARGE for interval in morning)
    assert all(
        interval.expected_soc_percent == pytest.approx(MAX_SOC_PERCENT)
        for interval in morning
    )


def test_cheapest_bucket_spreads_power_evenly() -> None:
    """C1: one bucket that can finish the job charges every interval equally."""
    plan = create_greedy_charge_plan(
        _inputs(_solar_day_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    powers = {_charge_power_kw(interval) for interval in plan.intervals[_NIGHT]}

    assert len(powers) == 1
    assert 0 < powers.pop() < MAX_POWER_KW


def test_charge_descends_to_the_next_price_bucket() -> None:
    """C2: a bucket too small to finish runs at full power and the rest descends."""
    prices = _prices((4, 0.02), (12, 0.05), (80, 0.30))
    plan = create_greedy_charge_plan(_inputs(prices), CHARGE_THRESHOLD_PER_KWH)
    spread = {_charge_power_kw(interval) for interval in plan.intervals[4:16]}

    assert all(
        _charge_power_kw(interval) == MAX_POWER_KW for interval in plan.intervals[0:4]
    )
    assert len(spread) == 1
    assert 0 < spread.pop() < MAX_POWER_KW


def test_charge_plan_books_a_slow_charging_margin() -> None:
    """The commanded charge exceeds the deficit without overfilling the battery."""
    battery = _battery(charge_efficiency=0.9)
    plan = create_greedy_charge_plan(
        _inputs(_solar_day_prices(), battery=battery), CHARGE_THRESHOLD_PER_KWH
    )
    commanded_kwh = sum(
        -interval.target_battery_power_kw * INTERVAL_HOURS
        for interval in plan.intervals[_NIGHT]
    )
    minimum_energy_kwh = CAPACITY_KWH * MIN_SOC_PERCENT / 100

    assert commanded_kwh == pytest.approx(
        (CAPACITY_KWH - minimum_energy_kwh) / 0.9
        + CHARGE_MARGIN_FRACTION * CAPACITY_KWH / 0.9
    )
    # The margin is commanded but not absorbed: the night block still stops at
    # max SOC, and the day's total is exactly the two refills it really performs.
    assert plan.intervals[_NIGHT][-1].expected_soc_percent == pytest.approx(
        MAX_SOC_PERCENT
    )
    assert plan.summary.total_charge_kwh == pytest.approx(
        2 * (CAPACITY_KWH - minimum_energy_kwh) / 0.9
    )


def test_charging_never_exceeds_the_charge_threshold() -> None:
    """The charge threshold is a hard cap even when the battery stays unfilled."""
    inputs = _inputs(_solar_day_prices())
    thresholds = calculate_price_thresholds(inputs, CHARGE_THRESHOLD_PER_KWH)
    plan = create_greedy_charge_plan(inputs, CHARGE_THRESHOLD_PER_KWH)

    assert all(
        interval.price_per_kwh < thresholds.charge_per_kwh
        for interval in plan.intervals
        if interval.state is OperatingState.CHARGE
    )


def test_scattered_cheap_intervals_are_not_a_full_charge() -> None:
    """Feasibility needs one block to refill on its own, not the day's total."""
    prices = _prices(*((2, 0.02), (2, 0.30)) * 24)
    plan = create_greedy_charge_plan(_inputs(prices), CHARGE_THRESHOLD_PER_KWH)

    assert not plan.summary.full_charge_feasible


def test_plan_respects_soc_limits_and_forecast_load() -> None:
    """SOC stays inside its limits and discharge never exceeds the interval load."""
    plan = create_greedy_charge_plan(
        _inputs(_solar_day_prices(), current_soc_percent=55.0),
        CHARGE_THRESHOLD_PER_KWH,
    )

    for interval in plan.intervals:
        assert MIN_SOC_PERCENT <= interval.expected_soc_percent <= MAX_SOC_PERCENT
        if interval.state is OperatingState.DISCHARGE:
            assert (
                interval.target_battery_power_kw * INTERVAL_HOURS
                <= interval.consumption_kwh + 1e-9
            )

    assert plan.summary.daily_saving == pytest.approx(
        plan.summary.cost_without_battery - plan.summary.cost_with_battery
    )
    assert plan.summary.daily_saving > 0
