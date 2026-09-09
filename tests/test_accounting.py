"""Regression tests for real-energy cost accounting."""

from math import isclose

from custom_components.dynenergy.accounting import BatteryCostAccount


def test_real_meter_deltas_update_costs_and_savings() -> None:
    """Actual charged and discharged energy is valued at the current price."""
    account = BatteryCostAccount().initialize(
        charged_energy_kwh=10.0,
        discharged_energy_kwh=5.0,
        opening_energy_kwh=2.0,
    )

    account = account.record(
        charged_energy_kwh=10.2,
        discharged_energy_kwh=5.1,
        price_per_kwh=0.10,
    )

    assert isclose(account.total_charging_cost_eur, 0.02)
    assert isclose(account.total_saved_cost_eur, 0.01)
    assert isclose(account.stored_energy_kwh, 2.1)


def test_all_real_generation_counts_as_savings() -> None:
    """Savings are not capped by the cost-accounting battery balance."""
    account = BatteryCostAccount().initialize(10.0, 5.0, 0.0)

    account = account.record(10.0, 6.0, 0.20)

    assert isclose(account.total_saved_cost_eur, 0.20)


def test_old_estimated_totals_are_not_restored() -> None:
    """Pre-real-meter persisted totals reset because they cannot be corrected."""
    account = BatteryCostAccount.from_dict(
        {
            "total_charging_cost_eur": 100.0,
            "total_saved_cost_eur": 200.0,
            "initialized": True,
        }
    )

    assert account == BatteryCostAccount()


def test_real_meter_account_round_trips_through_storage() -> None:
    """Current-version totals and meter baselines remain persistent."""
    account = BatteryCostAccount().initialize(10.0, 5.0, 2.0)
    account = account.record(10.2, 5.1, 0.10)

    assert BatteryCostAccount.from_dict(account.as_dict()) == account
