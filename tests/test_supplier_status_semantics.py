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
    assert not item.is_available


def test_digikey_variation_exists_but_no_part_number_is_not_available():
    item = BomItem(mpn="TEST-MPN")
    result = DigiKeySearchResult()
    result.configured = True
    result.found = False
    result.exact_match = True
    result.digikey_part_number = ""  # Empty part number

    enrich_bom_item_digikey(item, result)

    assert item.digikey_status == "not_found"
    assert not item.is_available


def test_ui_categorization_and_mismatch_isolation():
    # 1. Available
    item1 = BomItem(mpn="M1", jlcpcb_status="found", jlcpcb_part_number="C1")
    # 2. Not Found
    item2 = BomItem(mpn="M2", jlcpcb_status="not_found", digikey_status="not_found")
    item2.refresh_status()
    # 3. Mismatch
    item3 = BomItem(mpn="M3", jlcpcb_status="mismatch", digikey_status="not_found")
    item3.refresh_status()
    # 4. Low stock
    item4 = BomItem(mpn="M4", status="Insufficient JLCPCB stock")
    # 5. Error with mismatch preserved
    item5 = BomItem(mpn="M5", jlcpcb_status="mismatch", digikey_status="error", digikey_error="500 Server Error")
    item5.refresh_status()

    assert item1.get_ui_category() == "available"
    assert item2.get_ui_category() == "not_found"
    assert item3.get_ui_category() == "mismatch"
    assert item4.get_ui_category() == "low_stock"
    assert item5.get_ui_category() == "mismatch"  # Mismatch with API error preserved in status string
    assert "No exact JLCPCB match" in item5.status
    assert "DigiKey API error" in item5.status

    # Mismatch is distinct from not_found
    assert not item3.is_not_found
    assert item2.is_not_found


def test_mixed_sourcing_stock_semantics():
    from core.base_excel_writer import BaseExcelWriter
    writer = BaseExcelWriter()

    # 1. JLCPCB stock=0, DigiKey sufficient -> canonical DigiKey source selected
    item_dk_fallback = BomItem(
        jlcpcb_part_number="C1",
        available_stock_qty=0,
        jlcpcb_status="found",
        digikey_part_number="DK-1",
        digikey_status="found",
        digikey_stock_qty=50,
    )
    source, price = writer._selected_supplier_price(item_dk_fallback, j_price=0.10, d_price=0.50, required_quantity=10)
    assert source == "DigiKey"
    assert price == 0.50

    # 2. Both suppliers insufficient -> Shortage / Unavailable, purchasable total None
    item_shortage = BomItem(
        jlcpcb_part_number="C1",
        available_stock_qty=5,
        jlcpcb_status="found",
        digikey_part_number="DK-1",
        digikey_status="found",
        digikey_stock_qty=8,
    )
    source, price = writer._selected_supplier_price(item_shortage, j_price=0.10, d_price=0.50, required_quantity=10)
    assert source == "Shortage / Unavailable"
    assert price is None

    # 3. Unknown stock (None) -> not considered sufficient
    item_unknown_stock = BomItem(
        jlcpcb_part_number="C1",
        available_stock_qty=None,
        jlcpcb_status="found",
        digikey_part_number="DK-1",
        digikey_status="found",
        digikey_stock_qty=None,
    )
    source, price = writer._selected_supplier_price(item_unknown_stock, j_price=0.10, d_price=0.50, required_quantity=10)
    assert source == "Shortage / Unavailable"
    assert price is None


def test_decoupled_pending_supplier_approvals(tmp_path):
    from core.database_manager import DatabaseManager
    db_path = str(tmp_path / "test.db")
    db = DatabaseManager(db_path)

    # Add mapping with pending for both suppliers
    db.insert_pending_suggestion("IC1", "MPN1", "LCSC_NEW", "DK_NEW")
    m = db.get_internal_mapping("IC1")
    assert m["lcsc_pending_change"] == 1
    assert m["digikey_pending_change"] == 1

    # Approve only JLCPCB
    db.approve_supplier_mapping("IC1", "JLCPCB", "LCSC_NEW", "MPN1")
    m = db.get_internal_mapping("IC1")
    assert m["lcsc_code"] == "LCSC_NEW"
    assert m["lcsc_approved"] == 1
    assert m["lcsc_pending_change"] == 0
    assert m["digikey_code"] == ""  # DigiKey untouched
    assert m["digikey_pending_change"] == 1

    # Reject DigiKey
    db.reject_supplier_pending_change("IC1", "DIGIKEY")
    m = db.get_internal_mapping("IC1")
    assert m["lcsc_code"] == "LCSC_NEW"
    assert m["lcsc_approved"] == 1
    assert m["digikey_code"] == ""
    assert m["digikey_pending_change"] == 0
