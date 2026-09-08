"""Scheduling and execution coordinator for DynEnergy charge plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
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
    BatteryParameters,
    OptimizationPlan,
    OptimizerInputs,
    create_greedy_charge_plan,
    default_consumption_kwh,
)

LOGGER = logging.getLogger(__name__)


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
        self._unsub_monitor: Callable[[], None] | None = None
        self._last_target_power_w: int | None = None
        self._account = BatteryCostAccount()
        self._account_store: Store[dict[str, object]] = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.account"
        )

    async def async_start(self) -> None:
        """Start daily planning and 15-minute charge target updates."""
        self._account = BatteryCostAccount.from_dict(
            await self._account_store.async_load()
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
            self._async_apply_scheduled_power,
            minute=range(0, 60, 15),
            second=0,
        )
        self._unsub_monitor = async_track_time_change(
            self.hass,
            self._async_monitor_battery_energy,
            minute=range(60),
            second=0,
        )
        await self._async_monitor_battery_energy(dt_util.now())
        await self._async_apply_scheduled_power(dt_util.now())

    async def async_shutdown(self) -> None:
        """Stop scheduled callbacks and leave the charge target idle."""
        if self._unsub_plan:
            self._unsub_plan()
        if self._unsub_apply:
            self._unsub_apply()
        if self._unsub_monitor:
            self._unsub_monitor()
        await self._async_set_battery_power_target(0)

    async def _async_update_data(self) -> DynEnergyData:
        """Read configured source entities without generating a new plan."""
        current_plan = self.data.plan if self.data else None
        planning_error = self.data.planning_error if self.data else None
        monitoring_error = self.data.monitoring_error if self.data else None
        return self._read_data(current_plan, planning_error, monitoring_error)

    async def _async_create_next_day_plan(self, now: datetime) -> None:
        """Refresh EPEX data and generate tomorrow's plan at 23:50 local time."""
        target_date = dt_util.as_local(now).date() + timedelta(days=1)
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": self.entry.data[CONF_PRICE_ENTITY]},
                blocking=True,
            )
            data = self._read_data()
            plan = self._create_charge_plan(data, target_date)
        except (HomeAssistantError, KeyError, TypeError, ValueError) as err:
            LOGGER.warning("Unable to create DynEnergy plan for %s: %s", target_date, err)
            self.async_set_updated_data(
                self._read_data(
                    planning_error=str(err),
                    monitoring_error=self.data.monitoring_error if self.data else None,
                )
            )
            return

        self.async_set_updated_data(
            self._read_data(
                plan=plan,
                monitoring_error=self.data.monitoring_error if self.data else None,
            )
        )

    async def _async_apply_scheduled_power(self, now: datetime) -> None:
        """Set the signed Watt helper for the current 15-minute plan interval."""
        target_power_w = 0
        plan = self.data.plan if self.data else None
        if plan:
            local_now = dt_util.as_local(now)
            for interval in plan.intervals:
                if interval.timestamp <= local_now < interval.timestamp + timedelta(
                    hours=INTERVAL_HOURS
                ):
                    target_power_w = int(interval.target_battery_power_kw * 1000)
                    break

        await self._async_set_battery_power_target(target_power_w)

    async def _async_monitor_battery_energy(self, now: datetime) -> None:
        """Account for battery meter deltas at the current EPEX price each minute."""
        current_plan = self.data.plan if self.data else None
        planning_error = self.data.planning_error if self.data else None
        data = self._read_data(current_plan, planning_error)
        try:
            if (
                data.battery_charged_energy_kwh is None
                or data.battery_discharged_energy_kwh is None
            ):
                raise ValueError("Missing cumulative battery charged or discharged energy")

            if not self._account.initialized:
                if data.current_soc_percent is None or data.usable_capacity_kwh is None:
                    raise ValueError("Missing SOC or usable capacity for accounting baseline")
                self._account = self._account.initialize(
                    data.battery_charged_energy_kwh,
                    data.battery_discharged_energy_kwh,
                    data.usable_capacity_kwh * data.current_soc_percent / 100,
                )
            elif self._account.has_positive_meter_delta(
                data.battery_charged_energy_kwh, data.battery_discharged_energy_kwh
            ):
                self._account = self._account.record(
                    data.battery_charged_energy_kwh,
                    data.battery_discharged_energy_kwh,
                    self._current_price_per_kwh(now),
                    float(self.entry.data[CONF_CHARGE_EFFICIENCY]),
                    float(self.entry.data[CONF_DISCHARGE_EFFICIENCY]),
                )
            else:
                self._account = self._account.record(
                    data.battery_charged_energy_kwh,
                    data.battery_discharged_energy_kwh,
                    0.0,
                    float(self.entry.data[CONF_CHARGE_EFFICIENCY]),
                    float(self.entry.data[CONF_DISCHARGE_EFFICIENCY]),
                )
        except (KeyError, TypeError, ValueError) as err:
            LOGGER.warning("Unable to update DynEnergy battery accounting: %s", err)
            self.async_set_updated_data(
                self._read_data(current_plan, planning_error, str(err))
            )
            return

        await self._account_store.async_save(self._account.as_dict())
        self.async_set_updated_data(self._read_data(current_plan, planning_error))

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
            current_soc_percent=self._numeric_state(CONF_SOC_ENTITY),
            current_battery_power=self._numeric_state(CONF_BATTERY_POWER_ENTITY),
            battery_charged_energy_kwh=self._numeric_state(
                CONF_BATTERY_CHARGED_ENERGY_ENTITY
            ),
            battery_discharged_energy_kwh=self._numeric_state(
                CONF_BATTERY_DISCHARGED_ENERGY_ENTITY
            ),
            usable_capacity_kwh=self._numeric_state(CONF_CAPACITY_ENTITY),
            max_charge_power_kw=self._numeric_state(CONF_MAX_CHARGE_POWER_ENTITY),
            max_discharge_power_kw=self._numeric_state(
                CONF_MAX_DISCHARGE_POWER_ENTITY
            ),
            grid_import_energy_kwh=self._numeric_state(CONF_GRID_IMPORT_ENERGY_ENTITY),
            account=self._account,
            plan=plan,
            planning_error=planning_error,
            monitoring_error=monitoring_error,
        )

    def _create_charge_plan(
        self, data: DynEnergyData, target_date: date
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

        timestamps, prices_per_kwh = self._target_day_prices(target_date)
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
            consumption_kwh=[default_consumption_kwh(timestamp) for timestamp in timestamps],
            current_soc_percent=data.current_soc_percent,
            battery=battery,
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

    def _target_day_prices(self, target_date: date) -> tuple[list[datetime], list[float]]:
        """Extract target-day EPEX prices and expand hourly records to quarters."""
        price_state = self.hass.states.get(self.entry.data[CONF_PRICE_ENTITY])
        raw_data = price_state.attributes.get("data") if price_state else None
        if not isinstance(raw_data, Sequence) or isinstance(raw_data, str):
            raise ValueError("EPEX price entity must provide a data attribute")

        intervals: list[tuple[datetime, float]] = []
        for raw_interval in raw_data:
            if not isinstance(raw_interval, Mapping):
                continue
            try:
                start = datetime.fromisoformat(
                    str(raw_interval["start_time"]).replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    str(raw_interval["end_time"]).replace("Z", "+00:00")
                )
                price_per_kwh = float(raw_interval["price_per_kwh"])
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError("EPEX data has an invalid price interval") from err

            if start.date() != target_date:
                continue
            duration_seconds = end.timestamp() - start.timestamp()
            if duration_seconds == 15 * 60:
                intervals.append((start, price_per_kwh))
            elif duration_seconds == 60 * 60:
                intervals.extend(
                    (start + timedelta(minutes=15 * offset), price_per_kwh)
                    for offset in range(4)
                )
            else:
                raise ValueError("EPEX intervals must be 15 or 60 minutes long")

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
                    datetime.fromisoformat(
                        str(raw_interval["start_time"]).replace("Z", "+00:00")
                    )
                )
                end = dt_util.as_local(
                    datetime.fromisoformat(
                        str(raw_interval["end_time"]).replace("Z", "+00:00")
                    )
                )
                price_per_kwh = float(raw_interval["price_per_kwh"])
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError("EPEX data has an invalid price interval") from err
            if start <= local_now < end:
                return price_per_kwh

        raise ValueError("No EPEX price available for the current interval")

    def _numeric_state(self, config_key: str) -> float | None:
        """Read a configured numeric helper or sensor state."""
        entity_id = self.entry.data.get(config_key)
        state = self.hass.states.get(entity_id) if entity_id else None
        try:
            return float(state.state) if state else None
        except ValueError:
            return None