"""Net discharge margin regressions, including the reported six-cent cycle."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.dynenergy import optimizer as opt
from custom_components.dynenergy.accounting import BatteryCostAccount

START = datetime(2026, 9, 14, 23, 45, tzinfo=UTC)
BATTERY = opt.BatteryParameters(1, 4, 4, 0, 100, 1, 1)


def plan(prices, *, battery=BATTERY, soc=100, basis=.10, loads=None, cap=.11):
    return opt.create_greedy_charge_plan(opt.OptimizerInputs(
        [START+timedelta(minutes=15*n) for n in range(len(prices))], prices,
        loads if loads is not None else [1]*len(prices), soc, battery, basis,
    ), cap)


@pytest.mark.parametrize('price,discharge', [(.16, False), (.249999, False), (.25, True), (.30, True)])
def test_fifteen_cents_net_is_required_and_equality_qualifies(price, discharge):
    result = plan([price])
    slot = result.intervals[0]
    assert (slot.state is opt.OperatingState.DISCHARGE) == discharge
    assert slot.minimum_discharge_price_per_kwh == pytest.approx(.25)
    if discharge:
        assert slot.expected_discharge_profit_per_kwh == pytest.approx(price-.10)


@pytest.mark.parametrize('delta,discharge', [(-.00001, False), (0, True), (.01, True)])
def test_charged_energy_includes_both_losses_and_degradation(delta, discharge):
    battery = replace(BATTERY, charge_efficiency=.8, discharge_efficiency=.9, degradation_cost_per_kwh=.02)
    threshold = .10/(.8*.9) + .02 + .15
    result = plan([.10, threshold+delta], battery=battery, soc=0, basis=0)
    later = result.intervals[1]
    assert (later.state is opt.OperatingState.DISCHARGE) == discharge
    assert later.minimum_discharge_price_per_kwh == pytest.approx(threshold)
    if discharge:
        assert later.expected_discharge_profit_per_kwh >= .15-1e-9
    assert result.summary.total_discharge_kwh == pytest.approx(.72 if discharge else 0)


@pytest.mark.parametrize('charge_efficiency', [.5, .8, 1])
def test_measured_battery_side_cost_is_not_charged_for_losses_twice(charge_efficiency):
    battery = replace(BATTERY, charge_efficiency=charge_efficiency, discharge_efficiency=.9)
    threshold = .125/.9 + .15
    assert plan([threshold], battery=battery, basis=.125).summary.total_discharge_kwh > 0
    assert plan([threshold-.0001], battery=battery, basis=.125).summary.total_discharge_kwh == 0


def test_spike_cannot_bypass_minimum_net_profit():
    battery = replace(BATTERY, discharge_efficiency=.2)
    # Legacy spike threshold is .10 + .30 = .40, but .41 is below the .65 net floor.
    assert plan([.41], battery=battery, loads=[0]).summary.total_discharge_kwh == 0
    assert plan([.65], battery=battery, loads=[0]).summary.total_discharge_kwh == pytest.approx(.2)


@pytest.mark.parametrize('early,expected', [(.14, False), (.159999, False), (.16, True)])
def test_guaranteed_refill_also_requires_fifteen_cents_profit(early, expected):
    result = plan([early, .01, .40], basis=0, cap=.10)
    assert (result.intervals[0].state is opt.OperatingState.DISCHARGE) == expected


def test_spike_cannot_bypass_replacement_cycle_profit():
    # Very cheap stored energy makes .16 a spike, but replacing at .10 earns 6 ct.
    result = plan([.16, .10, .50], basis=-.20)
    assert result.intervals[0].target_battery_energy_kwh == 0


def test_reported_six_cent_charge_discharge_cycle_is_not_scheduled():
    # Purchases before midnight at 10 ct cannot trigger discharge at 16 ct tomorrow.
    result = plan([.10, .16, .09, .09, .17], soc=0, basis=0)
    assert result.summary.total_discharge_kwh == 0
    assert all(i.expected_discharge_profit_per_kwh is None for i in result.intervals)
    # Within a horizon greedy's existing lookahead can also defer the initial
    # purchase until the cheaper refill when no intervening demand earns enough.
    assert result.intervals[0].state is opt.OperatingState.IDLE
    assert result.intervals[2].state is opt.OperatingState.CHARGE


def test_projected_cost_carries_tonights_purchase_across_midnight():
    battery = replace(BATTERY, charge_efficiency=.8, discharge_efficiency=.9, degradation_cost_per_kwh=.02)
    tonight = plan([.10, .40], battery=battery, soc=0, basis=0)
    cost = opt.projected_stored_energy_cost_per_kwh(tonight.intervals[:1], battery, 0, 0)
    assert cost == pytest.approx(.125)
    tomorrow = plan([.16], battery=battery, soc=80, basis=cost)
    assert tomorrow.summary.total_discharge_kwh == 0
    assert tomorrow.intervals[0].minimum_discharge_price_per_kwh == pytest.approx(.125/.9+.02+.15)


def test_forecast_cost_is_weighted_by_remaining_energy_not_lifetime_purchases():
    battery = replace(BATTERY, usable_capacity_kwh=2)
    def interval(n, energy, price):
        return opt.PlanInterval(START+timedelta(minutes=15*n), price, 1, energy, 0,
                                opt.OperatingState.CHARGE if energy < 0 else opt.OperatingState.DISCHARGE,
                                allocated_grid_energy_kwh=energy)
    schedule = [interval(0, 1, .50), interval(1, -1, .10), interval(2, 1, .50), interval(3, -1, .30)]
    # 2 kWh @ .20 -> 1 kWh @ .20 -> 2 kWh @ .15 -> 1 kWh @ .15 -> 2 kWh @ .225.
    assert opt.projected_stored_energy_cost_per_kwh(schedule, battery, 100, .20) == pytest.approx(.225)


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf')])
def test_invalid_minimum_profit_is_rejected(value):
    with pytest.raises(ValueError):
        plan([.50], battery=replace(BATTERY, minimum_discharge_profit_per_kwh=value))


def test_measured_accounting_is_unchanged_by_profit_forecasts():
    account = BatteryCostAccount().initialize(0, 0, 1, .10)
    before = account.as_dict()
    result = plan([.10, .16], basis=account.stored_energy_cost_per_kwh)
    opt.projected_stored_energy_cost_per_kwh(result.intervals, BATTERY, 100, account.stored_energy_cost_per_kwh)
    assert account.as_dict() == before


@pytest.mark.parametrize('prices', [
    [.04]*8 + [.10] + [.039]*8 + [.19],
    [.08]*4 + [.17]*4 + [.04]*4 + [.35]*4,
    [.10]*4 + [.16]*4 + [.09]*4 + [.17]*4,
    [.05]*4 + [.12]*4 + [.051]*4 + [.28]*4,
])
def test_all_pooling_and_lookahead_paths_respect_profit_gate(prices):
    battery = replace(BATTERY, usable_capacity_kwh=2, charge_efficiency=.9, discharge_efficiency=.85)
    result = plan(prices, battery=battery, soc=50, basis=.1, loads=[.2]*len(prices))
    for i in result.intervals:
        if i.state is opt.OperatingState.DISCHARGE:
            assert i.expected_discharge_profit_per_kwh >= .15-1e-9

@pytest.mark.parametrize('seed', range(12))
def test_generated_greedy_horizons_never_release_below_net_profit(seed):
    import random
    rng = random.Random(seed)
    prices = [rng.choice([-.05, .02, .08, .10, .16, .22, .35, .50]) for _ in range(64)]
    battery = replace(BATTERY, usable_capacity_kwh=2, min_soc_percent=10,
                      charge_efficiency=.8, discharge_efficiency=.9, degradation_cost_per_kwh=.01)
    result = plan(prices, battery=battery, soc=60, basis=.10, loads=[.2]*64, cap=.20)
    for slot in result.intervals:
        if slot.state is opt.OperatingState.DISCHARGE:
            assert slot.expected_discharge_profit_per_kwh >= .15-1e-9
            assert slot.price_per_kwh >= slot.minimum_discharge_price_per_kwh-1e-9
        assert 10-1e-9 <= slot.expected_soc_percent <= 100+1e-9

@pytest.mark.parametrize('basis', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_stored_cost_is_rejected(basis):
    with pytest.raises(ValueError, match='Stored-energy cost'):
        plan([.50], basis=basis)
