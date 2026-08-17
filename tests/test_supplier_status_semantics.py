import pytest
from models.bom_item import BomItem
from core.jlcpcb_searcher import JlcpcbSearchResult, enrich_bom_item, clear_jlcpcb_live_data
from core.digikey_searcher import DigiKeySearchResult, enrich_bom_item_digikey, clear_digikey_live_data


def test_jlcpcb_mismatch_status_preserved():
    item = BomItem(mpn="STM32F401RET6", quantity=10)
    result = JlcpcbSearchResult()
    result.found = False
    result.exact_match = False
    result.match_count = 3  # Near matches found but no exact match

    enrich_bom_item(item, result)

    assert item.jlcpcb_status == "mismatch"
    assert item.status == "No exact JLCPCB match"
    
    # Trigger refresh_status to ensure it doesn't get squashed to generic 'Not Found'
    item.refresh_status()
    assert item.jlcpcb_status == "mismatch"
    assert item.status == "No exact JLCPCB match"
    assert not item.is_available


def test_clear_supplier_live_data():
    item = BomItem(
        mpn="TEST-MPN",
        jlcpcb_status="found",
        jlcpcb_part_number="C123",
        available_stock_qty=500,
        unit_price=0.15,
        jlcpcb_error="old error",
        jlcpcb_source="JOP",
        digikey_status="found",
        digikey_part_number="DK-123",
        digikey_stock_qty=200,
        digikey_unit_price=0.20,
        digikey_error="old dk error",
        digikey_source="DIGIKEY_LIVE",
    )

    clear_jlcpcb_live_data(item)
    assert item.available_stock_qty is None
    assert item.unit_price is None
    assert item.jlcpcb_status == "not_searched"
    assert item.jlcpcb_error == ""
    assert item.jlcpcb_source == ""
    # Part number preserved
    assert item.jlcpcb_part_number == "C123"

    clear_digikey_live_data(item)
    assert item.digikey_stock_qty is None
    assert item.digikey_unit_price is None
    assert item.digikey_status == "not_searched"
    assert item.digikey_error == ""
    assert item.digikey_source == ""
    assert item.digikey_part_number == "DK-123"


def test_digikey_only_found_when_purchasable_variation_exists():
    item = BomItem(mpn="TEST-MPN")
    result = DigiKeySearchResult()
    result.configured = True
    result.found = False
    result.exact_match = True  # MPN matches but no purchasable variation
    result.digikey_part_number = ""

    enrich_bom_item_digikey(item, result)

    assert item.digikey_status == "not_found"
    assert item.digikey_part_number == ""
