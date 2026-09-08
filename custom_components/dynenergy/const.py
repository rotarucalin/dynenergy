"""Constants for the DynEnergy integration."""

from datetime import timedelta

DOMAIN = "dynenergy"
PLATFORMS = ["sensor"]
PLAN_HOUR = 23
PLAN_MINUTE = 50
CHARGE_PRICE_THRESHOLD_PER_KWH = 0.10

CONF_PRICE_ENTITY = "price_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_BATTERY_POWER_ENTITY = "battery_power_entity"
CONF_BATTERY_CHARGED_ENERGY_ENTITY = "battery_charged_energy_entity"
CONF_BATTERY_DISCHARGED_ENERGY_ENTITY = "battery_discharged_energy_entity"
CONF_CAPACITY_ENTITY = "capacity_entity"
CONF_GRID_IMPORT_ENERGY_ENTITY = "grid_import_energy_entity"
CONF_MIN_SOC_PERCENT = "min_soc_percent"
CONF_MAX_SOC_PERCENT = "max_soc_percent"
CONF_MAX_CHARGE_POWER_ENTITY = "max_charge_power_entity"
CONF_MAX_DISCHARGE_POWER_ENTITY = "max_discharge_power_entity"
CONF_CHARGE_POWER_TARGET_ENTITY = "charge_power_target_entity"
CONF_CHARGE_PRICE_THRESHOLD = "charge_price_threshold"
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY = "discharge_efficiency"
CONF_DEGRADATION_COST = "degradation_cost"