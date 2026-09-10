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
    SPIKE_PREMIUM_PER_KWH,
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
    """Return a day whose cheap hours fall both at night and at midday.

    The evening peak stays under the spike premium over the ~0.045 charge
    basis, so these two curves exercise the charge and D1 logic on their own.
    """
    return _prices(
        (24, 0.05), (16, 0.15), (20, 0.04), (8, 0.20), (16, 0.30), (12, 0.15)
    )


def _short_midday_prices() -> list[float]:
    """Return the same day with a midday block too short to refill the battery."""
    return _prices(
        (24, 0.05), (16, 0.15), (4, 0.04), (24, 0.20), (16, 0.30), (12, 0.15)
    )


# Cheap night at 0.05 fills the battery, so the cost basis is 0.05 and the spike
# premium puts the trigger at 0.35. The 0.40 window is a spike that sits below
# the 0.435 post-charge floor; the 0.60 window clears both.
_SPIKE_BELOW_FLOOR = slice(70, 74)
_SPIKE_ABOVE_FLOOR = slice(74, 76)


def _spike_day_prices() -> list[float]:
    """Return a flat day broken by two evening spikes of different heights."""
    return _prices(
        (24, 0.05), (46, 0.20), (4, 0.40), (2, 0.60), (20, 0.20)
    )


def _inputs(
    prices: list[float],
    current_soc_percent: float = MIN_SOC_PERCENT,
    battery: BatteryParameters | None = None,
    stored_energy_cost_per_kwh: float | None = None,
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
        stored_energy_cost_per_kwh=stored_energy_cost_per_kwh,
    )


def _charge_power_kw(interval: PlanInterval) -> float:
    return round(-interval.target_battery_power_kw, 9)


def _discharge_kwh(interval: PlanInterval) -> float:
    return round(interval.target_battery_power_kw * INTERVAL_HOURS, 9)


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
            assert _discharge_kwh(interval) <= interval.consumption_kwh + 1e-9

    assert plan.summary.spike_discharge_kwh == 0
    assert plan.summary.daily_saving == pytest.approx(
        plan.summary.cost_without_battery - plan.summary.cost_with_battery
    )


def test_price_spike_discharges_above_the_forecast_load() -> None:
    """A price far above the cost basis runs the battery at full power."""
    plan = create_greedy_charge_plan(
        _inputs(_spike_day_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    spike = plan.intervals[_SPIKE_ABOVE_FLOOR]

    assert all(interval.state is OperatingState.DISCHARGE for interval in spike)
    assert all(
        _discharge_kwh(interval) == pytest.approx(MAX_POWER_KW * INTERVAL_HOURS)
        for interval in spike
    )
    assert MAX_POWER_KW * INTERVAL_HOURS > CONSUMPTION_KWH


def test_price_spike_below_the_discharge_floor_is_still_used() -> None:
    """The spike premium qualifies an interval on its own, ignoring the floor."""
    inputs = _inputs(_spike_day_prices())
    thresholds = calculate_price_thresholds(inputs, CHARGE_THRESHOLD_PER_KWH)
    plan = create_greedy_charge_plan(inputs, CHARGE_THRESHOLD_PER_KWH)
    spike = plan.intervals[_SPIKE_BELOW_FLOOR]

    # Without the spike rule the price floor alone would reject these intervals.
    assert all(
        interval.price_per_kwh < thresholds.post_charge_discharge_per_kwh
        for interval in spike
    )
    assert all(
        _discharge_kwh(interval) == pytest.approx(MAX_POWER_KW * INTERVAL_HOURS)
        for interval in spike
    )


def test_prices_below_the_spike_premium_stay_capped_by_the_forecast() -> None:
    """An ordinary expensive interval is still limited to the forecast load."""
    plan = create_greedy_charge_plan(
        _inputs(_spike_day_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    discharging = [
        interval
        for interval in plan.intervals
        if interval.state is OperatingState.DISCHARGE
        and interval.price_per_kwh < 0.35
    ]

    assert all(
        _discharge_kwh(interval) <= CONSUMPTION_KWH + 1e-9 for interval in discharging
    )


def test_spike_premium_is_measured_against_the_planned_charge_price() -> None:
    """The trigger follows what the plan paid, not the cheapest price of the day."""
    # A short 0.01 dip drags the day minimum well below the 0.049 the block
    # actually pays on average, and 0.33 falls between the two triggers.
    prices = _prices((4, 0.01), (20, 0.08), (56, 0.20), (16, 0.33))
    plan = create_greedy_charge_plan(
        _inputs(prices, stored_energy_cost_per_kwh=0.01), CHARGE_THRESHOLD_PER_KWH
    )
    evening = plan.intervals[80:96]

    assert 0.01 + SPIKE_PREMIUM_PER_KWH < 0.33 < 0.049 + SPIKE_PREMIUM_PER_KWH
    assert any(interval.state is OperatingState.DISCHARGE for interval in evening)
    assert all(
        _discharge_kwh(interval) <= CONSUMPTION_KWH + 1e-9 for interval in evening
    )
    assert plan.summary.spike_discharge_kwh == 0


def test_measured_cost_basis_drives_the_gap_before_the_first_block() -> None:
    """Nothing is planned yet before the first block, so the account value rules."""
    prices = _prices((8, 0.40), (24, 0.05), (64, 0.20))

    def morning(stored_energy_cost_per_kwh: float | None) -> list[PlanInterval]:
        plan = create_greedy_charge_plan(
            _inputs(
                prices,
                current_soc_percent=MAX_SOC_PERCENT,
                stored_energy_cost_per_kwh=stored_energy_cost_per_kwh,
            ),
            CHARGE_THRESHOLD_PER_KWH,
        )
        return plan.intervals[0:8]

    cheap_basis = morning(0.05)
    dear_basis = morning(0.15)

    # 0.40 clears 0.05 + 0.30 but not 0.15 + 0.30.
    assert all(
        _discharge_kwh(interval) == pytest.approx(MAX_POWER_KW * INTERVAL_HOURS)
        for interval in cheap_basis
    )
    assert all(
        _discharge_kwh(interval) <= CONSUMPTION_KWH + 1e-9 for interval in dear_basis
    )
    # Omitting the measured basis falls back to the cheapest price of the day.
    assert [_discharge_kwh(interval) for interval in morning(None)] == [
        _discharge_kwh(interval) for interval in cheap_basis
    ]


def test_summary_reports_the_discharge_booked_above_the_forecast() -> None:
    """Energy that only flows if the house really draws it is reported apart."""
    plan = create_greedy_charge_plan(
        _inputs(_spike_day_prices()), CHARGE_THRESHOLD_PER_KWH
    )
    spike_intervals = plan.intervals[_SPIKE_BELOW_FLOOR] + plan.intervals[
        _SPIKE_ABOVE_FLOOR
    ]

    assert plan.summary.spike_discharge_kwh == pytest.approx(
        sum(
            _discharge_kwh(interval) - interval.consumption_kwh
            for interval in spike_intervals
        )
    )
    assert plan.summary.spike_discharge_kwh > 0
    assert plan.summary.daily_saving > 0
