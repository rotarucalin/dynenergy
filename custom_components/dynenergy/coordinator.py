"""Data coordinator for DynEnergy inputs and future optimization plans."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_CAPACITY_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_MAX_CHARGE_POWER_ENTITY,
    CONF_MAX_DISCHARGE_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    DOMAIN,
    SCAN_INTERVAL,
)
from .optimizer import OptimizationPlan

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DynEnergyData:
    """Latest upstream input states and optional optimized plan."""

    price_source_state: str | None
    current_soc_percent: float | None
    current_battery_power: float | None
    usable_capacity_kwh: float | None
    max_charge_power_kw: float | None
    max_discharge_power_kw: float | None
    grid_import_energy_kwh: float | None
    plan: OptimizationPlan | None = None


class DynEnergyCoordinator(DataUpdateCoordinator[DynEnergyData]):
    """Poll configured entities every 15 minutes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry

    async def _async_update_data(self) -> DynEnergyData:
        """Read the price source and SOC entities for the future solver."""
        price_state = self.hass.states.get(self.entry.data[CONF_PRICE_ENTITY])

        return DynEnergyData(
            price_source_state=price_state.state if price_state else None,
            current_soc_percent=self._numeric_state(CONF_SOC_ENTITY),
            current_battery_power=self._numeric_state(CONF_BATTERY_POWER_ENTITY),
            usable_capacity_kwh=self._numeric_state(CONF_CAPACITY_ENTITY),
            max_charge_power_kw=self._numeric_state(CONF_MAX_CHARGE_POWER_ENTITY),
            max_discharge_power_kw=self._numeric_state(
                CONF_MAX_DISCHARGE_POWER_ENTITY
            ),
            grid_import_energy_kwh=self._numeric_state(CONF_GRID_IMPORT_ENERGY_ENTITY),
        )

    def _numeric_state(self, config_key: str) -> float | None:
        """Read a configured numeric helper or sensor state."""
        state = self.hass.states.get(self.entry.data[config_key])
        try:
            return float(state.state) if state else None
        except ValueError:
            return None