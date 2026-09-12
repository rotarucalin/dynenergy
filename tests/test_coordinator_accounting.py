"""Exercise coordinator planning and accounting with Home Assistant stand-ins."""

import importlib.util
from datetime import UTC, datetime, timedelta
from math import isclose
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.dynenergy.accounting import BatteryCostAccount
from custom_components.dynenergy.optimizer import WeeklyConsumptionProfile


def _load_coordinator():
    """Import the real coordinator without requiring a Home Assistant install."""

    class CoordinatorBase:
        def __init__(self, hass, **kwargs):
            self.hass = hass
            self.data = None

        @classmethod
        def __class_getitem__(cls, item):
            return cls

        def async_set_updated_data(self, data):
            self.data = data

    def module(name, **attributes):
        result = ModuleType(name)
        result.__dict__.update(attributes)
        return result

    dt = SimpleNamespace(as_local=lambda value: value.astimezone(UTC), now=Mock())
    stubs = {
        name: module(name)
        for name in ("homeassistant", "homeassistant.helpers")
    }
    for name, attributes in {
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.const": {
            "ATTR_UNIT_OF_MEASUREMENT": "unit_of_measurement",
            "PERCENTAGE": "%",
            "UnitOfEnergy": SimpleNamespace(
                WATT_HOUR="Wh", KILO_WATT_HOUR="kWh", MEGA_WATT_HOUR="MWh"
            ),
            "UnitOfPower": SimpleNamespace(
                WATT="W", KILO_WATT="kW", MEGA_WATT="MW"
            ),
        },
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {"HomeAssistantError": RuntimeError},
        "homeassistant.helpers.event": {"async_track_time_change": Mock()},
        "homeassistant.helpers.storage": {"Store": Mock()},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": CoordinatorBase
        },
        "homeassistant.util": {"dt": dt},
    }.items():
        stubs[name] = module(name, **attributes)

    name = "custom_components.dynenergy._coordinator_accounting_test"
    path = Path(__file__).parents[1] / "custom_components/dynenergy/coordinator.py"
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**stubs, name: result}):
        spec.loader.exec_module(result)
    return result


coordinator_module = _load_coordinator()


class PowerRecommendationUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_limits_keep_kw_through_planning_and_helper_output(self):
        """Real HA readings, SOC prediction and helper writes share one unit path."""
        start = datetime(2026, 9, 14, 8, tzinfo=UTC)
        prices = [0.05, 0.40]
        states = {
            "sensor.price": SimpleNamespace(state="0.05", attributes={"data": [
                {
                    "start_time": (start + timedelta(minutes=15 * index)).isoformat(),
                    "end_time": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
                    "price_per_kwh": price,
                }
                for index, price in enumerate(prices)
            ]}),
        }
        config = {
            "price_entity": "sensor.price",
            "min_soc_percent": 10,
            "max_soc_percent": 100,
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.8,
            "charge_power_target_entity": "input_number.target",
        }
        for key, value, unit in [
            ("soc_entity", "50", "%"),
            ("capacity_entity", "2", "kWh"),
            ("max_charge_power_entity", "1500", "W"),
            ("max_discharge_power_entity", "1.5", "kW"),
        ]:
            config[key] = f"sensor.{key}"
            states[config[key]] = SimpleNamespace(
                state=value, attributes={"unit_of_measurement": unit}
            )
        hass = SimpleNamespace(
            states=SimpleNamespace(get=states.get),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        entry = SimpleNamespace(entry_id="test", data=config)
        coordinator = coordinator_module.DynEnergyCoordinator(hass, entry)
        data = coordinator._read_data()

        self.assertEqual(data.max_charge_power_kw, 1.5)
        self.assertEqual(data.max_discharge_power_kw, 1.5)
        plan = coordinator._create_charge_plan(data, start.date())
        self.assertAlmostEqual(plan.intervals[0].expected_soc_percent, 66.875)
        self.assertAlmostEqual(plan.intervals[1].expected_soc_percent, 43.4375)
        coordinator.data = coordinator._read_data(plan=plan)
        for index, expected_watts in enumerate([-1500, 1500]):
            await coordinator._async_apply_scheduled_power(
                start + timedelta(minutes=15 * index)
            )
            hass.services.async_call.assert_awaited_with(
                "input_number", "set_value",
                {"entity_id": "input_number.target", "value": expected_watts},
                blocking=True,
            )

        states[config["max_charge_power_entity"]].state = "unavailable"
        self.assertIsNone(coordinator._read_data().max_charge_power_kw)


class QuarterHourAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 11, 12, tzinfo=UTC)
        self.prices = []
        price_state = SimpleNamespace(attributes={"data": self.prices})
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: price_state))
        entry = SimpleNamespace(entry_id="test", data={"price_entity": "sensor.price"})
        self.coordinator = coordinator_module.DynEnergyCoordinator(hass, entry)
        self.coordinator._account_store = SimpleNamespace(
            async_load=AsyncMock(return_value=None), async_save=AsyncMock()
        )
        self.charged = 10.0
        self.discharged = 5.0
        self.soc = 50.0
        self.coordinator._read_data = self.read_data
        self.add_price(self.start, 0.05)
        self.add_price(self.start + timedelta(minutes=15), 0.50)
        self.add_price(self.start + timedelta(minutes=30), 0.50)

    def read_data(self, plan=None, planning_error=None, monitoring_error=None):
        return SimpleNamespace(
            battery_charged_energy_kwh=self.charged,
            battery_discharged_energy_kwh=self.discharged,
            current_soc_percent=self.soc,
            usable_capacity_kwh=10.0,
            plan=plan,
            planning_error=planning_error,
            monitoring_error=monitoring_error,
        )

    def add_price(self, start, price, minutes=15):
        self.prices.append({
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=minutes)).isoformat(),
            "price_per_kwh": price,
        })

    async def update(self, minutes):
        await self.coordinator._async_monitor_battery_energy(
            self.start + timedelta(minutes=minutes)
        )

    def assert_totals(self, cost, savings):
        account = self.coordinator._account
        self.assertTrue(isclose(account.total_charging_cost_eur, cost, abs_tol=1e-9))
        self.assertTrue(isclose(account.total_saved_cost_eur, savings, abs_tol=1e-9))

    async def test_completed_interval_price_and_unchanged_price(self):
        await self.update(0)
        self.charged += 1.0
        self.discharged += 0.5
        await self.update(15)
        self.assert_totals(0.05, 0.025)
        self.charged += 1.0
        self.discharged += 0.5
        await self.update(30)
        self.assert_totals(0.55, 0.275)
        self.charged += 1.0
        self.discharged += 0.5
        await self.update(45)
        self.assert_totals(1.05, 0.525)
        saved = self.coordinator._account_store.async_save.call_args.args[0]
        self.assertEqual(BatteryCostAccount.from_dict(saved), self.coordinator._account)

    async def test_midnight_uses_cached_price_after_old_data_disappears(self):
        self.start = datetime(2026, 9, 11, 23, 45, tzinfo=UTC)
        self.prices.clear()
        self.add_price(self.start, 0.05)
        await self.update(0)
        self.prices.clear()
        self.add_price(self.start + timedelta(minutes=15), 0.50)
        self.charged += 1.0
        self.discharged += 0.5
        await self.update(15)
        self.assert_totals(0.05, 0.025)

    async def assert_clock_transition(self, start_text, end_text):
        start = datetime.fromisoformat(start_text)
        end = datetime.fromisoformat(end_text)
        self.prices.clear()
        self.prices.append({
            "start_time": start_text,
            "end_time": end_text,
            "price_per_kwh": 0.05,
        })
        self.add_price(end, 0.50)
        await self.coordinator._async_monitor_battery_energy(start)
        self.charged += 1.0
        self.discharged += 0.5
        await self.coordinator._async_monitor_battery_energy(end)
        self.assert_totals(0.05, 0.025)
        self.assertIsNone(self.coordinator.data.monitoring_error)

    async def test_spring_clock_change_closes_one_real_quarter(self):
        await self.assert_clock_transition(
            "2026-03-29T01:45:00+01:00", "2026-03-29T03:00:00+02:00"
        )

    async def test_autumn_repeated_hour_closes_one_real_quarter(self):
        await self.assert_clock_transition(
            "2026-10-25T02:45:00+02:00", "2026-10-25T02:00:00+01:00"
        )

    async def test_startup_does_not_book_old_counters_or_require_soc(self):
        self.soc = None
        await self.update(7)
        self.assert_totals(0.0, 0.0)
        self.charged += 1.0
        self.discharged += 0.5
        await self.update(15)
        self.assert_totals(0.05, 0.025)

    async def test_restart_preserves_totals_and_rebaselines_downtime(self):
        old = BatteryCostAccount().initialize(0.0, 0.0, 0.0)
        old = old.record(1.0, 0.5, 0.20)
        self.coordinator._account = BatteryCostAccount.from_dict(old.as_dict())
        await self.update(7)
        self.assert_totals(0.20, 0.10)
        self.charged += 1.0
        await self.update(15)
        self.assert_totals(0.25, 0.10)

    async def test_counter_resets_and_negative_price(self):
        self.prices.clear()
        self.add_price(self.start, 0.05)
        self.add_price(self.start + timedelta(minutes=15), -0.10)
        await self.update(0)
        self.charged = 0.0
        self.discharged = 0.0
        await self.update(15)
        self.assert_totals(0.0, 0.0)
        self.charged = 1.0
        self.discharged = 0.5
        await self.update(30)
        self.assert_totals(-0.10, -0.05)

    async def test_hourly_price_is_used_for_each_quarter(self):
        self.prices.clear()
        self.add_price(self.start, 0.10, minutes=60)
        await self.update(0)
        for minute in (15, 30, 45, 60):
            self.charged += 1.0
            await self.update(minute)
        self.assert_totals(0.40, 0.0)

    async def test_duplicate_or_early_callbacks_do_not_advance_baseline(self):
        await self.update(0)
        self.charged += 0.5
        await self.update(1)
        self.assert_totals(0.0, 0.0)
        self.charged += 0.5
        await self.update(15)
        await self.update(15)
        self.assert_totals(0.05, 0.0)

    async def test_missed_boundary_is_not_priced_as_one_quarter(self):
        await self.update(0)
        self.charged += 2.0
        await self.update(30)
        self.assert_totals(0.0, 0.0)
        self.assertIn("Missed accounting boundary", self.coordinator.data.monitoring_error)
        self.charged += 1.0
        await self.update(45)
        self.assert_totals(0.50, 0.0)

    async def test_missing_meter_reading_requires_fresh_baseline(self):
        await self.update(0)
        self.charged = None
        await self.update(15)
        self.assertIn("Missing cumulative", self.coordinator.data.monitoring_error)
        self.charged = 12.0
        await self.update(30)
        self.assert_totals(0.0, 0.0)
        self.charged += 1.0
        await self.update(45)
        self.assert_totals(0.50, 0.0)

    async def test_price_can_arrive_before_interval_closes(self):
        self.prices.clear()
        await self.update(0)
        self.add_price(self.start, 0.05)
        self.charged += 1.0
        await self.update(15)
        self.assert_totals(0.05, 0.0)

    async def test_missing_price_does_not_reprice_delta_in_next_interval(self):
        self.prices.clear()
        await self.update(0)
        self.add_price(self.start + timedelta(minutes=15), 0.50)
        self.charged += 1.0
        await self.update(15)
        self.assert_totals(0.0, 0.0)
        self.assertIn("Unable to price completed", self.coordinator.data.monitoring_error)
        self.charged += 1.0
        await self.update(30)
        self.assert_totals(0.50, 0.0)

    async def test_zero_price_is_cached(self):
        self.prices.clear()
        self.add_price(self.start, 0.0)
        await self.update(0)
        self.prices.clear()
        self.charged += 1.0
        await self.update(15)
        self.assert_totals(0.0, 0.0)
        self.assertIsNone(self.coordinator.data.monitoring_error)
        self.assertEqual(self.coordinator._account.total_charged_kwh, 1.0)

    async def test_start_registers_only_daily_and_quarter_hour_callbacks(self):
        coordinator = self.coordinator
        coordinator._consumption_profile_store = SimpleNamespace(
            async_load=AsyncMock(return_value=WeeklyConsumptionProfile.default().as_dict())
        )
        coordinator._start_consumption_tracking = Mock()
        coordinator._async_restore_plan = AsyncMock()
        coordinator._async_apply_scheduled_power = AsyncMock()
        with (
            patch.object(coordinator_module.dt_util, "now", return_value=self.start),
            patch.object(coordinator_module, "async_track_time_change") as track,
        ):
            await coordinator.async_start()
        self.assertEqual(track.call_count, 2)
        quarter = track.call_args_list[1]
        self.assertEqual(quarter.args[1], coordinator._async_run_interval_tasks)
        self.assertEqual(list(quarter.kwargs["minute"]), [0, 15, 30, 45])
        self.assertEqual(quarter.kwargs["second"], 0)

    async def test_quarter_accounts_before_applying_new_target(self):
        coordinator = self.coordinator
        calls = []

        async def accounting(now):
            calls.append("accounting")

        async def learning(now):
            calls.append("learning")

        async def applying(now):
            calls.append("applying")

        coordinator._async_monitor_battery_energy = accounting
        coordinator._async_update_consumption_profile = learning
        coordinator._async_apply_scheduled_power = applying
        await coordinator._async_run_interval_tasks(self.start)
        self.assertEqual(calls, ["accounting", "learning", "applying"])


if __name__ == "__main__":
    unittest.main()
