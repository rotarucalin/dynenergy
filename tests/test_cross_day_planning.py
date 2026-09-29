"""Cross-day lifecycle tests using real coordinator, HA entities and service writes."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from custom_components.dynenergy import optimizer as opt
from custom_components.dynenergy import cycle_cancellation as cycles
from test_coordinator_accounting import coordinator_module as cm
from test_cycle_cancellation import slots
from test_reload import sensor_module


class _CrossDayFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 14, 15, 30, tzinfo=UTC)
        self.tomorrow = datetime(2026, 9, 15, tzinfo=UTC)
        self.config = {
            'price_entity': 'sensor.price', 'min_soc_percent': 0, 'max_soc_percent': 100,
            'charge_efficiency': 1, 'discharge_efficiency': 1, 'cycle_cancellation_debug': True,
            'charge_price_threshold': .3, 'charge_power_target_entity': 'input_number.target',
        }
        self.states = {'sensor.price': SimpleNamespace(state='.2', attributes={})}
        for key, value, unit in [('soc_entity', 50, '%'), ('capacity_entity', 10, 'kWh'),
                                 ('max_charge_power_entity', 4, 'kW'), ('max_discharge_power_entity', 4, 'kW'),
                                 ('battery_charged_energy_entity', 0, 'kWh'),
                                 ('battery_discharged_energy_entity', 0, 'kWh')]:
            self.config[key] = 'sensor.'+key
            self.states[self.config[key]] = SimpleNamespace(state=str(value), attributes={'unit_of_measurement': unit})
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get),
                                    services=SimpleNamespace(async_call=AsyncMock()))
        self.entry = SimpleNamespace(entry_id='crossday', data=self.config)
        self.coordinator = cm.DynEnergyCoordinator(self.hass, self.entry)
        self.saved = None

        async def save(value):
            self.saved = deepcopy(value)

        self.store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: deepcopy(self.saved)),
                                     async_save=AsyncMock(side_effect=save))
        self.coordinator._plan_store = self.store
        self.battery = self.coordinator._battery_parameters(self.coordinator._read_data())
        self.today = cycles.replay(slots([1]+[0]*33, [.2]+[.25]*33, start=self.now), self.battery, 50)
        self.coordinator.data = self.coordinator._read_data(self.today)
        self.next = cycles.replay(slots([-1, 1]+[0]*94, [.15, .4]+[.25]*94, start=self.tomorrow), self.battery, 40)
        self.set_prices()
        self.enterContext(patch.object(cm.dt_util, 'now', side_effect=lambda: self.now))
        self.coordinator._create_charge_plan = Mock(return_value=self.next)

    def set_prices(self, count=96):
        self.rows = [{'start_time': i.timestamp.isoformat(),
                      'end_time': (i.timestamp+timedelta(minutes=15)).isoformat(),
                      'price_per_kwh': i.price_per_kwh} for i in self.next.intervals[:count]]
        self.states['sensor.price'].attributes = {'data': self.rows}

    async def compare(self):
        await self.coordinator._async_compare(self.now)

class CrossDayTests(_CrossDayFixture):
    async def test_projection_atomic_publication_summaries_attributes_and_helper(self):
        original = self.coordinator.data.plan
        account = self.coordinator._account.as_dict()

        def build(data, day, *args):
            self.assertIs(self.coordinator.data.plan, original)
            self.assertEqual(data.current_soc_percent, 40)
            return self.next

        self.coordinator._create_charge_plan.side_effect = build
        await self.compare()
        c = self.coordinator
        self.assertEqual(c._revision, 1)
        self.assertEqual(c.data.comparison['cancelled_discharge_grid_kwh'], 1)
        self.assertEqual(c._account.as_dict(), account)
        self.assertEqual(c.data.plan.summary.total_charge_kwh, 0)
        self.assertEqual(c.data.plan.summary.total_discharge_kwh, 1)
        self.assertEqual(c.data.plan.intervals[0].expected_soc_percent, 50)
        plan_sensor = sensor_module.DynEnergyPlanSensor(c, self.entry)
        power_sensor = sensor_module.DynEnergyPowerRecommendationSensor(c, self.entry)
        attrs = plan_sensor.extra_state_attributes
        self.assertEqual(attrs['plan_revision'], 1)
        self.assertEqual(attrs['comparison_status'], 'adjusted')
        self.assertIn('comparison_debug', plan_sensor._unrecorded_attributes)
        self.assertEqual(attrs['intervals'][0]['original_target_power_w'], 4000)
        self.assertEqual(attrs['intervals'][0]['effective_target_power_w'], 0)
        self.assertAlmostEqual(attrs['predicted_cash_flow_difference_eur'], -.05)
        self.assertAlmostEqual(attrs['estimated_policy_benefit_eur'], .01)
        for at, expected in [(self.now, 0), (self.tomorrow, 0), (self.tomorrow+timedelta(minutes=15), 4000)]:
            self.now = at
            await c._async_apply_scheduled_power(at)
            self.assertEqual(power_sensor.native_value, expected)
            self.assertEqual(self.hass.services.async_call.call_args.args[2]['value'], expected)
        self.assertEqual(c._account.as_dict(), account)

    async def test_missing_partial_and_late_prices_preserve_schedule_then_retry(self):
        original = self.coordinator.data.plan
        for count in [0, 24, 95]:
            self.set_prices(count)
            await self.compare()
            self.assertIs(self.coordinator.data.plan, original)
            self.assertEqual(self.coordinator._revision, 0)
            self.assertEqual(self.coordinator.data.comparison['comparison_status'], 'retry_pending')
        self.set_prices()
        await self.compare()
        self.assertEqual(self.coordinator._revision, 1)
        self.assertIsNone(self.coordinator.data.planning_error)

    async def test_repeated_and_concurrent_callbacks_apply_once(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def service(*args, **kwargs):
            started.set()
            await release.wait()

        self.hass.services.async_call.side_effect = service
        first = asyncio.create_task(self.compare())
        await started.wait()
        second = asyncio.create_task(self.compare())
        self.assertIs(self.coordinator.data.plan, self.today)
        release.set()
        await asyncio.gather(first, second)
        await self.compare()
        self.assertEqual(self.coordinator._revision, 1)
        self.assertEqual(self.store.async_save.await_count, 1)
        self.assertEqual(self.coordinator._create_charge_plan.call_count, 1)

    async def test_failed_planning_and_storage_preserve_active_plan(self):
        self.coordinator._create_charge_plan.side_effect = ValueError('failed candidate')
        await self.compare()
        self.assertIs(self.coordinator.data.plan, self.today)
        self.coordinator._create_charge_plan.side_effect = None
        self.store.async_save.side_effect = OSError('disk full')
        await self.compare()
        self.assertIs(self.coordinator.data.plan, self.today)
        self.assertEqual(self.coordinator._revision, 0)

    async def test_shutdown_during_persistence_does_not_publish(self):
        async def stop(value):
            self.coordinator._shutting_down = True
        self.store.async_save.side_effect = stop
        await self.compare()
        self.assertIs(self.coordinator.data.plan, self.today)
        self.assertEqual(self.coordinator._revision, 0)

    async def test_restart_without_yesterdays_prices_and_expiry(self):
        await self.compare()
        restored = cm.DynEnergyCoordinator(self.hass, self.entry)
        restored._plan_store = self.store
        self.now = self.tomorrow
        self.assertTrue(await restored._async_load_saved_plan(self.now))
        self.assertEqual(restored.data.plan.intervals[0].timestamp, self.tomorrow)
        self.assertEqual(restored.data.plan.summary.total_charge_kwh, 0)
        self.assertEqual(restored._revision, 1)
        await restored._async_compare(self.tomorrow-timedelta(minutes=15))
        self.assertEqual(restored._revision, 1)
        self.assertFalse(await restored._async_load_saved_plan(self.tomorrow+timedelta(days=1)))

    async def test_restart_waits_for_measurements_without_losing_saved_decision(self):
        await self.compare()
        restored = cm.DynEnergyCoordinator(self.hass, self.entry)
        restored._plan_store = self.store
        self.states['sensor.soc_entity'].state = 'unavailable'
        self.assertFalse(await restored._async_load_saved_plan(self.now))
        self.states['sensor.soc_entity'].state = '50'
        await restored._async_restore_plan()
        self.assertEqual(restored._revision, 1)
        self.assertEqual(restored.data.plan.summary.total_charge_kwh, 0)

    async def test_2350_and_midnight_keep_adjustment(self):
        await self.compare()
        c = self.coordinator
        c._async_refresh_consumption_profile = AsyncMock()
        self.now = self.tomorrow-timedelta(minutes=10)
        await c._async_create_next_day_plan(self.now)
        self.assertEqual(c._revision, 1)
        self.now = self.tomorrow
        await c._async_run_interval_tasks(self.now)
        self.assertEqual(c._revision, 1)
        self.assertEqual(c.data.plan.intervals[0].timestamp, self.tomorrow)
        self.assertEqual(c.data.plan.intervals[0].target_battery_energy_kwh, 0)
        self.assertEqual(c.data.plan.summary.total_charge_kwh, 0)

    async def test_soc_drift_rebuilds_from_prices_not_reduced_allocations(self):
        await self.compare()
        c = self.coordinator
        self.now += timedelta(minutes=15)
        self.states['sensor.soc_entity'].state = '60'
        await c._async_maintain_plan(self.now)
        self.assertEqual(c._revision, 2)
        self.assertEqual(c.data.comparison['rebuild_reason'], 'soc_drift')
        self.assertEqual(c.data.plan.intervals[0].timestamp, self.now)
        first_revision = c.data.plan
        await c._async_maintain_plan(self.now)
        self.assertEqual(c._revision, 2)
        self.assertIs(c.data.plan, first_revision)

    async def test_soc_drift_tolerance(self):
        await self.compare()
        self.states['sensor.soc_entity'].state = '55'
        await self.coordinator._async_maintain_plan(self.now)
        self.assertEqual(self.coordinator._revision, 1)

    async def test_prices_reject_duplicates_nonfinite_naive_and_misalignment(self):
        for mutation in ['duplicate', 'nan', 'naive', 'gap', 'misaligned']:
            self.set_prices()
            if mutation == 'duplicate':
                self.rows.append(self.rows[0])
            elif mutation == 'nan':
                self.rows[0]['price_per_kwh'] = float('nan')
            elif mutation == 'naive':
                self.rows[0]['start_time'] = self.tomorrow.replace(tzinfo=None).isoformat()
            elif mutation == 'gap':
                self.rows.pop(4)
            else:
                self.rows[0]['start_time'] = (self.tomorrow+timedelta(seconds=1)).isoformat()
                self.rows[0]['end_time'] = (self.tomorrow+timedelta(minutes=15, seconds=1)).isoformat()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.coordinator._target_day_prices(self.tomorrow.date(), require_complete=True)

    async def test_dst_complete_days_and_repeated_hour_execution(self):
        zone = ZoneInfo('Europe/Berlin')
        with patch.object(cm.dt_util, 'as_local', side_effect=lambda v: v.astimezone(zone)):
            for month, day, hours in [(3, 29, 23), (10, 25, 25)]:
                start = datetime(2026, month, day, tzinfo=zone).astimezone(UTC)
                rows = []
                for n in range(hours):
                    a = start+timedelta(hours=n)
                    rows.append({'start_time': a.isoformat(), 'end_time': (a+timedelta(hours=1)).isoformat(),
                                 'price_per_kwh': .15+n*.001})
                self.states['sensor.price'].attributes = {'data': rows}
                stamps, prices = self.coordinator._target_day_prices(start.astimezone(zone).date(), require_complete=True)
                self.assertEqual(len(stamps), hours*4)
                self.assertEqual(len(set(t.timestamp() for t in stamps)), hours*4)
                self.assertTrue(all(b.timestamp()-a.timestamp() == 900 for a,b in zip(stamps, stamps[1:])))
                if hours == 25:
                    repeated = [t for t in stamps if t.hour == 2 and t.minute == 0]
                    self.assertEqual(len(repeated), 2)
                    intervals = [opt.PlanInterval(t, .2, 1, .1*(n+1), 50, opt.OperatingState.DISCHARGE)
                                 for n,t in enumerate(repeated)]
                    self.coordinator.data = self.coordinator._read_data(cycles.summarize(intervals, self.battery))
                    for n,t in enumerate(repeated):
                        await self.coordinator._async_apply_scheduled_power(t)
                        self.assertEqual(self.hass.services.async_call.call_args.args[2]['value'], 400*(n+1))

class PersistenceAuditTests(_CrossDayFixture):
    async def test_corrupt_saved_forecast_or_ledger_is_rejected(self):
        await self.compare()
        good = deepcopy(self.saved)
        for field in ['forecast', 'ledger', 'timestamp', 'power']:
            self.saved = deepcopy(good)
            if field == 'forecast':
                self.saved['adjusted'][0]['expected_soc_percent'] = 75
            elif field == 'ledger':
                self.saved['matches'] = []
            elif field == 'timestamp':
                self.saved['adjusted'][1]['timestamp'] = self.saved['adjusted'][0]['timestamp']
            else:
                self.saved['adjusted'][0]['target_battery_energy_kwh'] = 100
            restored = cm.DynEnergyCoordinator(self.hass, self.entry)
            restored._plan_store = self.store
            with self.subTest(field=field):
                self.assertFalse(await restored._async_load_saved_plan(self.now))

    async def test_debug_disabled_still_persists_complete_ledger(self):
        self.config['cycle_cancellation_debug'] = False
        await self.compare()
        self.assertEqual(self.saved['debug'], {})
        self.assertEqual(len(self.saved['matches']), 1)
        self.assertEqual(self.saved['matches'][0]['matched_battery_kwh'], 1)

    async def test_quarter_boundary_retries_after_prices_arrive(self):
        # First quarter fails; next quarter has a remaining early discharge.
        energies = [0, 1] + [0]*32
        self.coordinator.data = self.coordinator._read_data(cycles.replay(
            slots(energies, [.2]*34, start=self.now), self.battery, 50))
        self.set_prices(50)
        await self.coordinator._async_run_interval_tasks(self.now)
        self.assertEqual(self.coordinator._revision, 0)
        self.now += timedelta(minutes=15)
        self.set_prices()
        await self.coordinator._async_run_interval_tasks(self.now)
        self.assertEqual(self.coordinator._revision, 1)
        self.assertEqual(self.coordinator.data.comparison['cancelled_discharge_grid_kwh'], 1)

    async def test_helper_waits_for_atomic_publication(self):
        started, release = asyncio.Event(), asyncio.Event()
        saved = self.store.async_save.side_effect

        async def pause(value):
            started.set()
            await release.wait()
            await saved(value)

        self.store.async_save.side_effect = pause
        comparison = asyncio.create_task(self.compare())
        await started.wait()
        applying = asyncio.create_task(self.coordinator._async_apply_scheduled_power(self.now))
        await asyncio.sleep(0)
        self.assertFalse(applying.done())
        release.set()
        await asyncio.gather(comparison, applying)
        self.assertEqual(self.hass.services.async_call.call_args.args[2]['value'], 0)

    async def test_daily_failed_greedy_refresh_preserves_existing_adjusted_plan(self):
        await self.compare()
        original = self.coordinator.data.plan
        self.coordinator._create_charge_plan.side_effect = ValueError('bad input')
        await self.coordinator._async_refresh_plan(self.now.date(), self.now)
        self.assertIs(self.coordinator.data.plan, original)

    async def test_mid_slot_restart_and_2350_never_overwrite_adjusted_plan(self):
        await self.compare()
        self.now += timedelta(minutes=7)
        restored = cm.DynEnergyCoordinator(self.hass, self.entry)
        restored._plan_store = self.store
        self.assertTrue(await restored._async_load_saved_plan(self.now))
        self.states['sensor.soc_entity'].state = '65'
        await restored._async_maintain_plan(self.now)
        self.assertEqual(restored._revision, 1)
        self.now = self.now.replace(minute=45)
        await restored._async_maintain_plan(self.now)
        self.assertEqual(restored._revision, 2)


    async def test_boundary_dispatch_with_fractional_seconds_still_compares(self):
        self.now = self.now.replace(second=1, microsecond=500000)
        await self.coordinator._async_run_interval_tasks(self.now)
        self.assertEqual(self.coordinator._revision, 1)
        self.assertEqual(self.coordinator.data.comparison['cancelled_discharge_grid_kwh'], 1)

    async def test_late_current_day_prices_repair_partial_startup_baseline(self):
        partial = cycles.summarize(self.today.intervals[:3], self.battery)
        self.coordinator.data = self.coordinator._read_data(partial)

        def complete(data, day, *args):
            self.assertIs(self.coordinator.data.plan, partial)
            return self.today if day == self.now.date() else self.next

        self.coordinator._create_charge_plan.side_effect = complete
        await self.compare()
        self.assertEqual(self.coordinator._revision, 1)
        self.assertEqual(len(self.coordinator.data.plan.intervals), 130)
