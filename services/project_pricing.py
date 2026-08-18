"""Pure project-scenario pricing calculations used by the results UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
from typing import Optional, Sequence

from core.mpn_utils import parse_positive_integer_quantity
from models.bom_item import BomItem


DEFAULT_SCENARIOS = (1, 10, 100, 1000)


@dataclass(frozen=True)
class PriceTier:
    minimum: int
    maximum: Optional[int]
    price: Decimal


@dataclass(frozen=True)
class SupplierQuote:
    supplier: str
    part_number: str
    currency: str
    minimum_order_quantity: int
    purchase_quantity: int
    unit_price: Decimal
    order_price: Decimal
    price_for_quantity: Decimal
    excess_stock_quantity: int
    excess_stock_cost: Decimal


@dataclass(frozen=True)
class ComponentPricing:
    component_key: str
    mpn: str
    description: str
    quantity_per_project: int
    required_quantity: int
    safety_surplus: int
    target_quantity: int
    quote: Optional[SupplierQuote]
    status: str
    supplier_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioPricing:
    project_quantity: int
    components: tuple[ComponentPricing, ...]
    price_for_quantity_totals: dict[str, Decimal] = field(default_factory=dict)
    order_price_totals: dict[str, Decimal] = field(default_factory=dict)
    per_project_production_totals: dict[str, Decimal] = field(default_factory=dict)
    per_project_order_totals: dict[str, Decimal] = field(default_factory=dict)
    excess_stock_cost_totals: dict[str, Decimal] = field(default_factory=dict)
    priced_count: int = 0
    unpriced_count: int = 0

    @property
    def cost_incomplete(self) -> bool:
        return self.unpriced_count > 0


def _decimal(value: object) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _positive_int(value: object, default: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool):
        return default
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        return default
    return int(number)


def _jlcpcb_tiers(raw: str) -> list[PriceTier]:
    try:
        values = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    tiers: list[PriceTier] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        minimum = _positive_int(value.get("qFrom"))
        maximum_raw = value.get("qTo")
        maximum = None if maximum_raw in (None, "", -1, "-1") else _positive_int(maximum_raw)
        price = _decimal(value.get("price"))
        if minimum is None or price is None or (maximum is not None and maximum < minimum):
            continue
        tiers.append(PriceTier(minimum, maximum, price))
    return sorted(tiers, key=lambda tier: tier.minimum)


def _digikey_tiers(values: Sequence[tuple[int, float]]) -> list[PriceTier]:
    valid: dict[int, Decimal] = {}
    for value in values or []:
        try:
            minimum = _positive_int(value[0])
            price = _decimal(value[1])
        except (IndexError, TypeError):
            continue
        if minimum is not None and price is not None:
            valid[minimum] = min(price, valid.get(minimum, price))
    ordered = sorted(valid.items())
    return [
        PriceTier(minimum, ordered[index + 1][0] - 1 if index + 1 < len(ordered) else None, price)
        for index, (minimum, price) in enumerate(ordered)
    ]


def _quote(
    supplier: str,
    part_number: str,
    stock: object,
    tiers: list[PriceTier],
    configured_moq: object,
    configured_multiple: object,
    currency: str,
    required_quantity: int,
    target_quantity: int,
    supplier_status: str = "",
    supplier_error: str = "",
) -> tuple[Optional[SupplierQuote], str]:
    if supplier_error or supplier_status == "error":
        return None, f"{supplier}: API error ({supplier_error or 'unknown error'})"
    if supplier_status == "preorder":
        return None, f"{supplier}: pre-order is not purchasable"
    if supplier_status == "mismatch":
        return None, f"{supplier}: exact MPN mismatch"
    if supplier_status == "not_found":
        return None, f"{supplier}: part number not found"
    if not str(part_number or "").strip():
        if supplier_status == "not_searched":
            return None, f"{supplier}: unsupported/not configured"
        return None, f"{supplier}: part number not found"
    if not tiers:
        return None, f"{supplier}: no valid price break"
    available = _positive_int(stock, default=0)
    moq = _positive_int(configured_moq) or tiers[0].minimum
    multiple = _positive_int(configured_multiple) or 1
    base_quantity = max(target_quantity, moq)
    purchase_quantity = ((base_quantity + multiple - 1) // multiple) * multiple
    if available is None or available < purchase_quantity:
        available_text = "unknown" if available is None else str(available)
        return None, f"{supplier}: insufficient stock ({available_text}/{purchase_quantity})"
    matching = [
        tier for tier in tiers
        if purchase_quantity >= tier.minimum
        and (tier.maximum is None or purchase_quantity <= tier.maximum)
    ]
    if not matching:
        return None, f"{supplier}: no price tier for purchase quantity {purchase_quantity}"
    tier = max(matching, key=lambda value: value.minimum)
    order_price = tier.price * purchase_quantity
    quantity_price = tier.price * required_quantity
    return SupplierQuote(
        supplier=supplier,
        part_number=str(part_number).strip(),
        currency=str(currency or "USD").strip().upper() or "USD",
        minimum_order_quantity=moq,
        purchase_quantity=purchase_quantity,
        unit_price=tier.price,
        order_price=order_price,
        price_for_quantity=quantity_price,
        excess_stock_quantity=purchase_quantity - required_quantity,
        excess_stock_cost=order_price - quantity_price,
    ), ""


def _component_scenario(component: object, item: BomItem, project_quantity: int) -> ComponentPricing:
    quantity_per_project = parse_positive_integer_quantity(getattr(component, "total_quantity"))
    required = quantity_per_project * project_quantity
    surplus = int(getattr(component, "safety_surplus", 0) or 0)
    target = required + surplus
    quotes: list[SupplierQuote] = []
    reasons: list[str] = []
    jlcpcb_tiers = _jlcpcb_tiers(item.jlcpcb_price_breaks_raw)
    scalar_jlcpcb_price = _decimal(item.unit_price)
    if not jlcpcb_tiers and scalar_jlcpcb_price is not None:
        jlcpcb_tiers = [PriceTier(1, None, scalar_jlcpcb_price)]
    quote, reason = _quote(
        "JLCPCB", item.jlcpcb_part_number, item.available_stock_qty,
        jlcpcb_tiers, item.jlcpcb_min_order_quantity,
        item.jlcpcb_order_multiple, item.jlcpcb_currency, required, target,
        item.jlcpcb_status, item.jlcpcb_error,
    )
    if quote:
        quotes.append(quote)
    else:
        reasons.append(reason)
    digikey_tiers = _digikey_tiers(item.digikey_price_breaks)
    scalar_digikey_price = _decimal(item.digikey_unit_price)
    if not digikey_tiers and scalar_digikey_price is not None:
        digikey_tiers = [PriceTier(1, None, scalar_digikey_price)]
    quote, reason = _quote(
        "DigiKey", item.digikey_part_number, item.digikey_stock_qty,
        digikey_tiers, item.digikey_min_order_quantity,
        item.digikey_order_multiple, item.digikey_currency, required, target,
        item.digikey_status, item.digikey_error,
    )
    if quote:
        quotes.append(quote)
    else:
        reasons.append(reason)
    quote_currencies = {quote.currency for quote in quotes}
    if len(quote_currencies) > 1:
        reasons.extend(
            f"{quote.supplier}: {quote.currency} price cannot be compared without an exchange rate"
            for quote in quotes
        )
        selected = None
    else:
        selected = min(quotes, key=lambda value: (value.order_price, value.supplier)) if quotes else None
    return ComponentPricing(
        component_key=str(getattr(component, "component_key")),
        mpn=item.mpn,
        description=item.description,
        quantity_per_project=quantity_per_project,
        required_quantity=required,
        safety_surplus=surplus,
        target_quantity=target,
        quote=selected,
        status="Priced" if selected else "Unpriced: " + "; ".join(reasons),
        supplier_reasons=tuple(reasons),
    )


def calculate_item_pricing(
    item: BomItem,
    required_quantity: int,
    safety_surplus: int = 0,
    component_key: str = "",
) -> ComponentPricing:
    """Return the canonical supplier choice for one already-scaled component."""
    required = parse_positive_integer_quantity(required_quantity)
    surplus = max(0, int(safety_surplus or 0))
    component = type("PricingComponent", (), {
        "total_quantity": required,
        "safety_surplus": surplus,
        "component_key": component_key or item.mpn or item.comment,
    })()
    return _component_scenario(component, item, 1)


def calculate_project_pricing(
    aggregation_result: object,
    enriched_items: Sequence[BomItem],
    component_keys: Sequence[str],
    scenarios: Sequence[int] = DEFAULT_SCENARIOS,
) -> tuple[ScenarioPricing, ...]:
    """Calculate independent purchasing scenarios from one aggregate BOM."""
    if len(enriched_items) != len(component_keys):
        raise ValueError("Enriched item/component key length mismatch")
    if len(set(component_keys)) != len(component_keys):
        raise ValueError("Duplicate component keys")
    by_key = dict(zip(component_keys, enriched_items))
    results: list[ScenarioPricing] = []
    components = getattr(aggregation_result, "components")
    aggregate_keys = {component.component_key for component in components}
    if aggregate_keys != set(component_keys):
        raise ValueError("Aggregation and enriched component keys do not match")
    for raw_scenario in scenarios:
        scenario = parse_positive_integer_quantity(raw_scenario)
        rows = tuple(
            _component_scenario(component, by_key[component.component_key], scenario)
            for component in components
            if component.component_key in by_key
        )
        price_totals: dict[str, Decimal] = {}
        order_totals: dict[str, Decimal] = {}
        excess_totals: dict[str, Decimal] = {}
        for row in rows:
            if row.quote is None:
                continue
            currency = row.quote.currency
            price_totals[currency] = price_totals.get(currency, Decimal(0)) + row.quote.price_for_quantity
            order_totals[currency] = order_totals.get(currency, Decimal(0)) + row.quote.order_price
            excess_totals[currency] = excess_totals.get(currency, Decimal(0)) + row.quote.excess_stock_cost
        divisor = Decimal(scenario)
        results.append(ScenarioPricing(
            project_quantity=scenario,
            components=rows,
            price_for_quantity_totals=price_totals,
            order_price_totals=order_totals,
            per_project_production_totals={key: value / divisor for key, value in price_totals.items()},
            per_project_order_totals={key: value / divisor for key, value in order_totals.items()},
            excess_stock_cost_totals=excess_totals,
            priced_count=sum(row.quote is not None for row in rows),
            unpriced_count=sum(row.quote is None for row in rows),
        ))
    return tuple(results)
