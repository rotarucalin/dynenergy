"""Dated cross-day plan lifecycle, separate from measured energy accounting."""
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import optimizer
from . import cycle_cancellation as cycles
from .const import (
    CONF_CHARGE_PRICE_THRESHOLD, CHARGE_PRICE_THRESHOLD_PER_KWH,
    CONF_CYCLE_COST, CONF_CYCLE_DEBUG, CONF_PRICE_ENTITY,
)

LOGGER = logging.getLogger(__name__)
SOC_DRIFT_PERCENTAGE_POINTS = 5.0


class CrossDayPlanning:
    """Coordinator methods using one immutable baseline per published revision."""

    def _publish_error(self, plan, err):
        LOGGER.warning("Unable to create DynEnergy plan: %s", err)
        self.async_set_updated_data(self._read_data(
            plan, str(err), self.data.monitoring_error if self.data else None,
        ))

    @staticmethod
    def _remaining(plan, now):
        return [i for i in plan.intervals if i.timestamp.timestamp() + 900 > now.timestamp()]

    @staticmethod
    def _predicted_soc(plan, now, battery):
        """Interpolate the active slot for restart and drift checks."""
        for i in plan.intervals:
            elapsed = (now.timestamp() - i.timestamp.timestamp()) / 900
            if 0 <= elapsed < 1:
                allocation = i.allocated_grid_energy_kwh
                end_soc = i.expected_soc_percent
                if i.command_grid_energy_kwh is not None:
                    allocation = i.command_grid_energy_kwh
                    end_soc = i.command_expected_soc_percent
                if allocation is None:
                    allocation = i.target_battery_energy_kwh
                delta = (-allocation * battery.charge_efficiency if allocation < 0
                         else -allocation / battery.discharge_efficiency)
                return end_soc - delta * (1 - elapsed) / battery.usable_capacity_kwh * 100
        return None

    async def _async_maintain_plan(self, now):
        """Quarter-hour retries, expiry and measurement-driven rebuilding."""
        if self._shutting_down or not self.data or not self.data.plan:
            return
        local = dt_util.as_local(now)
        try:
            data = self._read_data()
            self._validate_plan_inputs(data)
            battery = self._battery_parameters(data)
            expected = self._predicted_soc(self.data.plan, now, battery)
            drift = expected is not None and abs(expected - data.current_soc_percent) > SOC_DRIFT_PERCENTAGE_POINTS
            boundary = now.minute % 15 == 0 and now.second == 0
            if drift and boundary:
                await self._async_compare(now, rebuild=True)
            elif (local.hour, local.minute) >= (15, 30):
                await self._async_compare(now)
            elif not self._remaining(self.data.plan, now):
                await self._async_refresh_plan(local.date(), now)
            if self.data.plan:
                remaining = self._remaining(self.data.plan, now)
                if remaining and len(remaining) != len(self.data.plan.intervals):
                    self.async_set_updated_data(self._read_data(
                        cycles.summarize(remaining, battery), self.data.planning_error,
                        self.data.monitoring_error,
                    ))
        except (KeyError, TypeError, ValueError) as err:
            self._publish_error(self.data.plan, err)

    def _greedy_from_slots(self, slots, battery, soc, account):
        inputs = optimizer.OptimizerInputs(
            [i.timestamp for i in slots], [i.price_per_kwh for i in slots],
            [self._consumption_profile.consumption_kwh(i.timestamp) for i in slots],
            soc, battery, account.stored_energy_cost_per_kwh if account.initialized else None,
        )
        return optimizer.create_greedy_charge_plan(inputs, float(self.entry.data.get(
            CONF_CHARGE_PRICE_THRESHOLD, CHARGE_PRICE_THRESHOLD_PER_KWH)))

    async def _async_compare(self, now, *, rebuild=False):
        """Construct, validate, persist, then atomically publish one revision."""
        now = now.replace(microsecond=0)
        async with self._plan_lock:
            if self._shutting_down:
                return
            local = dt_util.as_local(now)
            tomorrow = local.date() + timedelta(days=1)
            if not rebuild and self._compared_date is not None and self._compared_date >= tomorrow:
                return
            previous = self.data.plan if self.data else None
            try:
                # 23:50 and mid-slot startups defer comparison to the next quarter:
                # measured SOC must anchor a complete remaining interval.
                if now.minute % 15 or now.second or now.microsecond:
                    return
                data = self._read_data()
                self._validate_plan_inputs(data)
                battery = self._battery_parameters(data)
                snapshot = self._remaining(previous, now) if previous else []
                today_slots = [i for i in snapshot if i.timestamp.date() == local.date()]
                if rebuild:
                    source = self._remaining(self._baseline, now) if self._baseline else snapshot
                    today_source = [i for i in source if i.timestamp.date() == local.date()]
                    today = self._greedy_from_slots(today_source, battery, data.current_soc_percent, data.account)
                elif today_slots:
                    try:
                        today = cycles.replay(today_slots, battery, data.current_soc_percent)
                    except ValueError:
                        # Even sub-tolerance SOC drift can invalidate a plan that
                        # touches minimum SOC. Rebuild it before comparing.
                        today = self._greedy_from_slots(today_slots, battery, data.current_soc_percent, data.account)
                else:
                    today = self._create_charge_plan(data, local.date(), now)
                today = cycles.replay(today.intervals, battery, data.current_soc_percent)
                midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=local.tzinfo)
                expected = list(range(int(now.timestamp()), int(midnight.timestamp()), 900))
                if [i.timestamp.timestamp() for i in today.intervals] != expected:
                    # Startup may have had only a partial current-day feed. A
                    # later complete feed can repair that baseline on retry.
                    today = self._create_charge_plan(data, local.date(), now)
                    today = cycles.replay(today.intervals, battery, data.current_soc_percent)
                    if [i.timestamp.timestamp() for i in today.intervals] != expected:
                        raise ValueError("Today's remaining baseline coverage is incomplete")
                projected_soc = today.intervals[-1].expected_soc_percent
                future = [i for i in self._baseline.intervals if i.timestamp.date() == tomorrow] if self._baseline else []
                if rebuild and future:
                    next_day = self._greedy_from_slots(future, battery, projected_soc, data.account)
                elif rebuild and (local.hour, local.minute) < (15, 30):
                    next_day = None
                else:
                    await self.hass.services.async_call(
                        "homeassistant", "update_entity",
                        {"entity_id": self.entry.data[CONF_PRICE_ENTITY]}, blocking=True,
                    )
                    if self._shutting_down:
                        return
                    self._target_day_prices(tomorrow, require_complete=True)
                    next_day = self._create_charge_plan(replace(data, current_soc_percent=projected_soc), tomorrow)
                horizon = list(today.intervals) + (list(next_day.intervals) if next_day else [])
                result = cycles.cancel_cycles(
                    horizon, battery, data.current_soc_percent, tomorrow,
                    float(self.entry.data.get(CONF_CYCLE_COST, cycles.DEFAULT_CYCLE_COST)),
                    debug=bool(self.entry.data.get(CONF_CYCLE_DEBUG, False)),
                )
                revision = self._revision + 1
                diagnostics = {**result.diagnostics, 'comparison_timestamp': now.isoformat(),
                               'horizon_start': horizon[0].timestamp.isoformat(),
                               'horizon_end': (horizon[-1].timestamp.astimezone(UTC) + timedelta(minutes=15)).isoformat(),
                               'plan_revision': revision,
                               'soc_drift_tolerance_percentage_points': SOC_DRIFT_PERCENTAGE_POINTS,
                               'rebuild_reason': 'soc_drift' if rebuild else 'daily_comparison'}
                compared_date = tomorrow if next_day else local.date()
                payload = {'version': 1, 'battery': asdict(battery), 'opening_soc': data.current_soc_percent,
                           'baseline': cycles.serialize_plan(result.baseline),
                           'adjusted': cycles.serialize_plan(result.adjusted),
                           'comparison': diagnostics, 'debug': result.debug,
                           'matches': result.match_ledger,
                           'compared_date': compared_date.isoformat(), 'revision': revision}
                await self._plan_store.async_save(payload)
                if self._shutting_down:
                    return
            except (HomeAssistantError, OSError, KeyError, TypeError, ValueError) as err:
                self._comparison = {**self._comparison, 'comparison_status': 'retry_pending',
                                    'decision_reason': str(err), 'last_attempt_timestamp': now.isoformat()}
                self._publish_error(previous, err)
                return
            self._baseline = result.baseline
            self._comparison = diagnostics
            self._comparison_debug = result.debug
            self._compared_date = compared_date
            self._revision = revision
            self.async_set_updated_data(self._read_data(result.adjusted, monitoring_error=(
                self.data.monitoring_error if self.data else None)))
            self._cancel_startup_retry()

    async def _async_load_saved_plan(self, now):
        """Restore a dated revision without yesterday's source prices."""
        try:
            saved = await self._plan_store.async_load()
            if not saved or saved.get('version') != 1:
                return False
            battery = optimizer.BatteryParameters(**saved['battery'])
            baseline = cycles.deserialize_plan(saved['baseline'], battery)
            adjusted = cycles.deserialize_plan(saved['adjusted'], battery)
            baseline = cycles.validate_saved_plan(baseline, battery, saved['opening_soc'])
            adjusted = cycles.validate_saved_plan(adjusted, battery, saved['opening_soc'])
            cycles.validate_matches(baseline, adjusted, saved['matches'], battery)
            # Recompute command rounding instead of trusting persisted commands.
            adjusted, _ = cycles.round_commands(adjusted, battery, saved['opening_soc'])
            remaining = self._remaining(adjusted, now)
            if not remaining:
                return False
            data = self._read_data()
            self._validate_plan_inputs(data)
            if self._battery_parameters(data) != battery:
                return False
            self._baseline = baseline
            self._comparison = saved['comparison']
            self._comparison_debug = saved.get('debug', {})
            self._compared_date = date.fromisoformat(saved['compared_date'])
            self._revision = int(saved['revision'])
            self.async_set_updated_data(self._read_data(cycles.summarize(remaining, battery)))
            return True
        except (HomeAssistantError, OSError, KeyError, TypeError, ValueError) as err:
            LOGGER.warning("Unable to restore DynEnergy schedule: %s", err)
            return False
