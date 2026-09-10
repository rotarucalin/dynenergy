# DynEnergy

DynEnergy is a Home Assistant custom integration that creates and applies
day-ahead charge and discharge targets from EPEX Spot prices. It is battery
vendor-neutral: connect it to existing Home Assistant sensors and one writable
`input_number` helper rather than depending on a specific battery integration.

It also tracks the EPEX cost basis of energy held in the battery and the
realized EPEX savings when that energy is discharged.

## Features

- Creates the next day's battery plan at 23:50 local time from EPEX day-ahead
  prices.
- Updates a signed battery-power target every 15 minutes: negative Watts charge,
  positive Watts discharge, and zero is idle.
- Uses configurable battery capacity, power limits, SOC limits, and round-trip
  efficiency.
- Uses daily dynamic price thresholds with a 10 ct/kWh maximum charge price and
  a 13 ct/kWh minimum discharge price.
- Discharges existing energy before charging and available energy after charging
  to the highest-priced eligible household demand slots.
- Monitors cumulative battery charged/discharged energy every minute and
  publishes the cost of stored energy, total charging costs, and total savings.
- Learns a persistent 672-slot weekly consumption profile from the cumulative
  grid-import meter and exposes it through the Typical consumption sensor.
- Persists accounting data and meter baselines across Home Assistant restarts.

## Requirements

