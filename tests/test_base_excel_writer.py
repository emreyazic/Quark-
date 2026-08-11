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
    assert not writer._is_jlcpcb_usable(item3)

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
