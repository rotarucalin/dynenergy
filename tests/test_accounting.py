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


def test_drifted_version_two_state_is_discarded() -> None:
    """A version-2 payload carries drifted stored energy and cannot be migrated."""
    account = BatteryCostAccount.from_dict(
        {
            "accounting_version": 2,
            "stored_energy_kwh": 7.46,
            "stored_energy_cost_eur": 0.72,
            "initialized": True,
        }
    )

    assert account == BatteryCostAccount()


def test_version_three_ledger_resets_once_for_quarter_hour_accounting() -> None:
    """Old totals reset; fresh readings and version-4 totals survive reloads."""
    account = BatteryCostAccount.from_dict(
        {
            "accounting_version": 3,
            "stored_energy_kwh": 2.0,
            "stored_energy_cost_eur": 0.20,
            "total_charged_kwh": 100.0,
            "total_charging_cost_eur": 10.0,
            "total_saved_cost_eur": 20.0,
            "previous_charged_energy_kwh": 100.0,
            "previous_discharged_energy_kwh": 80.0,
            "initialized": True,
        }
    )
    assert account == BatteryCostAccount()

    account = account.initialize(110.0, 85.0, 3.0, 0.10)
    assert account.total_charged_kwh == 0.0
    assert account.total_charging_cost_eur == 0.0
    assert account.total_saved_cost_eur == 0.0
    assert isclose(account.stored_energy_cost_eur, 0.30)

    account = account.record(110.2, 85.1, 0.25)
    assert isclose(account.total_charging_cost_eur, 0.05)
    assert isclose(account.total_saved_cost_eur, 0.025)
    assert account.as_dict()["accounting_version"] == 4
    assert BatteryCostAccount.from_dict(account.as_dict()) == account


def test_stored_energy_follows_the_measured_battery() -> None:
    """Meter deltas never decide how much energy the battery is holding."""
    account = BatteryCostAccount().initialize(0.0, 0.0, 1.0, 0.10)

    account = account.record(2.0, 0.0, 0.10, measured_energy_kwh=1.8)

    assert isclose(account.stored_energy_kwh, 1.8)


def test_measured_energy_stops_the_round_trip_drift() -> None:
    """Thirty cycles of lossy meter deltas leave the ledger on the real capacity."""
    capacity_kwh = 2.0
    account = BatteryCostAccount().initialize(0.0, 0.0, 1.3, 0.07)
    charged = discharged = 0.0

    for _ in range(30):
        charged += capacity_kwh / 0.95
        discharged += capacity_kwh * 0.95
        account = account.record(
            charged, discharged, 0.07, measured_energy_kwh=capacity_kwh
        )

    # Without re-anchoring this reaches 7.46 kWh on a 2 kWh battery.
    assert isclose(account.stored_energy_kwh, capacity_kwh)
    assert isclose(account.stored_energy_cost_per_kwh, 0.07, abs_tol=1e-9)


def test_reanchoring_keeps_the_unit_cost_of_stored_energy() -> None:
    """Correcting the amount of energy must not silently reprice it."""
    account = BatteryCostAccount().initialize(0.0, 0.0, 0.0)
    account = account.record(4.0, 0.0, 0.25)
    assert isclose(account.stored_energy_cost_per_kwh, 0.25)

    account = account.record(4.0, 0.0, 0.25, measured_energy_kwh=1.0)

    assert isclose(account.stored_energy_kwh, 1.0)
    assert isclose(account.stored_energy_cost_eur, 0.25)
    assert isclose(account.stored_energy_cost_per_kwh, 0.25)


def test_opening_energy_is_priced_rather_than_free() -> None:
    """A zero basis would understate every average until that energy is gone."""
    account = BatteryCostAccount().initialize(0.0, 0.0, 2.0, 0.08)

    assert isclose(account.stored_energy_cost_eur, 0.16)
    assert isclose(account.stored_energy_cost_per_kwh, 0.08)


def test_average_charge_price_tracks_every_purchase() -> None:
    """The lifetime average price paid per charged kWh is now derivable."""
    account = BatteryCostAccount().initialize(0.0, 0.0, 0.0)
    account = account.record(1.0, 0.0, 0.10)
    account = account.record(4.0, 0.0, 0.20)

    assert isclose(account.total_charged_kwh, 4.0)
    # 1 kWh at 0.10 plus 3 kWh at 0.20.
    assert isclose(account.total_charging_cost_eur, 0.70)
    assert isclose(account.average_charge_price_per_kwh, 0.175)


def test_average_charge_price_is_zero_before_any_charging() -> None:
    """An untouched account reports no average rather than dividing by zero."""
    assert BatteryCostAccount().average_charge_price_per_kwh == 0.0


def test_real_meter_account_round_trips_through_storage() -> None:
    """Current-version totals and meter baselines remain persistent."""
    account = BatteryCostAccount().initialize(10.0, 5.0, 2.0)
    account = account.record(10.2, 5.1, 0.10)

    assert BatteryCostAccount.from_dict(account.as_dict()) == account