- Home Assistant with support for custom integrations.
- An EPEX day-ahead price sensor with a `data` attribute containing price
  intervals. DynEnergy supports the format exposed by
  [`ha_epex_spot`](https://github.com/mampfes/ha_epex_spot): `start_time`,
  `end_time`, and `price_per_kwh`.
- Battery sensors for SOC, usable capacity, power limits, and cumulative charged
  and discharged energy.
- An `input_number` helper that your battery automation or integration uses as a
  signed power target in Watts.

## Installation

### HACS

1. In Home Assistant, open **HACS**.
2. Open **Integrations**, select the three-dot menu, then choose
   **Custom repositories**.
3. Add this repository URL and select the **Integration** category.
4. Find **DynEnergy** in HACS and install it.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/dynenergy` directory into your Home Assistant
   configuration directory at `config/custom_components/dynenergy`.
2. Restart Home Assistant.

## Setup

1. Create an `input_number` helper for the battery target. Set its unit to `W`
   and choose limits that cover your permitted charge and discharge power, for
   example `-3000` to `3000`.
2. Ensure the automation or battery integration consuming this helper interprets
   negative values as charging and positive values as discharging.
3. Open **Settings > Devices & services > Add integration**, search for
   **DynEnergy**, and complete the configuration form.
4. Wait for the 23:50 local planning run, then review the `Battery plan` sensor
   before allowing the helper to control the battery.

Existing DynEnergy installations must use **Reconfigure** after upgrading to
select the cumulative battery charged and discharged energy sensors.

## Configuration

| Field | Required value | Notes |
|---|---|---|
| EPEX day-ahead price entity | Price sensor | Must expose supported `data` intervals in EUR/kWh. |
| Battery state of charge entity | Percent | Current battery SOC. |
| Battery power entity | Signed power sensor | Negative is charging, positive is discharging; currently used for diagnostics. |
| Cumulative battery charged energy entity | kWh total | Battery-side energy stored. Must increase while charging. |
| Cumulative battery discharged energy entity | kWh total | Battery-side energy removed. Must increase while discharging. |
| Usable battery capacity entity | kWh | Usable, not nameplate, capacity. |
| Maximum charging power entity | kW | Physical charge limit. |
| Maximum discharging power entity | kW | Physical discharge limit. |
| Cumulative grid-import energy entity | kWh total | Used every 15 minutes to learn the typical weekly consumption profile. |
| Writable battery power helper | `input_number`, W | Negative charge, positive discharge, zero idle. |
| Charge price threshold | EUR/kWh | Additional user cap; defaults to `0.10`. |
| Minimum / maximum SOC | Percent | Must be ordered and within 0-100. |
| Charge / discharge efficiency | Decimal | Defaults to `0.95` for each direction. |
| Battery degradation cost | EUR/kWh | Included in the plan cost estimate. Defaults to `0`. |

The charged and discharged totals must be battery-side energy counters. If they
reset, DynEnergy treats the decreased value as a new baseline; it does not
create a false charging or discharging transaction.

## How Planning Works

DynEnergy converts hourly price data to 15-minute intervals when necessary and
uses the actual timestamps, including daylight-saving days with 92 or 100
intervals. It calculates thresholds from the available daily EPEX range:

$$
T_{charge}=\min\left(T_{configured}, 0.10, P_{min}+0.25(P_{max}-P_{min})\right)
$$

$$
T_{pre}=\max\left(0.13, P_{min}+0.50(P_{max}-P_{min})\right)
$$

$$
T_{post}=\max\left(0.13, P_{min}+0.70(P_{max}-P_{min})\right)
$$

All prices are in EUR/kWh. The horizon is split into contiguous blocks of
intervals priced strictly below $T_{charge}$ and the discharge gaps around them,
then walked in order, so a day with a cheap night and a cheap midday runs two
charge and discharge cycles. Each gap is planned against the block that follows
it: when that block can refill the battery on its own the gap may empty it down
to minimum SOC, otherwise energy is held back for $T_{pre}$. The final gap, with
no block after it, uses $T_{post}$.

Within a block, intervals are grouped into 1 ct/kWh buckets and taken cheapest
first. A bucket that can supply everything still needed carries an equal share
in each of its intervals; a bucket that cannot runs at full power and the
remainder descends to the next bucket up. The request is 20% of usable capacity
larger than the deficit, so a battery charging more slowly than commanded still
reaches its target. That margin is commanded but never counted as absorbed.

Discharge is normally limited to the forecast demand of the interval. When a
price climbs more than 30 ct/kWh above the cost basis of the stored energy, the
interval is treated as a spike: it qualifies on its own, without also clearing
the discharge threshold, and is planned at full discharge power. The battery
automation scales the request down to whatever the house is really drawing, so
the plan reports the part booked above the forecast separately as
`spike_discharge_kwh`. The cost basis is the average price the plan pays in the
charge blocks already walked, falling back to the measured `Stored energy cost`
for the part of the day before the first block.

The initial demand profile is 0.3125 kWh (an average 1.25 kW) per 15 minutes
from Monday to Thursday, 07:45-18:30, and Friday, 07:45-13:30. All other
intervals use 0.015 kWh (an average 60 W). DynEnergy keeps one running average
for each of the 672 quarter-hour slots in a week. At every quarter-hour boundary
it adds the completed interval's grid-import delta to the corresponding average
and persists the profile. New plans use the learned values.

## Entities

DynEnergy creates these sensors:

| Entity name | Unit | Meaning |
|---|---|---|
| Battery plan | EUR | Expected daily EPEX saving from the current day-ahead plan. Its attributes contain the full schedule, plan summary, source readings, and status. |
| Battery power recommendation | W | Current signed battery target, with the complete plan in its attributes. |
| Typical consumption | W | Learned average for the current weekly slot. Its attributes contain all 672 weekly averages and their sample counts. |
| Stored energy cost | ct/kWh | Weighted-average EPEX cost basis of energy currently stored in the battery. Its attributes add the stored energy, the total charged energy, and the lifetime average price paid per charged kWh. |
| Total costs | EUR | Cumulative EPEX value of actual measured battery charging. |
| Total savings | EUR | Cumulative EPEX value of actual measured battery discharge. |

## Battery Cost Monitoring

Every minute, DynEnergy compares the two configured cumulative battery-energy
counters with their previous readings. Actual charged consumption $\Delta E_c$
updates `Total costs` by:

$$
\Delta E_cP_{EPEX}
$$

Actual discharged generation $\Delta E_d$ updates `Total savings` by:

$$
\Delta E_dP_{EPEX}
$$

At first installation, the energy implied by current SOC and usable capacity is
entered at the current EPEX price. What it really cost is unknowable, but a zero
basis would understate every average until that energy is discharged. The
current energy-counter readings become the baseline, so previous charging and
discharging never appear in `Total costs` or `Total savings`. The ledger is
saved across Home Assistant restarts.

How much energy the battery holds is taken from SOC and usable capacity on every
update, not from the meter deltas. A round trip charges more than it discharges,
so a ledger driven by deltas alone climbs past the physical capacity and turns
the cost basis into a lifetime average over energy that is not there. Re-reading
the real value keeps the divisor honest whichever side of the inverter the
counters sit on, and the recorded cost is scaled with the correction so that
fixing the amount of energy never silently reprices it.

When upgrading from an earlier accounting version, the previous totals are reset
because they cannot be corrected without historical meter readings.

This accounting uses spot EPEX prices only. It does not include electricity
taxes, network charges, VAT, fixed tariff components, export remuneration, or
battery degradation in `Total savings`.

## Automation Example

DynEnergy writes the selected helper directly. A battery-specific automation can
react to it as follows:

```yaml
automation:
  - alias: Apply DynEnergy battery target
    triggers:
      - trigger: state
        entity_id: input_number.dynenergy_battery_power_target
    actions:
      - choose:
          - conditions: "{{ states('input_number.dynenergy_battery_power_target') | float < 0 }}"
            sequence:
              # Call your battery integration's charge service here.
          - conditions: "{{ states('input_number.dynenergy_battery_power_target') | float > 0 }}"
            sequence:
              # Call your battery integration's discharge service here.
        default:
          # Call your battery integration's idle service here.
```

Replace the placeholder service calls with the services of your battery
integration. Validate the sign convention and power limits with the battery
disconnected from automatic control first.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Battery plan` remains unavailable | Day-ahead planning runs at 23:50 local time. Confirm that the EPEX entity provides tomorrow's intervals and inspect `planning_error` in the sensor attributes. |
| Accounting sensors do not update | Confirm both battery energy counters are numeric cumulative kWh totals. Inspect `monitoring_error` on `Battery plan`. |
| No target is applied | Confirm the writable entity is an `input_number`, has adequate negative and positive limits, and your battery automation reads it. |
| Unexpected accounting total after a meter reset | A reduced counter value is intentionally used as a new baseline. The next positive increment will be accounted normally. |
| Plan times look shifted | Confirm the upstream price intervals contain timezone-aware ISO timestamps. |

## Current Limitations

- The learned consumption profile uses grid import as the consumption signal;
  behind-the-meter generation is not included in that profile.
- Discharge only offsets modeled household demand. Export optimization is out of
  scope.
- The plan is generated once daily and assumes the current SOC is the opening
  SOC for the next day.
- Cost monitoring uses raw EPEX prices, not all-in retail electricity prices.

## Development

Run the pure domain tests from the repository root:

```powershell
$env:PYTHONPATH = (Get-Location).Path
pytest tests
```

Home Assistant imports require a Home Assistant development environment; the
pure optimizer and accounting tests do not require it.
