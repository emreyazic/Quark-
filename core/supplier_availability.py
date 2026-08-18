"""Canonical supplier availability semantics."""

from __future__ import annotations

from enum import Enum
import re
from typing import Any, Mapping, Optional


class AvailabilityState(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"
    UNKNOWN = "UNKNOWN"


_PREORDER_FIELDS = (
    "is_pre_sale", "isPreSale", "preSale", "is_preorder", "isPreorder",
    "isPreOrder", "preorder", "preOrder",
)
_AVAILABLE_FIELDS = ("is_available", "isAvailable", "is_in_stock", "isInStock", "inStock")
_STATUS_FIELDS = (
    "availabilityStatus", "availability_status", "stockStatus", "stock_status",
    "saleStatus", "sale_status", "productStatus", "product_status", "status",
)


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    token = _token(value)
    if token in {"true", "yes", "y", "1", "preorder", "presale"}:
        return True
    if token in {"false", "no", "n", "0"}:
        return False
    return None


def normalize_availability(
    payload: Optional[Mapping[str, Any]], stock: Any = None
) -> AvailabilityState:
    """Normalize only documented/known status fields; descriptions are ignored."""
    values = payload or {}
    for key in _PREORDER_FIELDS:
        if key in values and _boolean(values.get(key)) is True:
            return AvailabilityState.PREORDER

    for key in _STATUS_FIELDS:
        if key not in values:
            continue
        token = _token(values.get(key))
        if token in {"preorder", "presale", "preorderonly", "presaleonly"}:
            return AvailabilityState.PREORDER
        if token in {"instock", "available", "active", "onsale"}:
            return AvailabilityState.IN_STOCK
        if token in {"outofstock", "unavailable", "soldout", "discontinued"}:
            return AvailabilityState.OUT_OF_STOCK

    for key in _AVAILABLE_FIELDS:
        if key in values:
            available = _boolean(values.get(key))
            if available is not None:
                return AvailabilityState.IN_STOCK if available else AvailabilityState.OUT_OF_STOCK

    try:
        if stock is not None:
            return AvailabilityState.IN_STOCK if int(stock) > 0 else AvailabilityState.OUT_OF_STOCK
    except (TypeError, ValueError):
        pass
    return AvailabilityState.UNKNOWN


def is_preorder(payload: Optional[Mapping[str, Any]]) -> bool:
    return normalize_availability(payload) == AvailabilityState.PREORDER
