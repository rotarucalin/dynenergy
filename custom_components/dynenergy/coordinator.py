"""Scheduling and execution coordinator for DynEnergy charge plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CHARGED_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGED_ENERGY_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_CAPACITY_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_CHARGE_POWER_TARGET_ENTITY,
    CONF_CHARGE_PRICE_THRESHOLD,
    CONF_DEGRADATION_COST,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_MAX_CHARGE_POWER_ENTITY,
    CONF_MAX_DISCHARGE_POWER_ENTITY,
    CONF_MAX_SOC_PERCENT,
    CONF_MIN_SOC_PERCENT,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    CHARGE_PRICE_THRESHOLD_PER_KWH,
    DOMAIN,
    PLAN_HOUR,
    PLAN_MINUTE,
)
from .accounting import BatteryCostAccount
from .optimizer import (
    INTERVAL_HOURS,
    INTERVAL_MINUTES,
    BatteryParameters,
    OptimizationPlan,
    OptimizerInputs,
    WeeklyConsumptionProfile,
    create_greedy_charge_plan,
    target_power_w,
)

LOGGER = logging.getLogger(__name__)

_POWER_SCALE_TO_KW: Mapping[str, float] = {
    UnitOfPower.WATT: 0.001,
    UnitOfPower.KILO_WATT: 1.0,
    UnitOfPower.MEGA_WATT: 1000.0,
}
_ENERGY_SCALE_TO_KWH: Mapping[str, float] = {
    UnitOfEnergy.WATT_HOUR: 0.001,
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.MEGA_WATT_HOUR: 1000.0,
}
_SOC_SCALE_TO_PERCENT: Mapping[str, float] = {PERCENTAGE: 1.0}


@dataclass(frozen=True, slots=True)
class DynEnergyData:
    """Latest upstream input states and optional optimized plan."""

    price_source_state: str | None
    current_soc_percent: float | None
    current_battery_power: float | None
    battery_charged_energy_kwh: float | None
    battery_discharged_energy_kwh: float | None
    usable_capacity_kwh: float | None
    max_charge_power_kw: float | None
    max_discharge_power_kw: float | None
    grid_import_energy_kwh: float | None
    account: BatteryCostAccount
    consumption_profile: WeeklyConsumptionProfile
    plan: OptimizationPlan | None = None
    planning_error: str | None = None
    monitoring_error: str | None = None


class DynEnergyCoordinator(DataUpdateCoordinator[DynEnergyData]):
    """Create daily battery plans and apply them through an input_number helper."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger=LOGGER,
            name=DOMAIN,
            config_entry=entry,
        )
        self.entry = entry
        self._unsub_plan: Callable[[], None] | None = None
        self._unsub_apply: Callable[[], None] | None = None
        self._last_target_power_w: int | None = None
        self._account = BatteryCostAccount()
        self._account_dirty = False
        self._account_interval_start: datetime | None = None
        self._account_interval_price_per_kwh: float | None = None
        self._account_store: Store[dict[str, object]] = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.account"
        )
        self._consumption_profile = WeeklyConsumptionProfile.default()
        self._consumption_profile_store: Store[dict[str, object]] = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.consumption_profile"
        )
        self._previous_grid_import_kwh: float | None = None
        self._previous_grid_import_at: datetime | None = None

    async def async_start(self) -> None:
        """Start daily planning and 15-minute charge target updates."""
        self._account = BatteryCostAccount.from_dict(
            await self._account_store.async_load()
        )
        self._consumption_profile = WeeklyConsumptionProfile.from_dict(
            await self._consumption_profile_store.async_load()
        )
        self._unsub_plan = async_track_time_change(
            self.hass,
            self._async_create_next_day_plan,
            hour=PLAN_HOUR,
            minute=PLAN_MINUTE,
            second=0,
        )
        self._unsub_apply = async_track_time_change(
            self.hass,
            self._async_run_interval_tasks,
            minute=range(0, 60, 15),
            second=0,
        )
        now = dt_util.now()
        self._start_consumption_tracking(now)
        await self._async_monitor_battery_energy(now)
        await self._async_restore_plan()
        await self._async_apply_scheduled_power(now)

    async def async_shutdown(self) -> None:
        """Stop scheduled callbacks and leave the charge target idle."""
        for unsubscribe in (self._unsub_plan, self._unsub_apply):
            if unsubscribe:
                unsubscribe()
        self._unsub_plan = None
        self._unsub_apply = None
        await self._async_set_battery_power_target(0)
        if self._account_dirty:
            await self._account_store.async_save(self._account.as_dict())
            self._account_dirty = False
        await super().async_shutdown()

    async def _async_run_interval_tasks(self, now: datetime) -> None:
        """Account for the completed interval, learn it, and apply the next target."""
        await self._async_monitor_battery_energy(now)
        await self._async_update_consumption_profile(now)
        await self._async_apply_scheduled_power(now)

    async def _async_update_data(self) -> DynEnergyData:
        """Read configured source entities without generating a new plan."""
        current_plan = self.data.plan if self.data else None
        planning_error = self.data.planning_error if self.data else None
        monitoring_error = self.data.monitoring_error if self.data else None
        return self._read_data(current_plan, planning_error, monitoring_error)

    async def _async_create_next_day_plan(self, now: datetime) -> None:
        """Refresh EPEX data and generate tomorrow's plan at 23:50 local time."""
        await self._async_refresh_plan(dt_util.as_local(now).date() + timedelta(days=1))

    async def _async_restore_plan(self) -> None:
        """Plan the remainder of today so a restart does not leave the battery idle."""
        local_now = dt_util.now()
        if (local_now.hour, local_now.minute) >= (PLAN_HOUR, PLAN_MINUTE):
            await self._async_refresh_plan(local_now.date() + timedelta(days=1))
            return
        await self._async_refresh_plan(local_now.date(), not_before=local_now)

    async def _async_refresh_plan(
        self, target_date: date, not_before: datetime | None = None
    ) -> None:
        """Refresh EPEX data and replace the active plan for the target date."""
        monitoring_error = self.data.monitoring_error if self.data else None
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": self.entry.data[CONF_PRICE_ENTITY]},
                blocking=True,
            )
            data = self._read_data()
            plan = self._create_charge_plan(data, target_date, not_before)
        except (HomeAssistantError, KeyError, TypeError, ValueError) as err:
            LOGGER.warning("Unable to create DynEnergy plan for %s: %s", target_date, err)
            self.async_set_updated_data(
                self._read_data(
                    planning_error=str(err),
                    monitoring_error=monitoring_error,
                )
            )
            return

        self.async_set_updated_data(
            self._read_data(plan=plan, monitoring_error=monitoring_error)
        )

    async def _async_apply_scheduled_power(self, now: datetime) -> None:
        """Set the signed Watt helper for the current 15-minute plan interval."""
        target_power = 0
        plan = self.data.plan if self.data else None
        if plan:
            local_now = dt_util.as_local(now)
            for interval in plan.intervals:
                if interval.timestamp <= local_now < interval.timestamp + timedelta(
                    hours=INTERVAL_HOURS
                ):
                    target_power = target_power_w(interval)
                    break

        await self._async_set_battery_power_target(target_power)

    async def _async_monitor_battery_energy(self, now: datetime) -> None:
        """Close the previous quarter using its meter deltas and cached price."""
        utc_now = now.astimezone(UTC)
        interval_start = utc_now.replace(
            minute=utc_now.minute // INTERVAL_MINUTES * INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        )
        # Startup captures a baseline; repeated callbacks must not close it early.
        if (
            self._account_interval_start is not None
            and interval_start <= self._account_interval_start
        ):
            return
        current_plan = self.data.plan if self.data else None
        planning_error = self.data.planning_error if self.data else None
        monitoring_error = None
        previous_account = self._account
        data = self._read_data(current_plan, planning_error)
        try:
            if (
                data.battery_charged_energy_kwh is None
                or data.battery_discharged_energy_kwh is None
            ):
                raise ValueError("Missing cumulative battery charged or discharged energy")

            measured_energy_kwh = self._measured_stored_energy_kwh(data)
            # Cache the new interval's price before the source drops old data
            # (notably at midnight). A missing new price must not prevent closing
            # the previous interval; retry its lookup at the next boundary.
            try:
                next_price_per_kwh = self._current_price_per_kwh(interval_start)
            except (KeyError, TypeError, ValueError):
                next_price_per_kwh = None

            if not self._account.initialized:
                self._account = self._account.initialize(
                    data.battery_charged_energy_kwh,
                    data.battery_discharged_energy_kwh,
                    measured_energy_kwh or 0.0,
                    next_price_per_kwh or 0.0,
                )
            else:
                # Keep lifetime totals on restart, but never price an unknown
                # downtime delta as though it all occurred in a single quarter.
                baseline = replace(
                    self._account,
                    previous_charged_energy_kwh=data.battery_charged_energy_kwh,
                    previous_discharged_energy_kwh=data.battery_discharged_energy_kwh,
                )
                if self._account_interval_start is None:
                    self._account = baseline
                elif self._account_interval_start != interval_start - timedelta(
                    minutes=INTERVAL_MINUTES
                ):
                    self._account = baseline
                    monitoring_error = "Missed accounting boundary; meter baselines reset"
                else:
                    try:
                        price_per_kwh = 0.0
                        if self._account.has_positive_meter_delta(
                            data.battery_charged_energy_kwh,
                            data.battery_discharged_energy_kwh,
                        ):
                            price_per_kwh = self._account_interval_price_per_kwh
                            if price_per_kwh is None:
                                price_per_kwh = self._current_price_per_kwh(
                                    self._account_interval_start
                                )
                        self._account = self._account.record(
                            data.battery_charged_energy_kwh,
                            data.battery_discharged_energy_kwh,
                            price_per_kwh,
                            measured_energy_kwh,
                        )
                    except (KeyError, TypeError, ValueError) as err:
                        self._account = baseline
                        monitoring_error = (
                            "Unable to price completed battery interval; "
                            f"meter baselines reset: {err}"
                        )
            self._account_interval_start = interval_start
            self._account_interval_price_per_kwh = next_price_per_kwh
        except (KeyError, TypeError, ValueError) as err:
            self._account_interval_start = None
            self._account_interval_price_per_kwh = None
            LOGGER.warning("Unable to update DynEnergy battery accounting: %s", err)
            self.async_set_updated_data(
                self._read_data(current_plan, planning_error, str(err))
            )
            return

        if self._account != previous_account:
            self._account_dirty = True
        if self._account_dirty:
            await self._account_store.async_save(self._account.as_dict())
            self._account_dirty = False
        if monitoring_error:
            LOGGER.warning(
                "Unable to update DynEnergy battery accounting: %s", monitoring_error
            )
        self.async_set_updated_data(
            self._read_data(current_plan, planning_error, monitoring_error)
        )

    @staticmethod
    def _measured_stored_energy_kwh(data: DynEnergyData) -> float | None:
        """Return the energy the battery reports holding, when both inputs exist."""
        if data.current_soc_percent is None or data.usable_capacity_kwh is None:
            return None
        return data.usable_capacity_kwh * data.current_soc_percent / 100

    def _start_consumption_tracking(self, now: datetime) -> None:
        """Capture a grid-import baseline without learning a partial interval."""
        self._previous_grid_import_kwh = self._numeric_state(
            CONF_GRID_IMPORT_ENERGY_ENTITY, _ENERGY_SCALE_TO_KWH
        )
        self._previous_grid_import_at = dt_util.as_local(now)

    async def _async_update_consumption_profile(self, now: datetime) -> None:
        """Add the completed 15-minute grid-import sample to its weekly slot."""
        local_now = dt_util.as_local(now)
        grid_import_kwh = self._numeric_state(
            CONF_GRID_IMPORT_ENERGY_ENTITY, _ENERGY_SCALE_TO_KWH
        )
        previous_kwh = self._previous_grid_import_kwh
        previous_at = self._previous_grid_import_at
        is_full_interval = (
            previous_at is not None
            and previous_at.second == 0
            and previous_at.microsecond == 0
            and previous_at.minute % INTERVAL_MINUTES == 0
            and local_now.timestamp() - previous_at.timestamp()
            == INTERVAL_MINUTES * 60
        )

        if (
            grid_import_kwh is not None
            and previous_kwh is not None
            and previous_at is not None
            and grid_import_kwh >= previous_kwh
            and is_full_interval
        ):
            self._consumption_profile = self._consumption_profile.record(
                previous_at, grid_import_kwh - previous_kwh
            )
            await self._consumption_profile_store.async_save(
                self._consumption_profile.as_dict()
            )

        self._previous_grid_import_kwh = grid_import_kwh
        self._previous_grid_import_at = local_now
        current_plan = self.data.plan if self.data else None
        planning_error = self.data.planning_error if self.data else None
        monitoring_error = self.data.monitoring_error if self.data else None
        self.async_set_updated_data(
            self._read_data(current_plan, planning_error, monitoring_error)
        )

    async def _async_set_battery_power_target(self, target_power_w: int) -> None:
        """Write a changed signed power target to the configured input_number helper."""
        target_entity = self.entry.data.get(CONF_CHARGE_POWER_TARGET_ENTITY)
        if not target_entity or target_power_w == self._last_target_power_w:
            return

        try:
            await self.hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": target_entity, "value": target_power_w},
                blocking=True,
            )
        except HomeAssistantError as err:
            LOGGER.error("Unable to set DynEnergy battery power target: %s", err)
            return
        self._last_target_power_w = target_power_w

    def _read_data(
        self,
        plan: OptimizationPlan | None = None,
        planning_error: str | None = None,
        monitoring_error: str | None = None,
    ) -> DynEnergyData:
        """Read the latest configured input states."""
        price_state = self.hass.states.get(self.entry.data[CONF_PRICE_ENTITY])

        return DynEnergyData(
            price_source_state=price_state.state if price_state else None,
            current_soc_percent=self._numeric_state(
                CONF_SOC_ENTITY, _SOC_SCALE_TO_PERCENT
            ),
            current_battery_power=self._numeric_state(
                CONF_BATTERY_POWER_ENTITY, _POWER_SCALE_TO_KW
            ),
            battery_charged_energy_kwh=self._numeric_state(
                CONF_BATTERY_CHARGED_ENERGY_ENTITY, _ENERGY_SCALE_TO_KWH
            ),
            battery_discharged_energy_kwh=self._numeric_state(
                CONF_BATTERY_DISCHARGED_ENERGY_ENTITY, _ENERGY_SCALE_TO_KWH
            ),
            usable_capacity_kwh=self._numeric_state(
                CONF_CAPACITY_ENTITY, _ENERGY_SCALE_TO_KWH
            ),
            max_charge_power_kw=self._numeric_state(
                CONF_MAX_CHARGE_POWER_ENTITY, _POWER_SCALE_TO_KW
            ),
            max_discharge_power_kw=self._numeric_state(
                CONF_MAX_DISCHARGE_POWER_ENTITY, _POWER_SCALE_TO_KW
            ),
            grid_import_energy_kwh=self._numeric_state(
                CONF_GRID_IMPORT_ENERGY_ENTITY, _ENERGY_SCALE_TO_KWH
            ),
            account=self._account,
            consumption_profile=self._consumption_profile,
            plan=plan,
            planning_error=planning_error,
            monitoring_error=monitoring_error,
        )

    def _create_charge_plan(
        self,
        data: DynEnergyData,
        target_date: date,
        not_before: datetime | None = None,
    ) -> OptimizationPlan:
        """Build the greedy charging plan from EPEX prices and helper values."""
        required_values = {
            "current SOC": data.current_soc_percent,
            "usable capacity": data.usable_capacity_kwh,
            "maximum charge power": data.max_charge_power_kw,
            "maximum discharge power": data.max_discharge_power_kw,
        }
        missing = [name for name, value in required_values.items() if value is None]
        if missing:
            raise ValueError(f"Missing numeric input: {', '.join(missing)}")

        timestamps, prices_per_kwh = self._target_day_prices(target_date, not_before)
        battery = BatteryParameters(
            usable_capacity_kwh=data.usable_capacity_kwh,
            max_charge_power_kw=data.max_charge_power_kw,
            max_discharge_power_kw=data.max_discharge_power_kw,
            min_soc_percent=float(self.entry.data[CONF_MIN_SOC_PERCENT]),
            max_soc_percent=float(self.entry.data[CONF_MAX_SOC_PERCENT]),
            charge_efficiency=float(self.entry.data[CONF_CHARGE_EFFICIENCY]),
            discharge_efficiency=float(self.entry.data[CONF_DISCHARGE_EFFICIENCY]),
            degradation_cost_per_kwh=float(
                self.entry.data.get(CONF_DEGRADATION_COST, 0.0)
            ),
        )
        inputs = OptimizerInputs(
            timestamps=timestamps,
            prices_per_kwh=prices_per_kwh,
            consumption_kwh=[
                self._consumption_profile.consumption_kwh(timestamp)
                for timestamp in timestamps
            ],
            current_soc_percent=data.current_soc_percent,
            battery=battery,
            stored_energy_cost_per_kwh=(
                data.account.stored_energy_cost_per_kwh
                if data.account.initialized
                else None
            ),
        )
        return create_greedy_charge_plan(
            inputs,
            float(
                self.entry.data.get(
                    CONF_CHARGE_PRICE_THRESHOLD,
                    CHARGE_PRICE_THRESHOLD_PER_KWH,
                )
            ),
        )

    def _target_day_prices(
        self, target_date: date, not_before: datetime | None = None
    ) -> tuple[list[datetime], list[float]]:
        """Extract target-day EPEX prices and expand hourly records to quarters."""
        price_state = self.hass.states.get(self.entry.data[CONF_PRICE_ENTITY])
        raw_data = price_state.attributes.get("data") if price_state else None
        if not isinstance(raw_data, Sequence) or isinstance(raw_data, str):
            raise ValueError("EPEX price entity must provide a data attribute")

        local_not_before = dt_util.as_local(not_before) if not_before else None
        intervals: list[tuple[datetime, float]] = []
        for raw_interval in raw_data:
            if not isinstance(raw_interval, Mapping):
                continue
            try:
                start = dt_util.as_local(
                    datetime.fromisoformat(str(raw_interval["start_time"]))
                )
                end = dt_util.as_local(
                    datetime.fromisoformat(str(raw_interval["end_time"]))
                )
                price_per_kwh = float(raw_interval["price_per_kwh"])
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError("EPEX data has an invalid price interval") from err

            if start.date() != target_date:
                continue
            duration_seconds = end.timestamp() - start.timestamp()
            if duration_seconds == 15 * 60:
                quarter_starts = [start]
            elif duration_seconds == 60 * 60:
                quarter_starts = [
                    start + timedelta(minutes=15 * offset) for offset in range(4)
                ]
            else:
                raise ValueError("EPEX intervals must be 15 or 60 minutes long")

            intervals.extend(
                (quarter_start, price_per_kwh)
                for quarter_start in quarter_starts
                if local_not_before is None
                or quarter_start + timedelta(minutes=15) > local_not_before
            )

        intervals.sort(key=lambda interval: interval[0])
        if not intervals:
            raise ValueError(f"No EPEX prices available for {target_date.isoformat()}")
        if len({timestamp for timestamp, _ in intervals}) != len(intervals):
            raise ValueError("EPEX data contains duplicate price intervals")

        return (
            [timestamp for timestamp, _ in intervals],
            [price_per_kwh for _, price_per_kwh in intervals],
        )

    def _current_price_per_kwh(self, now: datetime) -> float:
        """Return the EPEX price interval containing the supplied local time."""
        price_state = self.hass.states.get(self.entry.data[CONF_PRICE_ENTITY])
        raw_data = price_state.attributes.get("data") if price_state else None
        if not isinstance(raw_data, Sequence) or isinstance(raw_data, str):
            raise ValueError("EPEX price entity must provide a data attribute")

        local_now = dt_util.as_local(now)
        for raw_interval in raw_data:
            if not isinstance(raw_interval, Mapping):
                continue
            try:
                start = dt_util.as_local(
                    datetime.fromisoformat(str(raw_interval["start_time"]))
                )
                end = dt_util.as_local(
                    datetime.fromisoformat(str(raw_interval["end_time"]))
                )
                price_per_kwh = float(raw_interval["price_per_kwh"])
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError("EPEX data has an invalid price interval") from err
            if start.timestamp() <= local_now.timestamp() < end.timestamp():
                return price_per_kwh

        raise ValueError("No EPEX price available for the current interval")

    def _numeric_state(
        self,
        config_key: str,
        scale_by_unit: Mapping[str, float] | None = None,
    ) -> float | None:
        """Read a configured numeric helper or sensor state in its expected unit."""
        entity_id = self.entry.data.get(config_key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None

        if scale_by_unit is None:
            return value
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit is None:
            return value
        scale = scale_by_unit.get(unit)
        if scale is None:
            LOGGER.warning(
                "Unsupported unit %s on %s; using the raw value", unit, entity_id
            )
            return value
        return value * scale
