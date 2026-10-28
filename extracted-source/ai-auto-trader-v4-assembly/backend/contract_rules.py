"""Fail-closed quantity/notional validation against parsed venue metadata."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from backend.scanner import Contract


@dataclass(frozen=True, slots=True)
class QuantityValidation:
    accepted: bool
    quantity: float | None
    notional: float | None
    reason: str
    orders_authorized: bool = False


def validate_paper_quantity(contract: Contract, *, quantity: float, price: float) -> QuantityValidation:
    """Validate a paper quantity against current known symbol filters.

    Unknown step/min/max filters block sizing. This does not authorize venue
    orders and is not a substitute for exchange-side validation.
    """
    try:
        q, p = Decimal(str(quantity)), Decimal(str(price))
        step = Decimal(str(contract.quantity_step)) if contract.quantity_step is not None else Decimal("0")
        min_q = Decimal(str(contract.min_quantity)) if contract.min_quantity is not None else Decimal("0")
        max_q = Decimal(str(contract.max_quantity)) if contract.max_quantity is not None else Decimal("0")
        min_n = Decimal(str(contract.min_notional)) if contract.min_notional is not None else Decimal("0")
    except (InvalidOperation, ValueError):
        return QuantityValidation(False, None, None, "invalid or missing contract metadata")
    if not all(x.is_finite() for x in (q, p, step, min_q, max_q, min_n)):
        return QuantityValidation(False, None, None, "non-finite quantity, price, or filter")
    if q <= 0 or p <= 0:
        return QuantityValidation(False, None, None, "quantity and price must be positive")
    if step <= 0 or min_q <= 0 or max_q < min_q or min_n <= 0:
        return QuantityValidation(False, None, None, "required step, min/max quantity, or min-notional filter missing")
    rounded = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
    notional = rounded * p
    if rounded != q:
        return QuantityValidation(False, float(rounded), float(notional), "quantity is not aligned to step size")
    if rounded < min_q:
        return QuantityValidation(False, float(rounded), float(notional), "quantity below minimum")
    if rounded > max_q:
        return QuantityValidation(False, float(rounded), float(notional), "quantity above maximum")
    if notional < min_n:
        return QuantityValidation(False, float(rounded), float(notional), "notional below minimum")
    return QuantityValidation(True, float(rounded), float(notional), "filters satisfied; paper only")
