"""Regression tests for the recorder-backed consumption history loader."""

import importlib.util
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

NOW = datetime(2026, 9, 14, 10, 7, 30, tzinfo=UTC)
# The loader drops the partial quarter hour in progress, so 10:07:30 ends at 10:00.
WINDOW_END = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def _module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def _load_history():
    """Import the real history module without requiring a Home Assistant install."""
    dt = SimpleNamespace(
        now=Mock(return_value=NOW),
        as_local=lambda value: value.astimezone(UTC),
        utc_from_timestamp=lambda value: datetime.fromtimestamp(value, UTC),
    )
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.util": _module("homeassistant.util", dt=dt),
    }
    name = "custom_components.dynenergy._history_test"
    path = Path(__file__).parents[1] / "custom_components/dynenergy/history.py"
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**stubs, name: result}):
        spec.loader.exec_module(result)
    return result


history_module = _load_history()


def _rows(start, changes):
    """Build recorder changes for consecutive five-minute intervals."""
    return [
        {"start": (start + timedelta(minutes=5 * index)).timestamp(), "change": change}
        for index, change in enumerate(changes)
    ]


def _meter_history(start, consumption, charged, discharged):
    return {
        "sensor.consumed": _rows(start, consumption),
        "sensor.charged": _rows(start, charged),
        "sensor.discharged": _rows(start, discharged),
    }


def _recorder_stubs(statistics_during_period, keep_days=10):
    """Return sys.modules entries exposing a running recorder."""
    instance = SimpleNamespace(
        keep_days=keep_days,
        async_add_executor_job=AsyncMock(side_effect=lambda job, *args: job(*args)),
    )
    return instance, {
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.recorder": _module(
            "homeassistant.components.recorder", get_instance=lambda hass: instance
        ),
        "homeassistant.components.recorder.statistics": _module(
            "homeassistant.components.recorder.statistics",
            statistics_during_period=statistics_during_period,
        ),
    }


class ConsumptionHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = SimpleNamespace()
        self.enterContext(
            patch.object(history_module.dt_util, "now", return_value=NOW)
        )

    async def _load(
        self, statistics, keep_days=10, max_days=28,
        charge_efficiency=1.0, discharge_efficiency=1.0,
    ):
        instance, stubs = _recorder_stubs(statistics, keep_days)
        with patch.dict(sys.modules, stubs):
            samples = await history_module.async_load_consumption_samples(
                self.hass, "sensor.consumed", max_days,
                charged_entity_id="sensor.charged",
                discharged_entity_id="sensor.discharged",
                charge_efficiency=charge_efficiency,
                discharge_efficiency=discharge_efficiency,
            )
        return samples, instance

    async def test_missing_entity_returns_no_samples(self):
        """An unconfigured entity leaves the caller with the default profile."""
        for missing in range(3):
            entities = ["sensor.consumed", "sensor.charged", "sensor.discharged"]
            entities[missing] = None
            with self.subTest(missing=missing):
                self.assertEqual(
                    await history_module.async_load_consumption_samples(
                        self.hass, entities[0], 28,
                        charged_entity_id=entities[1], discharged_entity_id=entities[2],
                        charge_efficiency=1.0, discharge_efficiency=1.0,
                    ),
                    [],
                )

    async def test_absent_recorder_returns_no_samples(self):
        """Recorder is a soft dependency; without it the loader stays quiet."""
        # No recorder modules in sys.modules, so the lazy import raises ImportError.
        for name in list(sys.modules):
            if name.startswith("homeassistant.components.recorder"):
                del sys.modules[name]
        self.assertEqual(
            await history_module.async_load_consumption_samples(
                self.hass, "sensor.consumed", 28,
                charged_entity_id="sensor.charged",
                discharged_entity_id="sensor.discharged",
                charge_efficiency=1.0, discharge_efficiency=1.0,
            ),
            [],
        )

    async def test_statistics_failure_returns_no_samples(self):
        """A recorder error must never propagate into planning."""
        def explode(*args):
            raise RuntimeError("database is locked")

        samples, _ = await self._load(explode)
        self.assertEqual(samples, [])

    async def test_entity_without_statistics_returns_no_samples(self):
        """A sensor recorder does not track statistics for yields nothing."""
        samples, _ = await self._load(Mock(return_value={}))
        self.assertEqual(samples, [])

    async def test_five_minute_rows_collapse_into_quarter_hour_totals(self):
        """Three five-minute rows sum into the quarter hour that contains them."""
        base = WINDOW_END - timedelta(hours=1)
        statistics = _meter_history(
            base, [0.10, 0.20, 0.30, 0.05, 0.05, 0.05], [0.0] * 6, [0.0] * 6
        )
        samples, _ = await self._load(Mock(return_value=statistics))

        self.assertEqual(len(samples), 2)
        first, second = samples
        self.assertEqual(first[0], base)
        self.assertAlmostEqual(first[1], 0.60)
        self.assertEqual(second[0], base + timedelta(minutes=15))
        self.assertAlmostEqual(second[1], 0.15)

    async def test_rows_without_a_change_value_are_skipped(self):
        """Incomplete statistic rows are ignored rather than counted as zero."""
        base = WINDOW_END - timedelta(hours=1)
        statistics = _meter_history(base, [None, None, 0.4], [0.0] * 3, [0.0] * 3)
        del statistics["sensor.consumed"][1]["change"]
        samples, _ = await self._load(Mock(return_value=statistics))

        self.assertEqual(samples, [])

    async def test_charging_is_subtracted_and_discharging_is_added_with_losses(self):
        """Convert both battery-side counters to the metered side before correcting."""
        base = WINDOW_END - timedelta(hours=1)
        for consumed, charged, discharged, expected in [
            (0.4, 0.27, 0.0, 0.1),   # 0.4 - 0.27 / 0.9
            (0.0, 0.0, 0.3, 0.24),   # Battery fully covers the real load.
            (0.22, 0.18, 0.1, 0.1),  # Both directions in the same quarter.
            (0.3, 0.27, 0.0, 0.0),   # Pure charging is not household demand.
            (0.01, 0.09, 0.0, 0.0),  # Inconsistent readings cannot create negatives.
        ]:
            with self.subTest(consumed=consumed, charged=charged, discharged=discharged):
                statistics = _meter_history(
                    base, [consumed, 0, 0], [charged, 0, 0], [discharged, 0, 0]
                )
                samples, _ = await self._load(
                    Mock(return_value=statistics),
                    charge_efficiency=0.9, discharge_efficiency=0.8,
                )
                self.assertEqual(len(samples), 1)
                self.assertEqual(samples[0][0], base)
                self.assertAlmostEqual(samples[0][1], expected)

    async def test_missing_or_partial_battery_history_does_not_teach_zero_load(self):
        """Unknown battery flow skips the whole quarter, preserving its default."""
        base = WINDOW_END - timedelta(hours=1)
        for entity in ("sensor.consumed", "sensor.charged", "sensor.discharged"):
            for row_count in (0, 1, 2):
                with self.subTest(entity=entity, row_count=row_count):
                    statistics = _meter_history(base, [0.1] * 3, [0.0] * 3, [0.0] * 3)
                    statistics[entity] = statistics[entity][:row_count]
                    samples, _ = await self._load(Mock(return_value=statistics))
                    self.assertEqual(samples, [])
                    self.assertEqual(
                        history_module.optimizer.WeeklyConsumptionProfile.from_samples(samples),
                        history_module.optimizer.WeeklyConsumptionProfile.default(),
                    )

    async def test_meter_histories_are_matched_by_time_not_list_position(self):
        base = WINDOW_END - timedelta(hours=1)
        statistics = _meter_history(
            base, [0.1] * 6, [0.0] * 3 + [0.05] * 3, [0.02] * 6
        )
        statistics["sensor.charged"] = list(reversed(statistics["sensor.charged"][3:]))
        samples, _ = await self._load(Mock(return_value=statistics))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][0], base + timedelta(minutes=15))
        self.assertAlmostEqual(samples[0][1], 0.21)

    async def test_invalid_changes_skip_the_quarter(self):
        base = WINDOW_END - timedelta(hours=1)
        for entity in ("sensor.consumed", "sensor.charged", "sensor.discharged"):
            for change in (None, -0.1, float("nan"), float("inf"), "unavailable"):
                with self.subTest(entity=entity, change=change):
                    statistics = _meter_history(base, [0.1] * 3, [0.0] * 3, [0.0] * 3)
                    statistics[entity][1]["change"] = change
                    samples, _ = await self._load(Mock(return_value=statistics))
                    self.assertEqual(samples, [])

    async def test_duplicate_readings_do_not_count_as_a_complete_quarter(self):
        base = WINDOW_END - timedelta(hours=1)
        statistics = _meter_history(base, [0.1] * 3, [0.0] * 3, [0.0] * 3)
        statistics["sensor.consumed"][2] = dict(statistics["sensor.consumed"][1])
        samples, _ = await self._load(Mock(return_value=statistics))
        self.assertEqual(samples, [])

    async def test_partial_current_quarter_is_excluded_even_if_returned_by_recorder(self):
        statistics = _meter_history(WINDOW_END, [0.1] * 3, [0.0] * 3, [0.0] * 3)
        samples, _ = await self._load(Mock(return_value=statistics))
        self.assertEqual(samples, [])

    async def test_invalid_efficiency_does_not_read_history(self):
        for efficiency in (0.0, -1.0, 1.1, float("nan"), float("inf")):
            for direction in ("charge_efficiency", "discharge_efficiency"):
                with self.subTest(efficiency=efficiency, direction=direction):
                    statistics = Mock(return_value={})
                    samples, _ = await self._load(statistics, **{direction: efficiency})
                    self.assertEqual(samples, [])
                    statistics.assert_not_called()

    async def test_battery_covered_peak_is_still_planned_the_following_week(self):
        """Zero imports at 19:45 retain demand supplied by the battery."""
        optimizer = history_module.optimizer
        previous_peak = datetime(2026, 9, 10, 19, 45, tzinfo=UTC)
        statistics = _meter_history(
            previous_peak, [0.0] * 3, [0.0] * 3, [0.025] * 3
        )
        samples, _ = await self._load(Mock(return_value=statistics), discharge_efficiency=0.8)
        profile = optimizer.WeeklyConsumptionProfile.from_samples(samples)
        next_peak = previous_peak + timedelta(days=7)
        # 0.075 kWh from the battery delivers 0.060 kWh; trim the margin once.
        expected_kwh = 0.060 - optimizer.CONSUMPTION_MARGIN_KWH
        self.assertAlmostEqual(profile.consumption_kwh(next_peak), expected_kwh)
        timestamps = [next_peak - timedelta(minutes=15), next_peak, next_peak + timedelta(minutes=15)]
        plan = optimizer.create_greedy_charge_plan(
            optimizer.OptimizerInputs(
                timestamps=timestamps,
                prices_per_kwh=[0.20, 0.23, 0.20],
                consumption_kwh=[profile.consumption_kwh(timestamp) for timestamp in timestamps],
                current_soc_percent=13,
                battery=optimizer.BatteryParameters(
                    usable_capacity_kwh=2.0,
                    max_charge_power_kw=1.5, max_discharge_power_kw=1.5,
                    min_soc_percent=10, max_soc_percent=100,
                    charge_efficiency=0.9, discharge_efficiency=0.8,
                ),
            ),
            0.10,
        )
        peak = plan.intervals[1]
        self.assertIs(peak.state, optimizer.OperatingState.DISCHARGE)
        self.assertAlmostEqual(peak.target_battery_energy_kwh, expected_kwh)
        self.assertAlmostEqual(peak.target_battery_energy_kwh / optimizer.INTERVAL_HOURS * 1000, 190)

    async def test_window_is_clamped_to_the_shorter_of_retention_and_maximum(self):
        """Recorder retention caps the window; four weeks caps recorder retention."""
        for keep_days, max_days, expected_days in [
            (10, 28, 10),  # Default HA retention is shorter than four weeks.
            (90, 28, 28),  # Generous retention still averages only four weeks.
        ]:
            with self.subTest(keep_days=keep_days):
                statistics = Mock(return_value={"sensor.consumed": []})
                await self._load(statistics, keep_days=keep_days, max_days=max_days)

                _hass, start, end, ids, period, units, types = statistics.call_args.args
                self.assertEqual(end, WINDOW_END)
                self.assertEqual(start, WINDOW_END - timedelta(days=expected_days))
                self.assertEqual(ids, {"sensor.consumed", "sensor.charged", "sensor.discharged"})
                self.assertEqual(period, "5minute")
                self.assertEqual(units, {"energy": "kWh"})
                self.assertEqual(types, {"change"})

    async def test_statistics_run_on_the_recorder_executor(self):
        """The database read must never happen on the event loop."""
        statistics = Mock(return_value={"sensor.consumed": []})
        _samples, instance = await self._load(statistics)

        instance.async_add_executor_job.assert_awaited_once()
        self.assertIs(instance.async_add_executor_job.await_args.args[0], statistics)


if __name__ == "__main__":
    unittest.main()
