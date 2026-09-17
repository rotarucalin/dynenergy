"""Exercise optimizer reloads through the real entry, coordinator and sensor code."""

import ast
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import custom_components.dynenergy as integration
from custom_components.dynenergy import optimizer
from test_coordinator_accounting import coordinator_module


def _load_sensor():
    """Cache the real sensor module before any optimizer reload takes place."""
    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        @classmethod
        def __class_getitem__(cls, item):
            return cls

    def module(name, **attributes):
        result = ModuleType(name)
        result.__dict__.update(attributes)
        return result

    stubs = {
        name: module(name, **attributes)
        for name, attributes in {
            "homeassistant.components.sensor": {
                "SensorEntity": type("SensorEntity", (), {}),
                "SensorDeviceClass": SimpleNamespace(POWER="power", MONETARY="monetary"),
                "SensorStateClass": SimpleNamespace(MEASUREMENT="measurement", TOTAL="total"),
            },
            "homeassistant.config_entries": {"ConfigEntry": object},
            "homeassistant.core": {"HomeAssistant": object},
            "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
            "homeassistant.helpers.update_coordinator": {"CoordinatorEntity": CoordinatorEntity},
            "homeassistant.util": {"dt": coordinator_module.dt_util},
            "homeassistant.const": {"UnitOfPower": SimpleNamespace(WATT="W")},
        }.items()
    }
    name = "custom_components.dynenergy._sensor_reload_test"
    path = Path(__file__).parents[1] / "custom_components/dynenergy/sensor.py"
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        **stubs, name: result,
        "custom_components.dynenergy.coordinator": coordinator_module,
    }):
        spec.loader.exec_module(result)
    return result


sensor_module = _load_sensor()


class OptimizerReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reload the one canonical optimizer module. Restore its namespace after
        # each test so existing domain tests can keep their imported test classes.
        self.enterContext(patch.dict(optimizer.__dict__))
        self.enterContext(patch.dict(sys.modules, {
            "custom_components.dynenergy.coordinator": coordinator_module,
        }))
        self.now = datetime(2026, 9, 14, 0, 2, tzinfo=UTC)
        start = self.now.replace(minute=0)
        self.states = {
            "sensor.price": SimpleNamespace(state="0.20", attributes={"data": [
                {
                    "start_time": (start + timedelta(minutes=15 * index)).isoformat(),
                    "end_time": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
                    "price_per_kwh": 0.20,
                } for index in range(3)
            ]}),
        }
        config = {
            "price_entity": "sensor.price",
            "min_soc_percent": 10, "max_soc_percent": 100,
            "charge_efficiency": 1.0, "discharge_efficiency": 1.0,
            "charge_power_target_entity": "input_number.target",
        }
        for key, value, unit in [
            ("soc_entity", "50", "%"), ("capacity_entity", "2", "kWh"),
            ("max_charge_power_entity", "1500", "W"),
            ("max_discharge_power_entity", "1500", "W"),
            ("battery_charged_energy_entity", "0", "kWh"),
            ("battery_discharged_energy_entity", "0", "kWh"),
        ]:
            config[key] = f"sensor.{key}"
            self.states[config[key]] = SimpleNamespace(
                state=value, attributes={"unit_of_measurement": unit}
            )
        self.entry = SimpleNamespace(entry_id="test", data=config)
        self.callbacks = {}
        self.entities = []
        self.events = []
        self.persisted = {}
        # One measured quarter hour in slot 1; the margin trims it to 0.123 kWh.
        self.history_samples = [
            (start + timedelta(minutes=15), 0.123 + optimizer.CONSUMPTION_MARGIN_KWH)
        ]

        async def executor(job, *args):
            return await asyncio.to_thread(job, *args)

        async def forward(entry, platforms):
            self.events.append("sensors")
            await sensor_module.async_setup_entry(self.hass, entry, self.entities.extend)

        async def unload(entry, platforms):
            self.events.append("unload_sensors")
            self.entities.clear()
            return True

        self.hass = SimpleNamespace(
            data={}, states=SimpleNamespace(get=self.states.get),
            services=SimpleNamespace(async_call=AsyncMock()),
            async_add_executor_job=AsyncMock(side_effect=executor),
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(side_effect=forward),
                async_unload_platforms=AsyncMock(side_effect=unload),
            ),
        )

        def track(hass, callback, **kwargs):
            token = object()
            self.callbacks[token] = (callback, kwargs)

            def cancel():
                self.events.append("cancel")
                self.callbacks.pop(token, None)

            return cancel

        def store(hass, version, key):
            return SimpleNamespace(
                async_load=AsyncMock(side_effect=lambda: deepcopy(self.persisted.get(key))),
                async_save=AsyncMock(side_effect=lambda data: self.persisted.update(
                    {key: deepcopy(data)}
                )),
            )

        self.enterContext(patch.object(coordinator_module, "async_track_time_change", track))
        self.enterContext(patch.object(coordinator_module, "Store", store))
        self.enterContext(patch.object(
            coordinator_module, "async_load_consumption_samples",
            AsyncMock(side_effect=lambda *args, **kwargs: list(self.history_samples)),
        ))
        self.enterContext(patch.object(coordinator_module.dt_util, "now", return_value=self.now))
        real_coordinator = coordinator_module.DynEnergyCoordinator

        def construct(hass, entry):
            self.events.append("coordinator")
            return real_coordinator(hass, entry)

        self.construct = self.enterContext(patch.object(
            coordinator_module, "DynEnergyCoordinator", side_effect=construct
        ))
        self.real_reload = importlib.reload
        self.loaded_idle_kwh = 0.060

        def reload_optimizer(module):
            self.assertIs(module, optimizer)
            self.assertEqual(self.callbacks, {})
            self.events.append("reload")
            reloaded = self.real_reload(module)
            # Model an edit loaded from disk, keeping fresh real dataclasses and
            # wrapping the newly defined functions to prove consumers call them.
            reloaded.IDLE_CONSUMPTION_KWH = self.loaded_idle_kwh
            reloaded.create_greedy_charge_plan = Mock(wraps=reloaded.create_greedy_charge_plan)
            reloaded.target_power_w = Mock(wraps=reloaded.target_power_w)
            return reloaded

        self.reload = self.enterContext(patch.object(
            integration.importlib, "reload", side_effect=reload_optimizer
        ))

    async def setup_entry(self):
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        return self.hass.data[integration.DOMAIN][self.entry.entry_id]

    async def unload_entry(self):
        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.assertEqual(self.callbacks, {})
        self.assertEqual(self.entities, [])
        self.assertEqual(self.hass.data[integration.DOMAIN], {})

    def test_runtime_imports_and_annotations_reference_the_module(self):
        directory = Path(__file__).parents[1] / "custom_components/dynenergy"
        for path in directory.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual((node.module or "").split(".")[-1], "optimizer", path)
        self.assertIs(coordinator_module.optimizer, optimizer)
        self.assertIs(sensor_module.optimizer, optimizer)
        self.assertEqual(
            coordinator_module.DynEnergyData.__annotations__["consumption_profile"],
            "optimizer.WeeklyConsumptionProfile",
        )
        self.assertEqual(
            sensor_module._interval_as_dict.__annotations__["interval"], "optimizer.PlanInterval"
        )

    async def test_initial_startup_reloads_before_construction_and_plans_immediately(self):
        old_profile_type = optimizer.WeeklyConsumptionProfile
        coordinator = await self.setup_entry()
        self.reload.assert_called_once_with(optimizer)
        self.hass.async_add_executor_job.assert_awaited_once_with(self.reload, optimizer)
        self.assertEqual(self.events, ["reload", "coordinator", "sensors"])
        self.assertIsNot(type(coordinator.data.consumption_profile), old_profile_type)
        self.assertIs(type(coordinator.data.consumption_profile), optimizer.WeeklyConsumptionProfile)
        self.assertIs(type(coordinator.data.plan), optimizer.OptimizationPlan)
        inputs = optimizer.create_greedy_charge_plan.call_args.args[0]
        self.assertIs(type(inputs), optimizer.OptimizerInputs)
        self.assertIs(type(inputs.battery), optimizer.BatteryParameters)
        self.assertEqual(len(self.callbacks), 2)
        self.assertEqual(len(self.entities), 6)
        optimizer.create_greedy_charge_plan.assert_called_once()
        self.hass.services.async_call.assert_awaited_with(
            "input_number", "set_value",
            {"entity_id": "input_number.target", "value": 240}, blocking=True,
        )
        await self.unload_entry()
        self.hass.services.async_call.assert_awaited_with(
            "input_number", "set_value",
            {"entity_id": "input_number.target", "value": 0}, blocking=True,
        )

    async def test_repeated_reloads_use_new_code_and_do_not_accumulate_callbacks(self):
        previous = None
        for idle_kwh, watts in [(0.060, 240), (0.080, 320), (0.090, 360)]:
            self.loaded_idle_kwh = idle_kwh
            coordinator = await self.setup_entry()
            self.assertEqual(len(self.hass.data[integration.DOMAIN]), 1)
            self.assertEqual(len(self.callbacks), 2)
            self.assertTrue(all(callback.__self__ is coordinator
                                for callback, _ in self.callbacks.values()))
            if previous is not None:
                self.assertIsNot(coordinator, previous)
                self.assertIsNot(type(coordinator.data.plan), type(previous.data.plan))
            self.assertEqual(coordinator.data.plan.intervals[0].consumption_kwh, idle_kwh)
            self.assertAlmostEqual(
                coordinator.data.consumption_profile.values_kwh[1], 0.123
            )
            self.assertEqual(coordinator.data.consumption_profile.sample_counts[1], 1)
            self.assertEqual(self.entities[1].native_value, watts)
            self.assertEqual(self.entities[0].extra_state_attributes["intervals"][0]
                             ["target_battery_power_w"], watts)
            optimizer.create_greedy_charge_plan.assert_called_once()
            self.hass.services.async_call.assert_awaited_with(
                "input_number", "set_value",
                {"entity_id": "input_number.target", "value": watts}, blocking=True,
            )
            queued = [callback for callback, _ in self.callbacks.values()]
            await self.unload_entry()
            calls_after_unload = self.hass.services.async_call.await_count
            for callback in queued:
                await callback(self.now)
            await coordinator._async_apply_scheduled_power(self.now)
            self.assertEqual(self.hass.services.async_call.await_count, calls_after_unload)
            previous = coordinator
        self.assertEqual(self.reload.call_count, 3)

    async def test_cached_sensor_and_coordinator_read_replaced_functions_and_constants(self):
        coordinator = await self.setup_entry()
        with patch.object(optimizer, "target_power_w", return_value=777):
            await coordinator._async_apply_scheduled_power(self.now)
            self.assertEqual(self.entities[1].native_value, 777)
            self.assertEqual(self.entities[1].extra_state_attributes["intervals"][0]
                             ["target_battery_power_w"], 777)
            self.hass.services.async_call.assert_awaited_with(
                "input_number", "set_value",
                {"entity_id": "input_number.target", "value": 777}, blocking=True,
            )
        with patch.object(optimizer, "INTERVAL_HOURS", 0.5):
            self.assertEqual(self.entities[2].native_value, 120)
            self.assertEqual(self.entities[2].extra_state_attributes["interval_minutes"], 30)
        await self.unload_entry()

    async def test_reloads_while_inputs_are_missing_clean_up_readiness_timers(self):
        self.states[self.entry.data["soc_entity"]].state = "unavailable"
        for _ in range(3):
            coordinator = await self.setup_entry()
            self.assertIsNone(coordinator.data.plan)
            self.assertEqual(len(self.callbacks), 3)
            await self.unload_entry()
        self.states[self.entry.data["soc_entity"]].state = "50"
        coordinator = await self.setup_entry()
        self.assertIsNotNone(coordinator.data.plan)
        self.assertEqual(len(self.callbacks), 2)
        await self.unload_entry()

    async def test_failed_platform_unload_retains_the_active_coordinator(self):
        coordinator = await self.setup_entry()
        self.hass.config_entries.async_unload_platforms.side_effect = None
        self.hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.assertIs(self.hass.data[integration.DOMAIN][self.entry.entry_id], coordinator)
        self.assertEqual(len(self.callbacks), 2)
        self.assertFalse(coordinator._shutting_down)
        self.reload.assert_called_once()

    async def test_invalid_optimizer_edit_fails_before_constructing_a_coordinator(self):
        await self.setup_entry()
        await self.unload_entry()
        self.reload.side_effect = SyntaxError("invalid optimizer edit")
        with self.assertRaises(SyntaxError):
            await integration.async_setup_entry(self.hass, self.entry)
        self.construct.assert_called_once()
        self.assertEqual(self.callbacks, {})
        self.assertEqual(self.hass.data[integration.DOMAIN], {})


if __name__ == "__main__":
    unittest.main()
