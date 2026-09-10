"""Persistent cost accounting for energy stored in a battery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_EPSILON = 1e-9
_ACCOUNTING_VERSION = 3


@dataclass(frozen=True, slots=True)
class BatteryCostAccount:
    """Cost basis and cumulative savings for battery-side energy counters."""

    stored_energy_kwh: float = 0.0
    stored_energy_cost_eur: float = 0.0
    total_charged_kwh: float = 0.0
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

    @property
    def average_charge_price_per_kwh(self) -> float:
        """Return the lifetime average price paid per charged kWh."""
        if self.total_charged_kwh <= _EPSILON:
            return 0.0
        return self.total_charging_cost_eur / self.total_charged_kwh

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> BatteryCostAccount:
        """Restore an account, tolerating a missing or older stored payload."""
        if not data or data.get("accounting_version") != _ACCOUNTING_VERSION:
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
            total_charged_kwh=max(0.0, number("total_charged_kwh")),
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

    def as_dict(self) -> dict[str, float | int | bool | None]:
        """Serialize the account for Home Assistant storage."""
        return {
            "accounting_version": _ACCOUNTING_VERSION,
            "stored_energy_kwh": self.stored_energy_kwh,
            "stored_energy_cost_eur": self.stored_energy_cost_eur,
            "total_charged_kwh": self.total_charged_kwh,
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
        opening_price_per_kwh: float = 0.0,
    ) -> BatteryCostAccount:
        """Set meter baselines and price the energy already in the battery.

        What the opening charge really cost is unknowable, but the current
        market price is a far better estimate than treating it as free: a zero
        basis would understate every average until that energy is discharged.
        """
        stored_energy_kwh = max(0.0, opening_energy_kwh)
        return BatteryCostAccount(
            stored_energy_kwh=stored_energy_kwh,
            stored_energy_cost_eur=stored_energy_kwh * opening_price_per_kwh,
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
        measured_energy_kwh: float | None = None,
    ) -> BatteryCostAccount:
        """Apply real charged and discharged meter deltas to the cost ledger.

        ``measured_energy_kwh`` is the energy the battery actually reports
        holding. Meter deltas alone cannot track it: a round trip charges more
        than it discharges, so the ledger quantity would climb past the
        physical capacity and turn every average into a lifetime figure over
        energy that is not there. Re-anchoring on each update keeps the
        divisor honest whichever side of the inverter the meters sit on.
        """
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
        total_charged_kwh = self.total_charged_kwh
        total_charging_cost_eur = self.total_charging_cost_eur
        total_saved_cost_eur = self.total_saved_cost_eur

        if charge_delta_kwh > _EPSILON:
            charge_cost_eur = charge_delta_kwh * price_per_kwh
            stored_energy_kwh += charge_delta_kwh
            stored_energy_cost_eur += charge_cost_eur
            total_charged_kwh += charge_delta_kwh
            total_charging_cost_eur += charge_cost_eur

        if discharge_delta_kwh > _EPSILON:
            total_saved_cost_eur += discharge_delta_kwh * price_per_kwh

        discharged_stored_energy_kwh = min(discharge_delta_kwh, stored_energy_kwh)
        if discharged_stored_energy_kwh > _EPSILON:
            cost_basis_eur = (
                discharged_stored_energy_kwh
                * stored_energy_cost_eur
                / stored_energy_kwh
            )
            stored_energy_kwh -= discharged_stored_energy_kwh
            stored_energy_cost_eur -= cost_basis_eur

        if measured_energy_kwh is not None:
            stored_energy_kwh, stored_energy_cost_eur = _reanchor(
                stored_energy_kwh, stored_energy_cost_eur, measured_energy_kwh
            )

        return BatteryCostAccount(
            stored_energy_kwh=max(0.0, stored_energy_kwh),
            stored_energy_cost_eur=stored_energy_cost_eur,
            total_charged_kwh=total_charged_kwh,
            total_charging_cost_eur=total_charging_cost_eur,
            total_saved_cost_eur=total_saved_cost_eur,
            previous_charged_energy_kwh=charged_energy_kwh,
            previous_discharged_energy_kwh=discharged_energy_kwh,
            initialized=True,
        )


def _reanchor(
    stored_energy_kwh: float,
    stored_energy_cost_eur: float,
    measured_energy_kwh: float,
) -> tuple[float, float]:
    """Force the ledger quantity onto the measured one, keeping its unit cost.

    Scaling the cost with the quantity leaves the weighted average untouched,
    so correcting the amount of energy never silently reprices it.
    """
    measured_energy_kwh = max(0.0, measured_energy_kwh)
    if stored_energy_kwh <= _EPSILON:
        return measured_energy_kwh, 0.0
    return (
        measured_energy_kwh,
        stored_energy_cost_eur * measured_energy_kwh / stored_energy_kwh,
    )


def _meter_delta(current_value: float, previous_value: float | None) -> float:
    """Return a positive total-energy delta, treating counter resets as a baseline."""
    if previous_value is None or current_value < previous_value:
        return 0.0
    return current_value - previous_value
