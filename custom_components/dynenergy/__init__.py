"""DynEnergy Home Assistant integration."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from . import optimizer
from .const import DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DynEnergy from a config entry."""
    # HA caches integration modules across entry reloads. Refresh only this
    # boundary, off the event loop, before creating any optimizer objects.
    await hass.async_add_executor_job(importlib.reload, optimizer)
    from .coordinator import DynEnergyCoordinator

    coordinator = DynEnergyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a DynEnergy config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
    return unloaded
