"""Regression tests for DynEnergy optimizer output conversions."""

from datetime import UTC, datetime

from custom_components.dynenergy.optimizer import (
    OperatingState,
    PlanInterval,
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
