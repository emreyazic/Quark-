import pytest
from core.bom_parser import BomParser
from models.bom_item import ColumnMapping

def test_auto_detect_columns_prioritizes_mpn():
    parser = BomParser()
    headers = ["Manufacturer", "Manufacturer Part Number", "Quantity"]
    preview_rows = [["Mfr A", "MPN123", "10"]]
    
    mapping = parser._auto_detect_columns(headers, preview_rows)
    
    assert mapping.manufacturer == 0
    assert mapping.mpn == 1
    assert mapping.quantity == 2

def test_auto_detect_columns_avoids_duplicate_mapping():
    parser = BomParser()
    headers = ["Manufacturer Part Number", "Quantity"]
    preview_rows = [["MPN123", "10"]]
    
    mapping = parser._auto_detect_columns(headers, preview_rows)
    
    assert mapping.mpn == 0
    assert mapping.manufacturer is None  # Should not also claim col 0

def test_validate_mfr_mpn_mapping_swap():
    parser = BomParser()
    headers = ["Manufacturer", "MPN"]
    # We deliberately swap the data: col 0 has MPNs, col 1 has Manufacturers
    preview_rows = [
        ["RC0603FR-0710KL", "Yageo"],
        ["GRM188R72A104KA35D", "Murata Electronics"],
    ]
    mapping = ColumnMapping(manufacturer=0, mpn=1)
    parser._validate_mfr_mpn_mapping(mapping, preview_rows, headers)
    
    # It should have auto-swapped them
    assert mapping.manufacturer == 1
    assert mapping.mpn == 0
    assert len(mapping.warnings) > 0
    assert "Possible column swap detected" in mapping.warnings[0]

def test_kart_column_maps_to_board_identifier():
    parser = BomParser()
    headers = ["Kart", "MPN", "Quantity"]
    preview_rows = [["A", "MPN123", "1"]]
    
    mapping = parser._auto_detect_columns(headers, preview_rows)
    assert mapping.board_identifier == 0
