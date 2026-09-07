"""Constants for the DynEnergy integration."""

from datetime import timedelta

DOMAIN = "dynenergy"
PLATFORMS = ["sensor"]
SCAN_INTERVAL = timedelta(minutes=15)

CONF_PRICE_ENTITY = "price_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_BATTERY_POWER_ENTITY = "battery_power_entity"
CONF_CAPACITY_ENTITY = "capacity_entity"
CONF_GRID_IMPORT_ENERGY_ENTITY = "grid_import_energy_entity"
CONF_MIN_SOC_PERCENT = "min_soc_percent"
CONF_MAX_SOC_PERCENT = "max_soc_percent"
CONF_MAX_CHARGE_POWER_ENTITY = "max_charge_power_entity"
CONF_MAX_DISCHARGE_POWER_ENTITY = "max_discharge_power_entity"
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY = "discharge_efficiency"
CONF_DEGRADATION_COST = "degradation_cost"