"""Recorder-backed consumption history for the weekly profile."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
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
    hass: HomeAssistant, entity_id: str | None, max_days: int
) -> list[tuple[datetime, float]]:
    """Return completed quarter-hour consumption totals from recorder history.

    Each item is the local start of a quarter hour and the energy the house
    consumed during it. Returns an empty list whenever the history cannot be
    read, which leaves the caller with the default weekday profile.
    """
    if not entity_id:
        LOGGER.debug("No consumption entity configured; skipping history load")
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
            {entity_id},
            _STATISTICS_PERIOD,
            {"energy": "kWh"},
            {"change"},
        )
    except Exception:  # noqa: BLE001 - never let history break planning
        LOGGER.exception("Could not read consumption history for %s", entity_id)
        return []

    rows = statistics.get(entity_id) or []
    if not rows:
        LOGGER.warning(
            "No recorder statistics for %s; using the default consumption profile",
            entity_id,
        )
        return []

    return _quarter_hour_totals(rows)


def _quarter_hour_totals(rows: list[dict]) -> list[tuple[datetime, float]]:
    """Collapse five-minute statistic rows into local quarter-hour totals."""
    totals: dict[datetime, float] = {}
    for row in rows:
        change = row.get("change")
        start = row.get("start")
        if change is None or start is None:
            continue
        local_start = dt_util.as_local(dt_util.utc_from_timestamp(start))
        slot_start = local_start.replace(
            minute=local_start.minute
            // optimizer.INTERVAL_MINUTES
            * optimizer.INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        )
        totals[slot_start] = totals.get(slot_start, 0.0) + max(0.0, float(change))
    return sorted(totals.items())
