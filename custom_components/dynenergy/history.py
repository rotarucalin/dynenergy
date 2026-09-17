"""Recorder-backed consumption history for the weekly profile."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from . import optimizer

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

# Recorder keeps short-term statistics at this resolution for as long as it
# keeps states, which is the "detailed data" the profile averages over.
_STATISTICS_PERIOD = "5minute"


async def async_load_consumption_samples(
    hass: HomeAssistant,
    entity_id: str | None,
    max_days: int,
    *,
    charged_entity_id: str | None,
    discharged_entity_id: str | None,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> list[tuple[datetime, float]]:
    """Return quarter-hour demand with the battery's effect on the meter removed.

    The consumption meter includes charging and is reduced by battery discharge.
    Convert battery-side energy changes to AC energy before subtracting charging
    and adding discharge. Only complete, matching quarters from all three
    meters can become profile samples.
    """
    if not all((entity_id, charged_entity_id, discharged_entity_id)):
        LOGGER.warning("Consumption and battery energy entities are required for history")
        return []
    if not (0 < charge_efficiency <= 1 and 0 < discharge_efficiency <= 1):
        LOGGER.warning("Invalid battery efficiencies; using the default consumption profile")
        return []

    try:
        # Imported lazily: recorder is a soft dependency and may be absent.
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:
        LOGGER.warning("Recorder is unavailable; using the default consumption profile")
        return []

    try:
        instance = get_instance(hass)
    except (KeyError, RuntimeError):
        LOGGER.warning("Recorder is not running; using the default consumption profile")
        return []

    days = max(1, min(max_days, int(getattr(instance, "keep_days", max_days))))
    # Only completed quarter hours are averaged, so the partial one in progress
    # does not drag its slot down.
    end = dt_util.now().replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % optimizer.INTERVAL_MINUTES)
    start = end - timedelta(days=days)

    try:
        statistics = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {entity_id, charged_entity_id, discharged_entity_id},
            _STATISTICS_PERIOD,
            {"energy": "kWh"},
            {"change"},
        )
    except Exception:  # noqa: BLE001 - never let history break planning
        LOGGER.exception("Could not read consumption history for %s", entity_id)
        return []

    consumption = _quarter_hour_totals(statistics.get(entity_id) or [])
    charged = _quarter_hour_totals(statistics.get(charged_entity_id) or [])
    discharged = _quarter_hour_totals(statistics.get(discharged_entity_id) or [])
    shared_starts = consumption.keys() & charged.keys() & discharged.keys()
    samples = [
        (
            dt_util.as_local(dt_util.utc_from_timestamp(timestamp)),
            max(
                0.0,
                consumption[timestamp]
                - charged[timestamp] / charge_efficiency
                + discharged[timestamp] * discharge_efficiency,
            ),
        )
        for timestamp in sorted(shared_starts)
        if start.timestamp() <= timestamp
        and timestamp + optimizer.INTERVAL_MINUTES * 60 <= end.timestamp()
    ]
    if not samples:
        LOGGER.warning(
            "No complete matching consumption/battery history for %s; "
            "using the default consumption profile",
            entity_id,
        )
    return samples


def _quarter_hour_totals(rows: list[dict]) -> dict[int, float]:
    """Sum quarters with three valid five-minute rows, keyed by UTC timestamp."""
    changes: dict[int, float] = {}
    duplicates: set[int] = set()
    for row in rows:
        try:
            change = float(row["change"])
            timestamp = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not isfinite(change) or change < 0
            or not isfinite(timestamp) or timestamp % (5 * 60) != 0
        ):
            continue
        key = int(timestamp)
        if key in changes:
            duplicates.add(key)
        changes[key] = change

    totals: dict[int, float] = {}
    for timestamp in changes:
        if timestamp % (optimizer.INTERVAL_MINUTES * 60) != 0:
            continue
        parts = [timestamp + offset * 60 for offset in (0, 5, 10)]
        if all(part in changes and part not in duplicates for part in parts):
            totals[timestamp] = sum(changes[part] for part in parts)
    return totals
