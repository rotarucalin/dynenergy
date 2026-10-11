# Energy-matched cycle cancellation

## Scope and candidate source

This checkout started at version 0.2.0 and contained no 0.2.1 blanket-hold rule,
spread comparison, hold state, or obsolete hold configuration. No such gate is
introduced. All execution decisions come from the effective per-slot plan.
The existing greedy planner remains the only source of candidate allocations.
This is a deterministic reduction pass, not a general optimization solver.

The quarter-hour callback starts the daily comparison at 15:30 in HA's local
timezone. It snapshots today's remaining schedule before awaiting tomorrow's
price refresh. Starting with measured SOC, it replays today's baseline to obtain
tomorrow's opening SOC. Tomorrow is then planned greedily from that projected
value, rather than today's measured SOC. An infeasible current baseline is rebuilt
from its remaining prices and current measurements before comparison.

Tomorrow must cover the entire local date with finite prices and aware, unique,
quarter-aligned timestamps. Hourly records expand in UTC. Coverage validation,
sorting and interval selection use absolute time, including both occurrences of
the repeated autumn hour. Local dates determine day and block membership.
Consequently 23/25-hour days contain 92/100 quarters.

## Discharge eligibility before cancellation

The greedy planner now requires `minimum_discharge_profit_per_kwh` (default 0.15)
net EUR per delivered kWh before authorizing any discharge. This applies to spikes,
ordinary demand, before/after charging, pooled blocks and lookahead simulations.
Setting it to zero explicitly opts out of the additional profit requirement.

A separate forecast ledger combines the opening acquisition cost with subsequent
absorbed purchases and removes discharged energy at weighted cost. Purchases cost
`grid_charge_kwh * price` and add `grid_charge_kwh * charge_efficiency` battery kWh.
A stored battery kWh costs `basis / discharge_efficiency` per delivered grid kWh.
The gate adds configured degradation and the required net profit to that delivered
cost. It also applies to the estimated replacement cost when a refill block follows;
cheap old energy must not justify an extra low-margin refill cycle. Ordinary daily
price bands and demand/power/SOC limits still apply. Equality is eligible.

Measured accounting deliberately multiplies battery-side meter deltas by raw EPEX
prices without efficiency adjustments. Its basis is therefore divided by charge
efficiency once when entering this forecast. The already-adjusted forecast basis
is carried across midnight explicitly, without applying charging losses twice or
changing the account. If acquisition history is unavailable, the existing fallback
to the lowest horizon price remains an estimate, not a known historical purchase.

The required 15 ct profit does not replace or get added to the 6 ct cancellation
allowance. The former determines greedy discharge eligibility; the latter compares
matched optional cycles. Ten-cent purchases followed by sixteen-cent discharge
fail the profit gate even when the old daily price bands would allow discharge.
The greedy charging target itself remains unchanged: energy can be bought and held
for a future opportunity rather than discharged at an inadequate margin.

Per-slot `minimum_discharge_price_per_kwh` reports the economic gate (daily price
bands can impose a stricter threshold). `expected_discharge_profit_per_kwh` reports
net profit against the weighted stored basis for the effective discharge; idle
and charge slots report no discharge profit. `minimum_discharge_profit_per_kwh`
and the projected tomorrow battery-side acquisition cost appear on Battery plan.

Saved-plan policy version 2 invalidates earlier plans that did not enforce this
minimum. Those schedules are rebuilt on startup; accounting storage is unaffected.
Charge pooling uses a 1e-7 kWh convergence tolerance (below one-Watt resolution)
and at most 256 refinements, preventing a floating-point residual from indefinitely
blocking the coordinator. Non-convergence is a planning error and retains the
previous valid schedule.

## Units and economics

- Slot targets and physical allocations are signed **grid-side kWh**: charging
  negative, discharge positive. Commands are signed integer W.
- All matching and reservation ledgers use **battery-side kWh**.
- Prices, stored cost basis, degradation and cycle allowance are EUR/kWh.
- SOC is a percentage of configured usable capacity, including the minimum SOC.
- `cycle_cost_per_kwh` defaults to **0.06 EUR per delivered grid kWh**.

