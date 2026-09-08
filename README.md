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
| Cumulative grid-import energy entity | kWh total | Reserved for future consumption learning; the current release uses its fixed profile. |
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

All prices are in EUR/kWh. The algorithm first uses energy already above the
configured minimum SOC to cover expensive demand before charging. It then fills
the battery in the cheapest slots strictly below $T_{charge}$. After the final
charge slot, it may discharge any available energy above minimum SOC into later
demand at or above $T_{post}$; the battery does not need to reach maximum SOC.

The built-in demand profile is 0.2 kWh per 15 minutes from Monday to Thursday,
07:45-18:30, and Friday, 07:45-13:00. All other intervals use 0.015 kWh.

## Entities

DynEnergy creates these sensors:

| Entity name | Unit | Meaning |
|---|---|---|
| Battery plan | EUR | Expected daily EPEX saving from the current day-ahead plan. Its attributes contain the full schedule, plan summary, source readings, and status. |
| Stored energy cost | ct/kWh | Weighted-average EPEX cost basis of energy currently stored in the battery. |
| Total costs | EUR | Cumulative EPEX cost paid to charge the battery since accounting began. |
| Total savings | EUR | Cumulative avoided EPEX cost less the cost basis of discharged energy. |

## Battery Cost Monitoring

Every minute, DynEnergy compares the two configured cumulative battery-energy
counters with their previous readings. For battery-side charged energy
$\Delta E_c$, it adds this acquisition cost to the stored-energy ledger:

$$
\frac{\Delta E_c}{\eta_c}P_{EPEX}
$$

For battery-side discharged energy $\Delta E_d$, it realizes this saving:

$$
\Delta E_d\eta_dP_{EPEX}-\Delta E_dC_{stored}
$$

At first installation, the energy implied by current SOC and usable capacity is
entered with a cost of EUR 0. The current energy-counter readings become the
baseline, so previous charging and discharging never appears in `Total costs`
or `Total savings`. The ledger is saved across Home Assistant restarts.

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

- The optimizer uses a fixed household consumption profile; it does not yet
  learn from the configured grid-import energy counter.
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