"""Plan sensor platform for DynEnergy."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from homeassistant.const import UnitOfPower

from .const import CONF_CHARGE_PRICE_THRESHOLD, CHARGE_PRICE_THRESHOLD_PER_KWH, DOMAIN
from .coordinator import DynEnergyCoordinator
from .optimizer import (
    INTERVAL_HOURS,
    INTERVALS_PER_DAY,
    PlanInterval,
    target_power_w,
)

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the DynEnergy plan sensor."""
    coordinator: DynEnergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DynEnergyPlanSensor(coordinator, entry),
            DynEnergyPowerRecommendationSensor(coordinator, entry),
            DynEnergyTypicalConsumptionSensor(coordinator, entry),
            DynEnergyStoredEnergyCostSensor(coordinator, entry),
            DynEnergyTotalChargingCostSensor(coordinator, entry),
            DynEnergyTotalSavedCostSensor(coordinator, entry),
        ]
    )


class DynEnergyPlanSensor(CoordinatorEntity[DynEnergyCoordinator], SensorEntity):
    """Expose the latest battery plan and its summary."""

    _attr_has_entity_name = True
    _attr_name = "Battery plan"
    _attr_icon = "mdi:battery-clock-outline"
    _unrecorded_attributes = frozenset({"intervals", "summary"})

    def __init__(self, coordinator: DynEnergyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the plan sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_plan"

    @property
    def native_value(self) -> float | None:
        """Return expected daily saving once the solver has produced a plan."""
        plan = self.coordinator.data.plan
        return plan.summary.daily_saving if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Provide source diagnostics and the full future schedule."""
        data = self.coordinator.data
        attributes: dict[str, object] = {
            "price_source_state": data.price_source_state,
            "current_soc_percent": data.current_soc_percent,
            "current_battery_power": data.current_battery_power,
            "battery_charged_energy_kwh": data.battery_charged_energy_kwh,
            "battery_discharged_energy_kwh": data.battery_discharged_energy_kwh,
            "usable_capacity_kwh": data.usable_capacity_kwh,
            "max_charge_power_kw": data.max_charge_power_kw,
            "max_discharge_power_kw": data.max_discharge_power_kw,
            "grid_import_energy_kwh": data.grid_import_energy_kwh,
            "optimization_status": (
                "failed"
                if data.planning_error
                else "waiting_for_23_50"
                if data.plan is None
                else "ready"
            ),
            "planning_error": data.planning_error,
            "monitoring_error": data.monitoring_error,
            "stored_energy_kwh": data.account.stored_energy_kwh,
            "stored_energy_cost_eur": data.account.stored_energy_cost_eur,
            "charge_price_threshold_per_kwh": self.coordinator.entry.data.get(
                CONF_CHARGE_PRICE_THRESHOLD, CHARGE_PRICE_THRESHOLD_PER_KWH
            ),
        }
        if data.plan:
            local_now = dt_util.now()
            attributes["summary"] = asdict(data.plan.summary)
            attributes["intervals"] = [
                _interval_as_dict(interval)
                for interval in data.plan.intervals
                if interval.timestamp + timedelta(hours=INTERVAL_HOURS) > local_now
            ]
        return attributes


def _interval_as_dict(interval: PlanInterval) -> dict[str, object]:
    """Render one plan interval for entity attributes."""
    return {
        "timestamp": interval.timestamp.isoformat(),
        "price_per_kwh": interval.price_per_kwh,
        "consumption_kwh": interval.consumption_kwh,
        "target_battery_power_w": target_power_w(interval),
        "expected_soc_percent": interval.expected_soc_percent,
        "state": interval.state.value,
    }


class DynEnergyPowerRecommendationSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose the recommended battery power for the current and all plan intervals."""

    _attr_has_entity_name = True
    _attr_name = "Battery power recommendation"
    _attr_icon = "mdi:transmission-tower"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"intervals"})

    def __init__(self, coordinator: DynEnergyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the power recommendation sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_power_recommendation"

    def _current_interval(self) -> PlanInterval | None:
        """Return the plan interval containing the current local time."""
        plan = self.coordinator.data.plan
        if not plan:
            return None
        local_now = dt_util.now()
        return next(
            (
                interval
                for interval in plan.intervals
                if interval.timestamp
                <= local_now
                < interval.timestamp + timedelta(hours=INTERVAL_HOURS)
            ),
            None,
        )

    @property
    def native_value(self) -> int | None:
        """Return the signed Watt recommendation for the current interval."""
        interval = self._current_interval()
        if interval is None:
            return None if self.coordinator.data.plan is None else 0
        return target_power_w(interval)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Provide the recommendation for every interval of the active plan."""
        plan = self.coordinator.data.plan
        if not plan:
            return {"intervals": []}
        current = self._current_interval()
        return {
            "current_interval_start": (
                current.timestamp.isoformat() if current else None
            ),
            "current_state": current.state.value if current else None,
            "plan_start": plan.intervals[0].timestamp.isoformat(),
            "plan_end": (
                plan.intervals[-1].timestamp + timedelta(hours=INTERVAL_HOURS)
            ).isoformat(),
            "interval_minutes": int(INTERVAL_HOURS * 60),
            "intervals": [_interval_as_dict(interval) for interval in plan.intervals],
        }


class DynEnergyTypicalConsumptionSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose the learned typical consumption for the current weekly slot."""

    _attr_has_entity_name = True
    _attr_name = "Typical consumption"
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"weekly_profile_w", "sample_counts"})

    def __init__(self, coordinator: DynEnergyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the typical-consumption sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_typical_consumption"

    @property
    def native_value(self) -> int:
        """Return the current slot's typical average power in Watts."""
        consumption_kwh = self.coordinator.data.consumption_profile.consumption_kwh(
            dt_util.now()
        )
        return round(consumption_kwh / INTERVAL_HOURS * 1000)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose all 672 weekly consumption averages and sample counts."""
        profile = self.coordinator.data.consumption_profile
        weekly_profile_w: dict[str, list[int]] = {}
        sample_counts: dict[str, list[int]] = {}
        for weekday, name in enumerate(_WEEKDAYS):
            start = weekday * INTERVALS_PER_DAY
            end = start + INTERVALS_PER_DAY
            weekly_profile_w[name] = [
                round(value / INTERVAL_HOURS * 1000)
                for value in profile.values_kwh[start:end]
            ]
            sample_counts[name] = list(profile.sample_counts[start:end])
        return {
            "interval_minutes": int(INTERVAL_HOURS * 60),
            "weekly_profile_w": weekly_profile_w,
            "sample_counts": sample_counts,
        }


class DynEnergyStoredEnergyCostSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose the weighted-average EPEX cost of energy in the battery."""

    _attr_has_entity_name = True
    _attr_name = "Stored energy cost"
    _attr_icon = "mdi:battery-charging-medium"
    _attr_native_unit_of_measurement = "ct/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DynEnergyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the stored-energy cost sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_stored_energy_cost"

    @property
    def native_value(self) -> float:
        """Return the current stored-energy weighted average in cents."""
        return self.coordinator.data.account.stored_energy_cost_per_kwh * 100

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the lifetime average price paid for charged energy."""
        account = self.coordinator.data.account
        return {
            "stored_energy_kwh": account.stored_energy_kwh,
            "total_charged_kwh": account.total_charged_kwh,
            "average_charge_price_ct_per_kwh": (
                account.average_charge_price_per_kwh * 100
            ),
        }


class DynEnergyTotalChargingCostSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose cumulative EPEX costs of measured battery consumption."""

    _attr_has_entity_name = True
    _attr_name = "Total costs"
    _attr_icon = "mdi:cash-plus"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DynEnergyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the total charging-cost sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_total_costs"

    @property
    def native_value(self) -> float:
        """Return cumulative measured charging costs in euros."""
        return self.coordinator.data.account.total_charging_cost_eur


class DynEnergyTotalSavedCostSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose cumulative EPEX value of measured battery generation."""

    _attr_has_entity_name = True
    _attr_name = "Total savings"
    _attr_icon = "mdi:cash-check"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: DynEnergyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the total savings sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_total_savings"

    @property
    def native_value(self) -> float:
        """Return cumulative measured discharge savings."""
        return self.coordinator.data.account.total_saved_cost_eur
