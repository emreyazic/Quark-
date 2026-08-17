from core.base_excel_writer import BaseExcelWriter
from models.bom_item import BomItem

def test_is_jlcpcb_usable_none_status():
    writer = BaseExcelWriter()
    
    # Missing JLC part
    item1 = BomItem(jlcpcb_part_number="", status=None)
    assert not writer._is_jlcpcb_usable(item1)
    
    # Valid JLC part, None status (should be treated as usable/"found" technically because it's not a failure string)
    item2 = BomItem(jlcpcb_part_number="C123", status=None)
    assert writer._is_jlcpcb_usable(item2)
    
    # Valid JLC part, skip requested
    item3 = BomItem(jlcpcb_part_number="C123", status=None)
    item3.skip_jlcpcb = True
    assert writer._is_jlcpcb_usable(item3)

    # Stock is informational and must not remove a valid price from totals.
    item4 = BomItem(jlcpcb_part_number="C123", status="Insufficient JLCPCB stock")
    assert writer._is_jlcpcb_usable(item4)

def test_get_status_fill():
    writer = BaseExcelWriter()
    
    # Failure strings -> Red
    assert writer._get_status_fill("JLCPCB not found") == writer.fill_red
    assert writer._get_status_fill("Error connecting") == writer.fill_red
    assert writer._get_status_fill("No exact match") == writer.fill_red
    assert writer._get_status_fill("Insufficient stock") == writer.fill_red
    
    # Success strings -> Green
    assert writer._get_status_fill("✅ Found") == writer.fill_green
    assert writer._get_status_fill("found it") == writer.fill_green
    assert writer._get_status_fill("", has_jlcpcb_part=True) == writer.fill_green
    
    # Warning/Unknown -> Yellow
    assert writer._get_status_fill("Some other status") == writer.fill_yellow
    assert writer._get_status_fill("Warning: something") == writer.fill_yellow
    
    # Empty with no JLC part -> None (no fill)
    assert writer._get_status_fill("", has_jlcpcb_part=False) is None
    assert writer._get_status_fill(None, has_jlcpcb_part=False) is None


def test_project_pricing_returns_tier_unit_and_extended_totals():
    writer = BaseExcelWriter(pricing_mode="project")
    item = BomItem(
        pricing_quantity=15,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 5.0}, {"qFrom": 10, "price": 3.5}]',
        digikey_price_breaks=[(1, 5.0), (10, 3.5)],
    )

    quantity, j_price, d_price, j_total, d_total = writer._component_price_values(item)

    assert quantity == 15
    assert j_price == d_price == 3.5
    assert j_total == d_total == 52.5


def test_unit_pricing_uses_base_price_for_extended_totals():
    writer = BaseExcelWriter(pricing_mode="unit")
    item = BomItem(
        pricing_quantity=15,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 5.0}, {"qFrom": 10, "price": 3.5}]',
        digikey_price_breaks=[(1, 5.0), (10, 3.5)],
    )

    _, j_price, d_price, j_total, d_total = writer._component_price_values(item)

    assert j_price == d_price == 5.0
    assert j_total == d_total == 75.0


def test_invalid_pricing_quantity_is_not_promoted_or_truncated():
    writer = BaseExcelWriter(pricing_mode="project")
    item = BomItem(
        pricing_quantity=0,
        unit_price=5.0,
        digikey_unit_price=6.0,
    )

    assert writer._component_price_values(item) == (0, None, None, None, None)
    assert writer._component_price_values(item, 1.5) == (0, None, None, None, None)
    assert writer._component_price_values(item, float("nan")) == (
        0, None, None, None, None
    )


def test_project_cost_summary_falls_back_to_scalar_supplier_prices():
    writer = BaseExcelWriter(pricing_mode="project")
    item = BomItem(
        jlcpcb_part_number="C123",
        unit_price=2.0,
        digikey_unit_price=3.0,
        jlcpcb_price_breaks_raw="",
        digikey_price_breaks=[],
    )

    pricing = writer._get_pricing_for_component_item(item, 4)

    assert pricing["j_price"] == 2.0
    assert pricing["d_price"] == 3.0
    assert pricing["jlcpcb_cost"] == 8.0
    assert pricing["combined_cost"] == 8.0
    assert pricing["digikey_only_cost"] == 12.0


def test_jlcpcb_price_remains_usable_when_digikey_has_api_error():
    writer = BaseExcelWriter(pricing_mode="unit")
    item = BomItem(
        jlcpcb_part_number="C123", unit_price=2.0, pricing_quantity=4,
        jlcpcb_status="found", digikey_status="error",
        digikey_error="503 Service Unavailable",
    )
    item.refresh_status()

    pricing = writer._get_pricing_for_component_item(item, 4)

    assert writer._is_jlcpcb_usable(item)
    assert pricing["selected_source"] == "JLCPCB"
    assert pricing["jlcpcb_cost"] == 8.0
    assert pricing["combined_cost"] == 8.0
