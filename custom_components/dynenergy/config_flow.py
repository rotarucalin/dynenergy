"""Config flow for DynEnergy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_CAPACITY_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_DEGRADATION_COST,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_MAX_CHARGE_POWER_ENTITY,
    CONF_MAX_DISCHARGE_POWER_ENTITY,
    CONF_MAX_SOC_PERCENT,
    CONF_MIN_SOC_PERCENT,
    CONF_PRICE_ENTITY,
    CONF_SOC_ENTITY,
    DOMAIN,
)


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the configuration schema with optional existing values."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_PRICE_ENTITY, default=defaults.get(CONF_PRICE_ENTITY)
            ): selector.EntitySelector(),
            vol.Required(CONF_SOC_ENTITY, default=defaults.get(CONF_SOC_ENTITY)): selector.EntitySelector(),
            vol.Required(
                CONF_BATTERY_POWER_ENTITY,
                default=defaults.get(CONF_BATTERY_POWER_ENTITY),
            ): selector.EntitySelector(),
            vol.Required(
                CONF_CAPACITY_ENTITY, default=defaults.get(CONF_CAPACITY_ENTITY)
            ): selector.EntitySelector(),
            vol.Required(
                CONF_MAX_CHARGE_POWER_ENTITY,
                default=defaults.get(CONF_MAX_CHARGE_POWER_ENTITY),
            ): selector.EntitySelector(),
            vol.Required(
                CONF_MAX_DISCHARGE_POWER_ENTITY,
                default=defaults.get(CONF_MAX_DISCHARGE_POWER_ENTITY),
            ): selector.EntitySelector(),
            vol.Required(
                CONF_GRID_IMPORT_ENERGY_ENTITY,
                default=defaults.get(CONF_GRID_IMPORT_ENERGY_ENTITY),
            ): selector.EntitySelector(),
            vol.Required(
                CONF_MIN_SOC_PERCENT, default=defaults.get(CONF_MIN_SOC_PERCENT, 10.0)
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Required(
                CONF_MAX_SOC_PERCENT, default=defaults.get(CONF_MAX_SOC_PERCENT, 100.0)
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Required(
                CONF_CHARGE_EFFICIENCY, default=defaults.get(CONF_CHARGE_EFFICIENCY, 0.95)
            ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=1)),
            vol.Required(
                CONF_DISCHARGE_EFFICIENCY,
                default=defaults.get(CONF_DISCHARGE_EFFICIENCY, 0.95),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=1)),
            vol.Optional(
                CONF_DEGRADATION_COST, default=defaults.get(CONF_DEGRADATION_COST, 0.0)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
        }
    )


class DynEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure DynEnergy."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial configuration step."""
        if user_input is not None:
            if user_input[CONF_MIN_SOC_PERCENT] >= user_input[CONF_MAX_SOC_PERCENT]:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_schema(user_input),
                    errors={CONF_MAX_SOC_PERCENT: "max_soc_must_exceed_min_soc"},
                )
            return self.async_create_entry(title="DynEnergy", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema())