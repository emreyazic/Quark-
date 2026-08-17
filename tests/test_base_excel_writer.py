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