For each matched battery quantity `q`, cancel `q * discharge_efficiency` early
(grid) discharge and `q / charge_efficiency` replacement (grid) charge. Reserve
`q` for eligible later discharge without removing that reserved quantity.

The decision is `early_price <= replacement_price + cycle_cost_per_kwh`, with
1e-9 EUR/kWh comparison tolerance. Equality prefers fewer cycles. Original stored
energy cost and later discharge price cancel between the compared strategies;
neither is an additional economic threshold. They still affect accounting and
normal greedy eligibility. Invariance to these prices is conditional on identical
eligible quantities and demand, not a promise that greedy planning ignores them.

The allowance is a **fixed economic approximation**, already including estimated
losses and wear. Physical efficiency factors match quantities exactly but do not
add another economic loss surcharge. The existing degradation setting is included
in ordinary summary costs only, never added to the cancellation allowance.

For an accepted match the two distinct diagnostic changes are:

```
predicted_cash_flow_difference_eur =
    (q / charge_efficiency) * replacement_price
    - (q * discharge_efficiency) * early_price

estimated_policy_benefit_eur =
    (q * discharge_efficiency) * (replacement_price + cycle_cost - early_price)
```

Positive means cancelling improves that respective measure. The cash-flow figure
contains no wear allowance. The policy figure uses the fixed price approximation;
at nonideal efficiency it is deliberately not cash-flow change plus another loss
charge. Summary savings may also include the configured degradation cost and
therefore are not the raw cash-flow difference.

At ideal efficiency, 1 kWh at 20/15/40 ct cancels: raw cash-flow change is **-0.05
EUR**, estimated policy benefit **+0.01 EUR**. At 25/15/40 ct no energy is removed.
Stored purchase cost of 0 ct is retained for accounting but does not gate either
result. Changing the eligible later discharge price alone leaves the decision
unchanged.

## Shared allocation ledger

D1 denotes today's remaining planned discharges. For each chronological tomorrow
charge block C2, D2 denotes tomorrow discharges preceding the block and D3 denotes
forecast-supported discharges after it. Blocks are contiguous commanded-charge
slots, including surplus-margin slots. Groups may share discharges: a discharge
between blocks can qualify as D3 for one and D2 for another.

Process the union of D1 and eligible D2 in ascending price order. For each early
slot, try eligible C2 quantities in descending price order and eligible D3 in
ascending price order. Absolute timestamps break ties. Search alternative charge
and later slots when one pairing is infeasible. Require strict chronological order
and block membership: early before the charge block, later after it.

All groups share one allocation array and one reservation array. Available early
energy excludes already reserved battery energy. Available later energy is the
smaller of its remaining discharge allocation and forecast consumption, less
reservations. Thus a speculative spike command beyond demand supplies no extra D3
eligibility, and a removed or reserved quantity cannot be used twice.

For every proposed match, bound q by early energy, absorbed charge, unreserved
later demand and minimum SOC headroom over the interval from early discharge
through the slot before replacement charging. Retaining q raises the battery's
trajectory by q only in that interval. This headroom bound is the exact largest
feasible q for fixed allocations; intervening charging at full SOC can reduce it
to zero. Replay the tentative combined schedule, checking power and SOC bounds.
Accepted partial reductions continue until quantities are exhausted.

Accepted reductions have a durable early/charge/later ledger even when detailed
HA debug is disabled. Ledger validation reconstructs every changed allocation and
checks reserved demand. Exact allocation conservation tolerance is 1e-9 kWh;
terminal SOC comparison tolerance is 1e-7 percentage points. This validates each
revision against its own baseline and measured opening, not yesterday's forecast.

## Commands, forecasts and accounting

The greedy planner's extra 20% charging margin is a command, not absorbed energy.
Baseline replay clips it at physical capacity. Matching uses only that absorbed
energy. For a reduced charging slot the effective command is capped at its new
physical allocation, with **no extra margin**. Unmatched commands retain normal
planning behavior. A second replay verifies that commands cannot buy back removed
charge. Forecasts and monetary summaries use absorbed allocations.

