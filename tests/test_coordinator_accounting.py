"""Exercise coordinator planning and accounting with Home Assistant stand-ins."""

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from math import isclose
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.dynenergy.accounting import BatteryCostAccount
from custom_components.dynenergy.optimizer import (
    INTERVALS_PER_WEEK,
    OperatingState,
    WeeklyConsumptionProfile,
)


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

        async def async_config_entry_first_refresh(self):
            self.async_set_updated_data(await self._async_update_data())

        async def async_shutdown(self):
            pass

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
    async def test_idle_profile_reaches_optimizer_and_helper_on_startup(self):
        """Fresh and migrated idle defaults request +240 W; learned data survives."""
        start = datetime(2026, 9, 14, tzinfo=UTC)  # Monday's first idle slot.
        states = {
            "sensor.price": SimpleNamespace(state="0.20", attributes={"data": [{
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=15)).isoformat(),
                "price_per_kwh": 0.20,  # Discharge is permitted without a spike.
            }]}),
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
            ("max_discharge_power_entity", "1500", "W"),
            ("battery_charged_energy_entity", "0", "kWh"),
            ("battery_discharged_energy_entity", "0", "kWh"),
        ]:
            config[key] = f"sensor.{key}"
            states[config[key]] = SimpleNamespace(
                state=value, attributes={"unit_of_measurement": unit}
            )

        # Two Mondays of history for slot 0 average to 0.0275 kWh, which the
        # 0.0125 kWh margin trims to the 0.015 kWh / 60 W the learned case expects.
        history = [(start - timedelta(days=7), 0.03), (start, 0.025)]
        for source, samples, sample_count, expected_kwh, expected_watts in [
            ("no_history", [], 0, 0.060, 240),
            ("history", history, 2, 0.015, 60),
        ]:
            with self.subTest(source=source):
                hass = SimpleNamespace(
                    states=SimpleNamespace(get=states.get),
                    services=SimpleNamespace(async_call=AsyncMock()),
                )
                entry = SimpleNamespace(entry_id="test", data=config)
                coordinator = coordinator_module.DynEnergyCoordinator(hass, entry)
                coordinator._account_store = SimpleNamespace(
                    async_load=AsyncMock(return_value=None), async_save=AsyncMock()
                )
                with (
                    patch.object(coordinator_module.dt_util, "now", return_value=start),
                    patch.object(
                        coordinator_module,
                        "async_load_consumption_samples",
                        AsyncMock(return_value=samples),
                    ),
                ):
                    await coordinator.async_start()

                hass.services.async_call.assert_awaited_with(
                    "input_number", "set_value",
                    {"entity_id": "input_number.target", "value": expected_watts},
                    blocking=True,
                )
                self.assertIsNone(coordinator.data.planning_error)
                interval, = coordinator.data.plan.intervals
                self.assertEqual(interval.state, OperatingState.DISCHARGE)
                self.assertEqual(interval.consumption_kwh, expected_kwh)
                self.assertAlmostEqual(interval.target_battery_energy_kwh, expected_kwh)
                self.assertAlmostEqual(
                    interval.expected_soc_percent, (1.0 - expected_kwh / 0.8) / 2 * 100
                )
                self.assertEqual(
                    coordinator.data.consumption_profile.sample_counts[0], sample_count
                )

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


class StartupPlanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 14, tzinfo=UTC)
        self.now = self.start + timedelta(minutes=14)
        self.price_rows = [
            {
                "start_time": (self.start + offset).isoformat(),
                "end_time": (self.start + offset + timedelta(minutes=15)).isoformat(),
                "price_per_kwh": 0.20,
            }
            for offset in [
                timedelta(days=day, minutes=minute)
                for day in (0, 1) for minute in (0, 15, 30)
            ]
        ]
        self.states = {
            "sensor.price": SimpleNamespace(
                state="0.20", attributes={"data": self.price_rows}
            ),
        }
        self.config = {
            "price_entity": "sensor.price",
            "min_soc_percent": 10,
            "max_soc_percent": 100,
            "charge_efficiency": 1.0,
            "discharge_efficiency": 1.0,
            "charge_power_target_entity": "input_number.target",
        }
        for key, value, unit in [
            ("soc_entity", "50", "%"),
            ("capacity_entity", "2", "kWh"),
            ("max_charge_power_entity", "1500", "W"),
            ("max_discharge_power_entity", "1500", "W"),
            ("battery_charged_energy_entity", "0", "kWh"),
            ("battery_discharged_energy_entity", "0", "kWh"),
        ]:
            self.config[key] = f"sensor.{key}"
            self.states[self.config[key]] = SimpleNamespace(
                state=value, attributes={"unit_of_measurement": unit}
            )
        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=self.states.get),
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        entry = SimpleNamespace(entry_id="test", data=self.config)
        self.coordinator = coordinator_module.DynEnergyCoordinator(self.hass, entry)
        for name in ("_account_store", "_consumption_profile_store"):
            setattr(self.coordinator, name, SimpleNamespace(
                async_load=AsyncMock(return_value=None), async_save=AsyncMock()
            ))
        self.cancel_retry = Mock()
        self.track = self.enterContext(patch.object(
            coordinator_module, "async_track_time_change",
            side_effect=[Mock(), Mock(), self.cancel_retry],
        ))
        self.enterContext(patch.object(
            coordinator_module.dt_util, "now", side_effect=lambda: self.now
        ))
        self.create_plan = self.enterContext(patch.object(
            self.coordinator, "_create_charge_plan",
            wraps=self.coordinator._create_charge_plan,
        ))

    def set_state(self, key, value):
        self.states[self.config[key]].state = value

    async def start_waiting_for_soc(self):
        self.set_state("soc_entity", "unavailable")
        await self.coordinator.async_start()
        self.create_plan.assert_not_called()
        self.assertIsNone(self.coordinator.data.planning_error)
        self.assertEqual(self.track.call_count, 3)
        registration = self.track.call_args
        self.assertEqual(list(registration.kwargs["second"]), list(range(0, 60, 5)))
        return registration.args[1]

    async def test_delayed_soc_plans_once_and_immediately_applies_current_target(self):
        retry = await self.start_waiting_for_soc()
        for minutes in (1, 2, 3):
            self.now = self.start + timedelta(minutes=14 + minutes)
            await retry(self.now)
            self.create_plan.assert_not_called()
        self.assertEqual(self.track.call_count, 3)
        self.set_state("soc_entity", "50")
        await retry(self.now)

        self.create_plan.assert_called_once()
        self.cancel_retry.assert_called_once_with()
        self.assertIsNone(self.coordinator._unsub_startup_retry)
        self.assertEqual(
            self.coordinator.data.plan.intervals[0].timestamp,
            self.start + timedelta(minutes=15),
        )
        self.hass.services.async_call.assert_awaited_with(
            "input_number", "set_value",
            {"entity_id": "input_number.target", "value": 240}, blocking=True,
        )
        await retry(self.now)  # A previously queued callback must not plan again.
        self.create_plan.assert_called_once()

    async def test_waits_for_all_required_numeric_inputs(self):
        keys = ["soc_entity", "capacity_entity", "max_charge_power_entity",
                "max_discharge_power_entity"]
        values = [self.states[self.config[key]].state for key in keys]
        for key in keys:
            self.set_state(key, "unknown")
        retry = await self.start_waiting_for_soc()
        for key, value in zip(keys, values, strict=True):
            self.create_plan.assert_not_called()
            self.set_state(key, value)
            await retry(self.now)
        self.create_plan.assert_called_once()

    async def test_unknown_missing_and_nonfinite_soc_are_not_ready(self):
        retry = await self.start_waiting_for_soc()
        for value in ("unknown", "nan", "inf", "-inf", None):
            self.set_state("soc_entity", value)
            await retry(self.now)
            self.create_plan.assert_not_called()
        state = self.states.pop(self.config["soc_entity"])
        await retry(self.now)
        self.create_plan.assert_not_called()
        state.state = "50"
        self.states[self.config["soc_entity"]] = state
        await retry(self.now)
        self.create_plan.assert_called_once()

    async def test_waits_for_prices_for_the_requested_day(self):
        price_state = self.states["sensor.price"]
        price_state.attributes = {}
        await self.coordinator.async_start()
        retry = self.track.call_args.args[1]
        for rows in ([], self.price_rows[3:]):
            price_state.attributes = {"data": rows}
            await retry(self.now)
            self.create_plan.assert_not_called()
        price_state.attributes = {"data": self.price_rows}
        await retry(self.now)
        self.create_plan.assert_called_once()
        self.cancel_retry.assert_called_once_with()

    async def test_ready_inputs_plan_immediately_without_a_retry_timer(self):
        # Accounting meters are not required inputs to the optimizer.
        self.set_state("battery_charged_energy_entity", "unavailable")
        self.set_state("battery_discharged_energy_entity", "unavailable")
        await self.coordinator.async_start()
        self.create_plan.assert_called_once()
        self.assertEqual(self.track.call_count, 2)
        self.assertIsNone(self.coordinator._unsub_startup_retry)

    async def test_transient_refresh_failure_keeps_retrying_until_success(self):
        async def fail_refresh(domain, service, data, **kwargs):
            if domain == "homeassistant":
                raise coordinator_module.HomeAssistantError("EPEX is starting")

        self.hass.services.async_call.side_effect = fail_refresh
        await self.coordinator.async_start()
        self.create_plan.assert_not_called()
        self.assertIn("EPEX is starting", self.coordinator.data.planning_error)
        retry = self.track.call_args.args[1]
        self.hass.services.async_call.side_effect = None
        await retry(self.now)
        self.create_plan.assert_called_once()
        self.cancel_retry.assert_called_once_with()
        self.assertIsNone(self.coordinator.data.planning_error)

    async def test_retry_recomputes_the_target_day_after_midnight(self):
        self.now = self.start + timedelta(hours=23, minutes=59)
        retry = await self.start_waiting_for_soc()
        self.now = self.start + timedelta(days=1, minutes=16)
        self.set_state("soc_entity", "50")
        await retry(self.now)
        self.create_plan.assert_called_once()
        self.assertEqual(
            self.coordinator.data.plan.intervals[0].timestamp,
            self.start + timedelta(days=1, minutes=15),
        )

    async def test_shutdown_cancels_waiting_and_ignores_a_queued_retry(self):
        retry = await self.start_waiting_for_soc()
        await self.coordinator.async_shutdown()
        self.cancel_retry.assert_called_once_with()
        self.set_state("soc_entity", "50")
        await retry(self.now)
        self.create_plan.assert_not_called()

    async def test_overlapping_retry_and_shutdown_during_refresh_are_safe(self):
        retry = await self.start_waiting_for_soc()
        self.set_state("soc_entity", "50")
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        async def service_call(domain, service, data, **kwargs):
            if domain == "homeassistant":
                refresh_started.set()
                await release_refresh.wait()

        self.hass.services.async_call.side_effect = service_call
        task = asyncio.create_task(retry(self.now))
        try:
            await refresh_started.wait()
            await retry(self.now)
            self.assertEqual(self.hass.services.async_call.await_count, 2)
            await self.coordinator.async_shutdown()
        finally:
            release_refresh.set()
            await task
        self.create_plan.assert_not_called()
        self.cancel_retry.assert_called_once_with()
        self.assertIsNone(self.coordinator._unsub_startup_retry)
        self.assertIsNone(self.coordinator.data.plan)


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
        coordinator._async_refresh_consumption_profile = AsyncMock()
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

        async def applying(now):
            calls.append("applying")

        coordinator._async_monitor_battery_energy = accounting
        coordinator._async_apply_scheduled_power = applying
        await coordinator._async_run_interval_tasks(self.start)
        self.assertEqual(calls, ["accounting", "applying"])

    async def test_nightly_plan_refreshes_the_consumption_profile_afterwards(self):
        """Tomorrow's plan is built first, then today's actuals correct the profile."""
        coordinator = self.coordinator
        calls = []
        coordinator._async_refresh_plan = AsyncMock(
            side_effect=lambda *args, **kwargs: calls.append("plan")
        )
        coordinator._async_refresh_consumption_profile = AsyncMock(
            side_effect=lambda: calls.append("profile")
        )

        await coordinator._async_create_next_day_plan(self.start)

        self.assertEqual(calls, ["plan", "profile"])
        target_date, = coordinator._async_refresh_plan.await_args.args
        self.assertEqual(target_date, self.start.date() + timedelta(days=1))

    async def test_shutdown_during_nightly_planning_skips_the_profile_refresh(self):
        """A teardown mid-plan must not start another recorder read."""
        coordinator = self.coordinator

        async def refresh_then_shut_down(*args, **kwargs):
            coordinator._shutting_down = True

        coordinator._async_refresh_plan = AsyncMock(side_effect=refresh_then_shut_down)
        coordinator._async_refresh_consumption_profile = AsyncMock()

        await coordinator._async_create_next_day_plan(self.start)

        coordinator._async_refresh_consumption_profile.assert_not_awaited()

    async def test_profile_refresh_reads_the_configured_consumption_entity(self):
        """The rebuild passes the configured entity and the four-week cap through."""
        coordinator = self.coordinator
        samples = [(self.start, 0.5)]
        with patch.object(
            coordinator_module,
            "async_load_consumption_samples",
            AsyncMock(return_value=samples),
        ) as load:
            await coordinator._async_refresh_consumption_profile()

        _hass, entity_id, max_days = load.await_args.args
        self.assertEqual(entity_id, coordinator.entry.data.get("consumed_energy_entity"))
        self.assertEqual(max_days, coordinator_module.CONSUMPTION_HISTORY_DAYS)
        self.assertEqual(
            coordinator._consumption_profile,
            WeeklyConsumptionProfile.from_samples(samples),
        )


if __name__ == "__main__":
    unittest.main()
