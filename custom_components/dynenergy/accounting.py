"""Persistent cost accounting for energy stored in a battery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class BatteryCostAccount:
    """Cost basis and cumulative savings for battery-side energy counters."""

    stored_energy_kwh: float = 0.0
    stored_energy_cost_eur: float = 0.0
    total_charging_cost_eur: float = 0.0
    total_saved_cost_eur: float = 0.0
    previous_charged_energy_kwh: float | None = None
    previous_discharged_energy_kwh: float | None = None
    initialized: bool = False

    @property
    def stored_energy_cost_per_kwh(self) -> float:
        """Return the weighted-average cost basis of stored energy."""
        if self.stored_energy_kwh <= _EPSILON:
            return 0.0
        return self.stored_energy_cost_eur / self.stored_energy_kwh

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> BatteryCostAccount:
        """Restore an account, tolerating a missing or older stored payload."""
        if not data:
            return cls()

        def number(key: str, default: float = 0.0) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        def optional_number(key: str) -> float | None:
            value = data.get(key)
            return number(key) if value is not None else None

        return cls(
            stored_energy_kwh=max(0.0, number("stored_energy_kwh")),
            stored_energy_cost_eur=number("stored_energy_cost_eur"),
            total_charging_cost_eur=number("total_charging_cost_eur"),
            total_saved_cost_eur=number("total_saved_cost_eur"),
            previous_charged_energy_kwh=optional_number(
                "previous_charged_energy_kwh"
            ),
            previous_discharged_energy_kwh=optional_number(
                "previous_discharged_energy_kwh"
            ),
            initialized=bool(data.get("initialized", False)),
        )

    def as_dict(self) -> dict[str, float | bool | None]:
        """Serialize the account for Home Assistant storage."""
        return {
            "stored_energy_kwh": self.stored_energy_kwh,
            "stored_energy_cost_eur": self.stored_energy_cost_eur,
            "total_charging_cost_eur": self.total_charging_cost_eur,
            "total_saved_cost_eur": self.total_saved_cost_eur,
            "previous_charged_energy_kwh": self.previous_charged_energy_kwh,
            "previous_discharged_energy_kwh": self.previous_discharged_energy_kwh,
            "initialized": self.initialized,
        }

    def initialize(
        self,
        charged_energy_kwh: float,
        discharged_energy_kwh: float,
        opening_energy_kwh: float,
    ) -> BatteryCostAccount:
        """Set meter baselines and treat existing battery energy as free."""
        return BatteryCostAccount(
            stored_energy_kwh=max(0.0, opening_energy_kwh),
            previous_charged_energy_kwh=charged_energy_kwh,
            previous_discharged_energy_kwh=discharged_energy_kwh,
            initialized=True,
        )

    def has_positive_meter_delta(
        self,
        charged_energy_kwh: float,
        discharged_energy_kwh: float,
    ) -> bool:
        """Return whether a price is needed to process current meter readings."""
        return (
            self.previous_charged_energy_kwh is not None
            and charged_energy_kwh > self.previous_charged_energy_kwh
        ) or (
            self.previous_discharged_energy_kwh is not None
            and discharged_energy_kwh > self.previous_discharged_energy_kwh
        )

    def record(
        self,
        charged_energy_kwh: float,
        discharged_energy_kwh: float,
        price_per_kwh: float,
        charge_efficiency: float,
        discharge_efficiency: float,
    ) -> BatteryCostAccount:
        """Apply meter deltas at the current EPEX price to the cost ledger."""
        if not self.initialized:
            raise ValueError("Battery cost account must be initialized first")

        charge_delta_kwh = _meter_delta(
            charged_energy_kwh, self.previous_charged_energy_kwh
        )
        discharge_delta_kwh = _meter_delta(
            discharged_energy_kwh, self.previous_discharged_energy_kwh
        )
        stored_energy_kwh = self.stored_energy_kwh
        stored_energy_cost_eur = self.stored_energy_cost_eur
        total_charging_cost_eur = self.total_charging_cost_eur
        total_saved_cost_eur = self.total_saved_cost_eur

        if charge_delta_kwh > _EPSILON:
            charge_cost_eur = charge_delta_kwh / charge_efficiency * price_per_kwh
            stored_energy_kwh += charge_delta_kwh
            stored_energy_cost_eur += charge_cost_eur
            total_charging_cost_eur += charge_cost_eur

        discharged_stored_energy_kwh = min(discharge_delta_kwh, stored_energy_kwh)
        if discharged_stored_energy_kwh > _EPSILON:
            cost_basis_eur = (
                discharged_stored_energy_kwh
                * stored_energy_cost_eur
                / stored_energy_kwh
            )
            avoided_grid_cost_eur = (
                discharged_stored_energy_kwh
                * discharge_efficiency
                * price_per_kwh
            )
            stored_energy_kwh -= discharged_stored_energy_kwh
            stored_energy_cost_eur -= cost_basis_eur
            total_saved_cost_eur += avoided_grid_cost_eur - cost_basis_eur

        return BatteryCostAccount(
            stored_energy_kwh=max(0.0, stored_energy_kwh),
            stored_energy_cost_eur=stored_energy_cost_eur,
            total_charging_cost_eur=total_charging_cost_eur,
            total_saved_cost_eur=total_saved_cost_eur,
            previous_charged_energy_kwh=charged_energy_kwh,
            previous_discharged_energy_kwh=discharged_energy_kwh,
            initialized=True,
        )


def _meter_delta(current_value: float, previous_value: float | None) -> float:
    """Return a positive total-energy delta, treating counter resets as a baseline."""
    if previous_value is None or current_value < previous_value:
        return 0.0
    return current_value - previous_value