Integer Watts truncate toward zero. A separate chronological command replay caps
rounded discharge against available energy, ensuring rounding cannot cross the
SOC floor; charge absorption remains capped at maximum SOC. Exact allocation SOC
and `integer_command_expected_soc_percent` are exposed separately. Quantization is
at most one W (0.00025 grid kWh per quarter) per original conversion; accumulated
undercharging may further limit a later discharge at minimum SOC. This tiny
execution difference does not enter the policy match ledger. The power sensor,
per-slot effective targets and actual helper writes all use the same integer
command. Original targets remain available for diagnosis.

The summary covers the remaining published horizon, potentially spanning two
local dates. Past slots expire from the published intervals and summary. D1/D2/C2/
D3 diagnostics describe the comparison snapshot; D2 and D3 categories can overlap
and must not be added as disjoint totals. D3 totals are forecast-limited. Monetary
and cancellation totals also refer to the comparison snapshot, not realized use.

Measured cost accounting is an independent meter-based process. Comparison,
forecasting, persistence and hypothetical cancellations never modify its totals.

## Publication, recovery and drift

A coordinator lock serializes planning and command writes. The active schedule
remains visible during candidate construction. Only a completely validated,
successfully persisted candidate is published. Price, planning or storage failures
retain the active plan and expose a reason with `retry_pending`. Quarter-hour
callbacks retry after 15:30. Repeated callbacks for an already compared date do not
apply reductions again, except when an integration reload explicitly requests a
new comparison.

A successful entry unload marks its next setup in the same HA instance as a
reload. That setup requests fresh greedy candidates for today's remainder and
tomorrow from measured SOC, refreshed prices, the rebuilt consumption profile and
current configuration. It bypasses the saved comparison date and runs even before
15:30 when tomorrow's complete prices are available. It never uses previously
reduced allocations as new greedy candidates. Reloads between quarter boundaries
defer to the next boundary. The request remains pending through input, price,
planning or storage failures and retries each quarter, retaining the last valid
schedule where possible. An expired schedule falls back to current-day planning
while comparison remains pending. Successful publication clears the request and
reports `rebuild_reason: reload`. A normal HA restart still restores its saved
schedule without forcing a new comparison.

The legacy 23:50 callback refreshes the consumption profile and preserves any
adjusted plan. Comparison requested between boundaries defers to the next quarter;
it does not replay a whole quarter from a partial-quarter measurement. Midnight
continues the saved tomorrow schedule rather than generating an unadjusted plan.
If no valid future schedule exists at midnight, normal current-day greedy planning
resumes and the next cross-day comparison starts that afternoon.

HA storage contains baseline and adjusted dated intervals, battery parameters,
opening SOC, comparison date/revision, match ledger, diagnostics and optional debug.
Restore replays both forecasts, validates their ledger and recalculates integer
commands before exposing the unexpired remainder. Yesterday's price sensor data
is unnecessary. Expired or invalid stored horizons are ignored. If numeric inputs
are temporarily unavailable, startup readiness checks retry restoration first.
Changed physical battery configuration invalidates the saved schedule.

At quarter boundaries, a measured SOC deviation **greater than 5 percentage
points** from the predicted opening causes fresh greedy planning over remaining
cached prices using current measurements. The next day's opening is projected
again and a new match ledger is built from these fresh candidates. Previous
reductions are not subtracted again. Mid-slot restarts defer drift rebuilding until
the next quarter (at most 15 minutes). Failure retains the last valid schedule.

Optional debug includes sorted candidates and accepted/rejected attempts with
prices, thresholds, timestamps, quantities, before/after allocations and reasons.
Each candidate list and attempt list is bounded to 200 entries, with truncation
and attempt count reported. `comparison_debug`, schedules and summaries are
excluded from recorder history. The durable acceptance ledger remains complete.

## Verification and limits

Run `python -m pip install -r requirements-dev.txt`, then
`python -m pytest tests -q -p no:cacheprovider`. The suite includes economic
examples, physical conservation, sorting and split matches, reservations across
blocks, headroom, command margin, rounding, DST, persistence, failed publication,
SOC drift and sensor/helper/accounting integration. HA lifecycle tests use local
stand-ins; a live inverter and live HA installation are not exercised.

The policy compares the existing greedy candidates; it does not search for a
globally optimal schedule or invent new demand. Unknown solar and demand changes
remain forecast errors handled by measured SOC drift and normal battery limits.
