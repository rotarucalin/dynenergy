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

from .const import CONF_CHARGE_PRICE_THRESHOLD, CHARGE_PRICE_THRESHOLD_PER_KWH, DOMAIN
from .coordinator import DynEnergyCoordinator
from .optimizer import INTERVAL_HOURS


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
                {
                    "timestamp": interval.timestamp.isoformat(),
                    "price_per_kwh": interval.price_per_kwh,
                    "consumption_kwh": interval.consumption_kwh,
                    "target_battery_power_w": int(
                        interval.target_battery_power_kw * 1000
                    ),
                    "expected_soc_percent": interval.expected_soc_percent,
                    "state": interval.state.value,
                }
                for interval in data.plan.intervals
                if interval.timestamp + timedelta(hours=INTERVAL_HOURS) > local_now
            ]
        return attributes


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


class DynEnergyTotalChargingCostSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose cumulative EPEX costs paid to charge the battery."""

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
        """Return cumulative charging costs in euros."""
        return self.coordinator.data.account.total_charging_cost_eur


class DynEnergyTotalSavedCostSensor(
    CoordinatorEntity[DynEnergyCoordinator], SensorEntity
):
    """Expose cumulative EPEX savings from battery discharge."""

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
        """Return cumulative avoided EPEX cost less stored-energy cost."""
        return self.coordinator.data.account.total_saved_cost_eur