"""Regression tests for DynEnergy optimizer output conversions."""

from datetime import UTC, datetime

from custom_components.dynenergy.optimizer import (
    ACTIVE_CONSUMPTION_KWH,
    IDLE_CONSUMPTION_KWH,
    INTERVALS_PER_WEEK,
    OperatingState,
    PlanInterval,
    WeeklyConsumptionProfile,
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
