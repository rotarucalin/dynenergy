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

    async def _load(self, statistics, keep_days=10, max_days=28):
        instance, stubs = _recorder_stubs(statistics, keep_days)
        with patch.dict(sys.modules, stubs):
            samples = await history_module.async_load_consumption_samples(
                self.hass, "sensor.consumed", max_days
            )
        return samples, instance

    async def test_missing_entity_returns_no_samples(self):
        """An unconfigured entity leaves the caller with the default profile."""
        for entity_id in (None, ""):
            with self.subTest(entity_id=entity_id):
                self.assertEqual(
                    await history_module.async_load_consumption_samples(
                        self.hass, entity_id, 28
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
                self.hass, "sensor.consumed", 28
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
        rows = [
            {"start": (base + timedelta(minutes=minutes)).timestamp(), "change": change}
            for minutes, change in [
                (0, 0.10), (5, 0.20), (10, 0.30),  # -> one 0.60 kWh quarter
                (15, 0.05), (20, 0.05),            # -> one 0.10 kWh quarter
            ]
        ]
        samples, _ = await self._load(Mock(return_value={"sensor.consumed": rows}))

        self.assertEqual(len(samples), 2)
        first, second = samples
        self.assertEqual(first[0], base)
        self.assertAlmostEqual(first[1], 0.60)
        self.assertEqual(second[0], base + timedelta(minutes=15))
        self.assertAlmostEqual(second[1], 0.10)

    async def test_rows_without_a_change_value_are_skipped(self):
        """Incomplete statistic rows are ignored rather than counted as zero."""
        base = WINDOW_END - timedelta(hours=1)
        rows = [
            {"start": base.timestamp(), "change": None},
            {"start": (base + timedelta(minutes=5)).timestamp()},
            {"start": (base + timedelta(minutes=10)).timestamp(), "change": 0.4},
        ]
        samples, _ = await self._load(Mock(return_value={"sensor.consumed": rows}))

        self.assertEqual(samples, [(base, 0.4)])

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
                self.assertEqual(ids, {"sensor.consumed"})
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
