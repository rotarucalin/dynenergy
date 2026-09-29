"""Physical and economic regression tests for the shared cancellation ledger."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.dynenergy import optimizer as opt
from custom_components.dynenergy import cycle_cancellation as cycles

START = datetime(2026, 9, 14, 23, 45, tzinfo=UTC)
TOMORROW = (START + timedelta(days=1)).date()
BATTERY = opt.BatteryParameters(10, 20, 20, 0, 100, 1, 1)


def slots(energies, prices=None, loads=None, start=START):
    prices = prices or [.20, .15, .40]
    loads = loads if loads is not None else [max(0, e) for e in energies]
    return [opt.PlanInterval(start + timedelta(minutes=15*n), p, load, e, 0,
                             opt.OperatingState.CHARGE if e < 0 else
                             opt.OperatingState.DISCHARGE if e > 0 else opt.OperatingState.IDLE)
            for n, (e, p, load) in enumerate(zip(energies, prices, loads, strict=True))]


def compare(energies=(1, -1, 1), prices=None, loads=None, battery=BATTERY, soc=50, **kwargs):
    return cycles.cancel_cycles(slots(energies, prices, loads), battery, soc, TOMORROW,
                                debug=True, **kwargs)


@pytest.mark.parametrize('early,cancelled,benefit', [(.20, 1, .01), (.25, 0, 0), (.21, 1, 0)])
def test_policy_examples_and_equality(early, cancelled, benefit):
    result = compare(prices=[early, .15, .40])
    d = result.diagnostics
    assert d['cancelled_discharge_grid_kwh'] == pytest.approx(cancelled)
    assert d['estimated_policy_benefit_eur'] == pytest.approx(benefit)
    assert d['predicted_cash_flow_difference_eur'] == pytest.approx(-cancelled*(early-.15))
    assert result.adjusted.intervals[-1].allocated_grid_energy_kwh == 1
    assert d['baseline_terminal_soc_percent'] == pytest.approx(d['adjusted_terminal_soc_percent'])


@pytest.mark.parametrize('later_price', [-.5, .10, .40, 2])
@pytest.mark.parametrize('degradation', [0, .25])
def test_later_price_and_degradation_do_not_enter_policy(later_price, degradation):
    result = compare(prices=[.20, .15, later_price], battery=replace(BATTERY, degradation_cost_per_kwh=degradation))
    assert result.diagnostics['cancelled_discharge_grid_kwh'] == 1
    assert result.diagnostics['estimated_policy_benefit_eur'] == pytest.approx(.01)


def test_stored_cost_is_not_a_cancellation_input():
    # Greedy produces the same eligible quantities at each acquisition cost.
    from custom_components.dynenergy.accounting import BatteryCostAccount
    decisions = []
    battery = replace(BATTERY, usable_capacity_kwh=1, max_charge_power_kw=4, max_discharge_power_kw=4)
    timestamps = [i.timestamp for i in slots([1, -1, 1])]
    for price in [0, .05, .50]:
        account = BatteryCostAccount().initialize(0, 0, 1, price)
        before = account.as_dict()
        candidate = opt.create_greedy_charge_plan(opt.OptimizerInputs(
            timestamps, [.2, .15, .4], [1, 0, 1], 100, battery,
            stored_energy_cost_per_kwh=account.stored_energy_cost_per_kwh,
        ), .18)
        result = cycles.cancel_cycles(candidate.intervals, battery, 100, TOMORROW)
        assert result.diagnostics['cancelled_discharge_grid_kwh'] == 1
        decisions.append(result.diagnostics)
        assert account.as_dict() == before
    assert decisions[0] == decisions[1] == decisions[2]


def test_efficiencies_match_exact_battery_energy():
    result = compare((.8, -1.25, .8), battery=replace(BATTERY, charge_efficiency=.8, discharge_efficiency=.8))
    d = result.diagnostics
    assert d['cancelled_discharge_grid_kwh'] == pytest.approx(.8)
    assert d['cancelled_charge_grid_kwh'] == pytest.approx(1.25)
    assert d['reserved_D3_battery_kwh'] == pytest.approx(1)
    assert d['predicted_cash_flow_difference_eur'] == pytest.approx(1.25*.15-.8*.20)
    assert d['estimated_policy_benefit_eur'] == pytest.approx(.8*.01)
    assert d['adjusted_terminal_soc_percent'] == pytest.approx(d['baseline_terminal_soc_percent'])


def test_sorting_partial_quantities_ties_and_repeated_matches():
    items = slots([.5, .75, -.5, -.75, .4, .6], [.20, .19, .14, .15, .41, .40])
    result = cycles.cancel_cycles(items, BATTERY, 50, TOMORROW, debug=True)
    matches = [m for m in result.debug['matches'] if m['reason'] == 'accepted']
    assert matches[0]['early_timestamp'] == items[1].timestamp.isoformat()
    assert matches[0]['charge_timestamp'] == items[3].timestamp.isoformat()
    assert matches[0]['later_timestamp'] == items[5].timestamp.isoformat()
    assert [m['matched_battery_kwh'] for m in matches] == pytest.approx([.6, .15, .25])
    assert result.diagnostics['reserved_D3_battery_kwh'] == pytest.approx(1)
    assert result.adjusted.intervals[0].allocated_grid_energy_kwh == pytest.approx(.25)
    # Ties resolve by absolute time in all three lists.
    tied = slots([.5, .5, -.5, -.5, .5, .5], [.2, .2, .15, .15, .4, .4])
    result = cycles.cancel_cycles(tied, BATTERY, 50, TOMORROW, debug=True)
    first = result.debug['matches'][0]
    for key, n in [('early_timestamp', 0), ('charge_timestamp', 2), ('later_timestamp', 4)]:
        assert first[key] == tied[n].timestamp.isoformat()


def test_forecast_demand_limits_spike_reservation():
    result = compare((1, -1, 2), loads=[1, 0, .3])
    assert result.diagnostics['reserved_D3_battery_kwh'] == pytest.approx(.3)
    assert result.adjusted.intervals[2].target_battery_energy_kwh == 2
    assert result.adjusted.summary.spike_discharge_kwh == pytest.approx(1.7)


def test_charge_margin_is_removed_from_reduced_command():
    # Only 1 kWh is absorbable out of the 3 kWh command.
    result = compare((1, -3, 1), soc=100)
    assert result.baseline.summary.total_charge_kwh == pytest.approx(1)
    assert opt.target_power_w(result.adjusted.intervals[1]) == 0
    assert result.adjusted.summary.total_charge_kwh == 0
    assert result.adjusted.intervals[1].original_target_power_w == -12000


def test_partial_margin_reduction_preserves_absorption():
    result = compare((1, -3, .4), soc=100)
    assert result.adjusted.intervals[1].target_battery_energy_kwh == pytest.approx(-.6)
    assert result.adjusted.summary.total_charge_kwh == pytest.approx(.6)


@pytest.mark.parametrize('energy', [0, 1e-12])
def test_zero_and_float_residue_do_not_match(energy):
    result = compare((energy, -energy, energy))
    assert result.diagnostics['accepted_matches'] == 0


def test_negative_prices():
    result = compare(prices=[-.10, -.15, .4])
    assert result.diagnostics['cancelled_discharge_grid_kwh'] == 1
    assert result.diagnostics['estimated_policy_benefit_eur'] == pytest.approx(.01)


def test_soc_headroom_limits_q_and_searches_alternative_charge():
    # Expensive C at index 3 is infeasible because index 1 already fills capacity.
    # The cheaper earlier C is still a valid alternative.
    result = compare((1, -1, 0, -1, 1), [.20, .15, .1, .16, .4], soc=100)
    assert result.diagnostics['cancelled_discharge_grid_kwh'] == pytest.approx(1)
    accepted = [m for m in result.debug['matches'] if m['reason'] == 'accepted']
    assert accepted[0]['charge_timestamp'] == result.baseline.intervals[1].timestamp.isoformat()


def test_partial_headroom():
    # Extra today charge leaves only 0.25 kWh of headroom before tomorrow C.
    items = slots([1, -.75, -1, 1], [.2, .1, .15, .4], start=START-timedelta(minutes=15))
    result = cycles.cancel_cycles(items, BATTERY, 100, TOMORROW, debug=True)
    assert result.diagnostics['cancelled_discharge_grid_kwh'] == pytest.approx(.25)
    assert max(i.expected_soc_percent for i in result.adjusted.intervals) <= 100


def test_multiple_blocks_use_one_shared_ledger():
    result = compare((1, -1, 1, -1, 1), [.2, .15, .21, .15, .4])
    # Middle discharge is reserved by the first match, cannot also be removed.
    assert result.diagnostics['accepted_matches'] == 1
    assert result.adjusted.intervals[2].allocated_grid_energy_kwh == 1
    assert result.diagnostics['reserved_D3_battery_kwh'] == 1


def test_earlier_removal_limits_later_reservation_in_another_group():
    result = compare((1, -1, 1, -1, 1), [.2, .15, .18, .15, .4])
    # Lower-priced middle discharge is processed first and removed. It cannot
    # then justify another reservation by the first discharge.
    assert result.adjusted.intervals[2].allocated_grid_energy_kwh == 0
    assert result.diagnostics['cancelled_discharge_grid_kwh'] == 1


def test_no_later_energy_and_no_chronological_match():
    result = compare((1, 1, -1), [.2, .4, .15])
    assert result.diagnostics['accepted_matches'] == 0
    result = compare((1, -1, 1), loads=[1, 0, 0])
    assert result.diagnostics['accepted_matches'] == 0


@pytest.mark.parametrize('energies,soc', [((6, -1, 1), 100), ((1, -6, 1), 50), ((1, -1, 1), 5)])
def test_reject_invalid_baseline_power_or_soc(energies, soc):
    with pytest.raises(ValueError):
        compare(energies, soc=soc)


def test_integer_commands_replay_within_limits_at_fractional_efficiencies():
    battery = replace(BATTERY, charge_efficiency=.93, discharge_efficiency=.87, min_soc_percent=10)
    items = slots([.87123, -1.078923, .87], [.2, .15, .4])
    result = cycles.cancel_cycles(items, battery, 30, TOMORROW)
    commands = [replace(i, target_battery_energy_kwh=opt.target_power_w(i)/4000)
                for i in result.adjusted.intervals]
    rounded = cycles.replay(commands, battery, 30)
    assert all(10-1e-9 <= i.expected_soc_percent <= 100+1e-9 for i in rounded.intervals)
    assert abs(rounded.intervals[-1].expected_soc_percent-result.adjusted.intervals[-1].expected_soc_percent) < .01


def test_debug_is_optional_bounded_and_indicates_truncation():
    items = slots([.001]*20 + [-.02]*20 + [.001]*20,
                  [.30]*20 + [.15]*20 + [.4]*20)
    result = cycles.cancel_cycles(items, BATTERY, 50, TOMORROW, debug=True)
    assert len(result.debug['matches']) == cycles.DEBUG_LIMIT
    assert result.debug['truncated']
    assert cycles.cancel_cycles(items, BATTERY, 50, TOMORROW).debug == {}

@pytest.mark.parametrize('seed', range(12))
def test_generated_feasible_horizons_conserve_energy_and_safe_integer_commands(seed):
    import random
    rng = random.Random(seed)
    battery = replace(BATTERY, max_charge_power_kw=4, max_discharge_power_kw=4,
                      charge_efficiency=.89, discharge_efficiency=.91, min_soc_percent=10)
    energy = 5.0
    commands, prices, demand = [], [], []
    for _ in range(100):
        if rng.random() < .4:
            command = -rng.uniform(0, 1)
            energy += min(-command * .89, 10-energy)
            price = rng.uniform(-.10, .20)
        else:
            command = min(rng.uniform(0, 1), max(0, energy-1)*.91)
            energy -= command/.91
            price = rng.uniform(.10, .40)
        commands.append(command)
        prices.append(price)
        demand.append(max(0, command)*rng.random())
    result = cycles.cancel_cycles(slots(commands, prices, demand), battery, 50, TOMORROW)
    before, after = result.baseline.summary, result.adjusted.summary
    assert (before.total_charge_kwh-after.total_charge_kwh)*.89 == pytest.approx(
        (before.total_discharge_kwh-after.total_discharge_kwh)/.91, abs=1e-8)
    assert result.adjusted.intervals[-1].expected_soc_percent == pytest.approx(
        result.baseline.intervals[-1].expected_soc_percent, abs=1e-8)
    commands = [replace(i, target_battery_energy_kwh=opt.target_power_w(i)/4000)
                for i in result.adjusted.intervals]
    actual = cycles.replay(commands, battery, 50)
    assert all(10-1e-8 <= i.expected_soc_percent <= 100+1e-8 for i in actual.intervals)
