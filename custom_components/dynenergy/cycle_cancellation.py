"""Energy-matched cycle cancellation; all internal ledgers are battery-side kWh.

The greedy plan remains the candidate source. This module only removes matched
energy, never invents charging or demand, and never touches measured accounting.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from math import floor, isfinite

from . import optimizer as opt

EPS = 1e-9
DEFAULT_CYCLE_COST = 0.06
DEBUG_LIMIT = 200


@dataclass(frozen=True)
class Comparison:
    baseline: opt.OptimizationPlan
    adjusted: opt.OptimizationPlan
    diagnostics: dict
    debug: dict
    match_ledger: tuple[dict, ...]


def energy_bounds(battery: opt.BatteryParameters) -> tuple[float, float]:
    return (battery.usable_capacity_kwh * battery.min_soc_percent / 100,
            battery.usable_capacity_kwh * battery.max_soc_percent / 100)


def summarize(
    intervals: Sequence[opt.PlanInterval], battery: opt.BatteryParameters,
    full_charge_feasible: bool | None = None,
) -> opt.OptimizationPlan:
    """Summarize physical allocations, not the charging command margin."""
    inputs = opt.OptimizerInputs(
        [i.timestamp for i in intervals], [i.price_per_kwh for i in intervals],
        [i.consumption_kwh for i in intervals], 0, battery,
    )
    energies = [i.allocated_grid_energy_kwh if i.allocated_grid_energy_kwh is not None
                else i.target_battery_energy_kwh for i in intervals]
    if full_charge_feasible is None:
        full_charge_feasible = any(i.expected_soc_percent >= battery.max_soc_percent - EPS for i in intervals)
    summary = opt._build_summary(
        inputs, intervals, [max(0, -e) for e in energies],
        [max(0, e) for e in energies], full_charge_feasible,
    )
    charge_prices = [i.price_per_kwh for i, e in zip(intervals, energies) if e < -EPS]
    discharge_prices = [i.price_per_kwh for i, e in zip(intervals, energies) if e > EPS]
    return opt.OptimizationPlan(tuple(intervals), replace(
        summary, highest_charge_price_per_kwh=max(charge_prices, default=None),
        lowest_discharge_price_per_kwh=min(discharge_prices, default=None),
    ))


def replay(
    intervals: Sequence[opt.PlanInterval], battery: opt.BatteryParameters,
    opening_soc: float, *, allocations: Sequence[float] | None = None,
) -> opt.OptimizationPlan:
    """Replay commands or exact allocations, rejecting infeasible physical plans.

    Explicit allocations must be absorbed in full. Unmodified baseline commands
    can include the optimizer's margin, which is clipped to physical headroom.
    """
    opt._validate_charge_inputs(opt.OptimizerInputs(
        [i.timestamp for i in intervals], [i.price_per_kwh for i in intervals],
        [i.consumption_kwh for i in intervals], opening_soc, battery,
    ), 0)
    low, high = energy_bounds(battery)
    energy = opening_soc * battery.usable_capacity_kwh / 100
    if not isfinite(energy) or not low - EPS <= energy <= high + EPS:
        raise ValueError("Opening SOC is outside configured limits")
    result = []
    previous = float('-inf')
    for index, interval in enumerate(intervals):
        if interval.timestamp.utcoffset() is None:
            raise ValueError("Schedule timestamps must be aware")
        stamp = interval.timestamp.timestamp()
        if stamp <= previous:
            raise ValueError("Schedule timestamps must be unique and chronological")
        previous = stamp
        command = interval.target_battery_energy_kwh
        if not all(isfinite(v) for v in (command, interval.price_per_kwh,
                                        interval.consumption_kwh)):
            raise ValueError("Schedule values must be finite")
        limit = (battery.max_charge_power_kw if command < 0
                 else battery.max_discharge_power_kw) * opt.INTERVAL_HOURS
        if abs(command) > limit + EPS or interval.consumption_kwh < 0:
            raise ValueError("Schedule exceeds power or demand constraints")
        if allocations is None:
            allocation = (-min(-command, max(0, high - energy) / battery.charge_efficiency)
                          if command < 0 else command)
        else:
            allocation = allocations[index]
            if abs(allocation) > abs(command) + EPS or allocation * command < -EPS:
                raise ValueError("Allocation exceeds its command")
        energy += (-allocation * battery.charge_efficiency if allocation < 0
                   else -allocation / battery.discharge_efficiency)
        if not low - EPS <= energy <= high + EPS:
            raise ValueError("Schedule exceeds SOC limits")
        result.append(replace(interval, allocated_grid_energy_kwh=allocation,
                              expected_soc_percent=energy / battery.usable_capacity_kwh * 100))
    return summarize(result, battery)


def round_commands(
    plan: opt.OptimizationPlan, battery: opt.BatteryParameters, opening_soc: float,
) -> tuple[opt.OptimizationPlan, float]:
    """Truncate Watts, capping discharge at the rounded trajectory's SOC floor.

    Exact allocations retain battery-side conservation. Integer execution is a
    separately replayed approximation; it cannot breach either SOC boundary.
    """
    energy = opening_soc * battery.usable_capacity_kwh / 100
    low, high = energy_bounds(battery)
    result = []
    for slot in plan.intervals:
        watts = int(slot.target_battery_energy_kwh * 4000)
        if watts > 0:
            watts = min(watts, floor(max(0, energy - low) * battery.discharge_efficiency * 4000 + EPS))
        grid = watts / 4000
        absorbed = min(-grid, max(0, high - energy) / battery.charge_efficiency) if watts < 0 else 0
        energy += absorbed * battery.charge_efficiency if watts < 0 else -grid / battery.discharge_efficiency
        result.append(replace(slot, command_power_w=watts,
                              command_expected_soc_percent=energy / battery.usable_capacity_kwh * 100,
                              command_grid_energy_kwh=-absorbed if watts < 0 else grid))
    return summarize(result, battery), energy / battery.usable_capacity_kwh * 100


def cancel_cycles(
    intervals: Sequence[opt.PlanInterval], battery: opt.BatteryParameters,
    opening_soc: float, tomorrow: date, cycle_cost: float = DEFAULT_CYCLE_COST,
    *, debug: bool = False,
) -> Comparison:
    """Remove economic early/replacement pairs while reserving later demand.

    The fixed allowance uses grid prices, with no stored-cost, degradation or
    extra loss threshold. Exact efficiencies apply to physical quantities only.
    All charge blocks share discharge and reservation ledgers. Headroom between
    early and replacement slots gives the exact maximum feasible reduction.
    """
    if not isfinite(cycle_cost) or cycle_cost < 0:
        raise ValueError("Cycle cost must be finite and nonnegative")
    baseline = replay(intervals, battery, opening_soc)
    slots = baseline.intervals
    allocations = [i.allocated_grid_energy_kwh for i in slots]
    original = allocations.copy()
    reserved = [0.0] * len(slots)
    removed = [0.0] * len(slots)
    tomorrow_indices = {n for n, i in enumerate(slots) if i.timestamp.date() >= tomorrow}
    charges = [n for n in tomorrow_indices if allocations[n] < -EPS]
    # Blocks include margin-only charge commands so their boundaries stay stable.
    blocks = []
    for n in sorted(tomorrow_indices):
        if slots[n].target_battery_energy_kwh < 0:
            if blocks and n == blocks[-1][-1] + 1:
                blocks[-1].append(n)
            else:
                blocks.append([n])
    block_for = {n: block for block in blocks for n in block}
    discharges = [n for n, e in enumerate(allocations) if e > EPS]
    d1 = [n for n in discharges if n not in tomorrow_indices]
    d2 = [n for n in discharges if n in tomorrow_indices and any(n < b[0] for b in blocks)]
    d3 = [n for n in discharges if n in tomorrow_indices and any(n > b[-1] for b in blocks)]
    ascending = lambda n: (slots[n].price_per_kwh, slots[n].timestamp.timestamp())
    early_order = sorted(set(d1 + d2), key=ascending)
    charge_order = sorted(charges, key=lambda n: (-slots[n].price_per_kwh,
                                                 slots[n].timestamp.timestamp()))
    later_order = sorted(d3, key=ascending)
    low, high = energy_bounds(battery)
    eta_c, eta_d = battery.charge_efficiency, battery.discharge_efficiency
    decisions = []
    match_ledger = []
    accepted = 0
    attempts = 0
    cash_difference = policy_benefit = cancelled_battery = 0.0

    def record(e, c, later, q, reason, before=None):
        nonlocal attempts
        attempts += 1
        if reason == 'accepted' or (debug and len(decisions) < DEBUG_LIMIT):
            decision = {
                'early_timestamp': slots[e].timestamp.isoformat(),
                'charge_timestamp': slots[c].timestamp.isoformat(),
                'later_timestamp': slots[later].timestamp.isoformat(),
                'early_price_eur_per_kwh': slots[e].price_per_kwh,
                'charge_price_eur_per_kwh': slots[c].price_per_kwh,
                'later_price_eur_per_kwh': slots[later].price_per_kwh,
                'threshold_eur_per_kwh': slots[c].price_per_kwh + cycle_cost,
                'matched_battery_kwh': q, 'removed_discharge_grid_kwh': q * eta_d,
                'removed_charge_grid_kwh': q / eta_c, 'reason': reason,
                'before_grid_kwh': before if before is not None else [allocations[e], allocations[c], allocations[later]],
                'after_grid_kwh': [allocations[e], allocations[c], allocations[later]],
            }
            if reason == 'accepted':
                match_ledger.append(decision)
            if debug and len(decisions) < DEBUG_LIMIT:
                decisions.append(decision)

    for e in early_order:
        for c in charge_order:
            if e >= block_for[c][0] or allocations[c] >= -EPS:
                continue
            for later in later_order:
                early_left = allocations[e] / eta_d - reserved[e]
                if early_left <= EPS or -allocations[c] * eta_c <= EPS:
                    break
                if later <= block_for[c][-1]:
                    continue
                later_left = min(allocations[later], slots[later].consumption_kwh) / eta_d - reserved[later]
                if later_left <= EPS:
                    continue
                if slots[e].price_per_kwh > slots[c].price_per_kwh + cycle_cost + EPS:
                    record(e, c, later, 0, 'price_above_threshold')
                    continue
                # Retaining q raises every intermediate battery energy by q;
                # after the replacement slot the trajectory is identical.
                current = replay(slots, battery, opening_soc, allocations=allocations)
                headroom = min(high - i.expected_soc_percent * battery.usable_capacity_kwh / 100
                               for i in current.intervals[e:c])
                q = max(0.0, min(early_left, -allocations[c] * eta_c, later_left, headroom))
                if q <= EPS:
                    record(e, c, later, 0, 'no_soc_headroom')
                    continue
                before = [allocations[e], allocations[c], allocations[later]]
                trial = allocations.copy()
                trial[e] -= q * eta_d
                trial[c] += q / eta_c
                trial = [0.0 if abs(v) < EPS else v for v in trial]
                replay(slots, battery, opening_soc, allocations=trial)
                allocations = trial
                reserved[later] += q
                removed[e] += q * eta_d
                removed[c] += q / eta_c
                cancelled_battery += q
                cash_difference += q / eta_c * slots[c].price_per_kwh - q * eta_d * slots[e].price_per_kwh
                policy_benefit += q * eta_d * (slots[c].price_per_kwh + cycle_cost - slots[e].price_per_kwh)
                accepted += 1
                record(e, c, later, q, 'accepted', before)

    adjusted_slots = []
    for n, slot in enumerate(slots):
        # Strip all surplus margin from a reduced charge command. Otherwise the
        # command would buy back energy we just removed from the allocation.
        command = allocations[n] if removed[n] > EPS else slot.target_battery_energy_kwh
        adjusted_slots.append(replace(
            slot, target_battery_energy_kwh=command,
            original_target_power_w=opt.target_power_w(slot),
            state=(opt.OperatingState.CHARGE if command < -EPS else
                   opt.OperatingState.DISCHARGE if command > EPS else opt.OperatingState.IDLE),
        ))
    adjusted = replay(adjusted_slots, battery, opening_soc, allocations=allocations)
    # Also simulate the actual commands: margins must not undo any reduction.
    command_plan = replay(adjusted_slots, battery, opening_soc)
    if any(abs(a.allocated_grid_energy_kwh - b.allocated_grid_energy_kwh) > EPS
           for a, b in zip(adjusted.intervals, command_plan.intervals)):
        raise ValueError("Charging margin would change matched allocations")
    if slots and abs(adjusted.intervals[-1].expected_soc_percent - slots[-1].expected_soc_percent) > 1e-7:
        raise ValueError("Cancellation changed terminal SOC")
    adjusted, rounded_terminal_soc = round_commands(adjusted, battery, opening_soc)
    diagnostics = {
        'comparison_status': 'adjusted' if accepted else 'unchanged',
        'cycle_cost_per_kwh': cycle_cost, 'accepted_matches': accepted,
        'cancelled_discharge_grid_kwh': cancelled_battery * eta_d,
        'cancelled_charge_grid_kwh': cancelled_battery / eta_c,
        'reserved_D3_battery_kwh': sum(reserved),
        'baseline_terminal_soc_percent': slots[-1].expected_soc_percent if slots else opening_soc,
        'adjusted_terminal_soc_percent': adjusted.intervals[-1].expected_soc_percent if slots else opening_soc,
        'integer_command_terminal_soc_percent': rounded_terminal_soc,
        'predicted_cash_flow_difference_eur': cash_difference,
        'estimated_policy_benefit_eur': max(0.0, policy_benefit),
        'decision_reason': 'matched_cycles_removed' if accepted else 'no_economic_feasible_match',
        'charge_block_count': len(blocks),
    }
    for name, indices in [('D1', d1), ('D2', d2), ('C2', charges), ('D3', d3)]:
        energy = sum(min(original[n], slots[n].consumption_kwh) if name == 'D3'
                     else abs(original[n]) for n in indices)
        diagnostics[name + '_slot_count'] = len(indices)
        diagnostics[name + '_grid_kwh'] = energy
        diagnostics[name + '_battery_kwh'] = energy * eta_c if name == 'C2' else energy / eta_d
    detail = {}
    if debug:
        detail = {'matches': decisions, 'truncated': attempts > DEBUG_LIMIT,
                  'decision_count': attempts, 'sorted_candidates': {}}
        for name, indices in [('early', early_order), ('charge', charge_order), ('later', later_order)]:
            detail['sorted_candidates'][name] = [
                {'timestamp': slots[n].timestamp.isoformat(), 'price_eur_per_kwh': slots[n].price_per_kwh,
                 'grid_kwh': min(original[n], slots[n].consumption_kwh) if name == 'later' else original[n]}
                for n in indices[:DEBUG_LIMIT]]
            detail['truncated'] |= len(indices) > DEBUG_LIMIT
    validate_matches(baseline, adjusted, match_ledger, battery)
    return Comparison(baseline, adjusted, diagnostics, detail, tuple(match_ledger))


def validate_matches(baseline, adjusted, matches, battery):
    """Audit every removed quantity against the durable early/charge/later ledger."""
    original = {i.timestamp.timestamp(): i for i in baseline.intervals}
    final = {i.timestamp.timestamp(): i for i in adjusted.intervals}
    if original.keys() != final.keys():
        raise ValueError("Baseline and adjusted horizons differ")
    changes = dict.fromkeys(original, 0.0)
    reserved = dict.fromkeys(original, 0.0)
    for match in matches:
        e, c, later = [datetime.fromisoformat(match[key]).timestamp()
                       for key in ('early_timestamp', 'charge_timestamp', 'later_timestamp')]
        q = match['matched_battery_kwh']
        if not isfinite(q) or q <= 0 or not e < c < later:
            raise ValueError("Invalid match ledger")
        changes[e] -= q * battery.discharge_efficiency
        changes[c] += q / battery.charge_efficiency
        reserved[later] += q
    for stamp, slot in original.items():
        if abs(slot.allocated_grid_energy_kwh + changes[stamp] - final[stamp].allocated_grid_energy_kwh) > EPS:
            raise ValueError("Unmatched energy reduction")
        supported = max(0, min(final[stamp].allocated_grid_energy_kwh, slot.consumption_kwh))
        if reserved[stamp] * battery.discharge_efficiency > supported + EPS:
            raise ValueError("Later demand was reserved twice or removed")


def serialize_plan(plan):
    return [{**asdict(i), 'timestamp': i.timestamp.isoformat(), 'state': i.state.value}
            for i in plan.intervals]


def deserialize_plan(rows, battery):
    intervals = [opt.PlanInterval(**{**row, 'timestamp': datetime.fromisoformat(row['timestamp']),
                                    'state': opt.OperatingState(row['state'])}) for row in rows]
    return summarize(intervals, battery)


def validate_saved_plan(plan, battery, opening_soc):
    """Validate both the serialized forecast and the corresponding commands."""
    verified = replay(plan.intervals, battery, opening_soc)
    for saved, actual in zip(plan.intervals, verified.intervals, strict=True):
        if (saved.allocated_grid_energy_kwh is None
                or not isfinite(saved.allocated_grid_energy_kwh)
                or not isfinite(saved.expected_soc_percent)
                or abs(saved.allocated_grid_energy_kwh - actual.allocated_grid_energy_kwh) > EPS
                or abs(saved.expected_soc_percent - actual.expected_soc_percent) > 1e-7):
            raise ValueError("Persisted forecast disagrees with its commands")
    return verified
