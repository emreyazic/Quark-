import os
import pytest
from core.bom_parser import BomParser

SAMPLE_DIR = os.path.dirname(os.path.dirname(__file__))
FILE_1 = os.path.join(SAMPLE_DIR, "Component_Order_List_r3.xlsx")
FILE_2 = os.path.join(SAMPLE_DIR, "XXXX. PCB KART.xlsx")

@pytest.mark.skipif(not os.path.exists(FILE_1), reason="Sample file 1 missing")
def test_parse_component_order_list():
    parser = BomParser()
    bom = parser.load_file(FILE_1)
    
    assert bom.column_mapping.is_valid()
    assert bom.headers[bom.column_mapping.mpn] == "Manufacturer Part Number"
    assert bom.headers[bom.column_mapping.manufacturer] == "Manufacturer"
    assert bom.headers[bom.column_mapping.board_identifier] == "Kart"
    
    items = parser.parse_bom_items(bom)
    assert len(items) > 0
    # First item should have Board A
    assert items[0].board_name == "Board A"

@pytest.mark.skipif(not os.path.exists(FILE_2), reason="Sample file 2 missing")
def test_parse_pcb_kart():
    parser = BomParser()
    bom = parser.load_file(FILE_2)
    
    assert bom.column_mapping.is_valid()
    
    items = parser.parse_bom_items(bom)
    assert len(items) > 0
    
    # Check that MPNs are properly extracted
    assert items[0].mpn == "GRM188R72A104KA35D"
