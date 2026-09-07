"""Plan sensor platform for DynEnergy."""

from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DynEnergyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the DynEnergy plan sensor."""
    coordinator: DynEnergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DynEnergyPlanSensor(coordinator, entry)])


class DynEnergyPlanSensor(CoordinatorEntity[DynEnergyCoordinator], SensorEntity):
    """Expose the latest battery plan and its summary."""

    _attr_has_entity_name = True
    _attr_name = "Battery plan"
    _attr_icon = "mdi:battery-clock-outline"

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
            "usable_capacity_kwh": data.usable_capacity_kwh,
            "max_charge_power_kw": data.max_charge_power_kw,
            "max_discharge_power_kw": data.max_discharge_power_kw,
            "grid_import_energy_kwh": data.grid_import_energy_kwh,
            "optimization_status": "not_implemented" if data.plan is None else "ready",
        }
        if data.plan:
            attributes["summary"] = asdict(data.plan.summary)
            attributes["intervals"] = [asdict(interval) for interval in data.plan.intervals]
        return attributes