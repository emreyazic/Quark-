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


def test_column_mapping_duplicate_detection():
    # Valid distinct mapping
    valid_map = ColumnMapping(mpn=0, quantity=1, description=2)
    assert valid_map.has_duplicate_mappings() is False
    assert valid_map.is_valid() is True

    # Duplicate mapping: MPN and Quantity share column 0
    dup_map1 = ColumnMapping(mpn=0, quantity=0)
    assert dup_map1.has_duplicate_mappings() is True
    assert dup_map1.is_valid() is False
    assert 0 in dup_map1.get_duplicate_fields()
    assert set(dup_map1.get_duplicate_fields()[0]) == {"mpn", "quantity"}

    # Duplicate mapping: MPN and Description share column 0
    dup_map2 = ColumnMapping(mpn=0, quantity=1, description=0)
    assert dup_map2.has_duplicate_mappings() is True
    assert dup_map2.is_valid() is False


def test_parser_rejects_duplicate_column_mapping(tmp_path):
    import openpyxl
    from models.bom_item import BomFile

    path = tmp_path / "test.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Part & Qty", "Other"])
    ws.append(["MPN-100", "Data"])
    wb.save(path)

    parser = BomParser()
    bom_file = BomFile(
        file_path=str(path),
        board_name="Test",
        sheet_name=ws.title,
        headers=["Part & Qty", "Other"],
        column_mapping=ColumnMapping(mpn=0, quantity=0),  # Duplicate mapping!
    )

    with pytest.raises(ValueError, match="duplicate column mapping detected"):
        parser.parse_bom_items(bom_file)


def test_parser_rejects_incomplete_mapping(tmp_path):
    import openpyxl
    from models.bom_item import BomFile

    path = tmp_path / "test_incomplete.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["MPN", "Desc"])
    ws.append(["MPN-100", "Data"])
    wb.save(path)

    parser = BomParser()
    bom_file = BomFile(
        file_path=str(path),
        board_name="Test",
        sheet_name=ws.title,
        headers=["MPN", "Desc"],
        column_mapping=ColumnMapping(mpn=0, quantity=None),  # Missing Quantity
    )

    with pytest.raises(ValueError, match="Column mapping is incomplete"):
        parser.parse_bom_items(bom_file)
