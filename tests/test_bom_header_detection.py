import openpyxl
import pytest
from core.bom_parser import BomParser
from models.bom_item import BomFile, ColumnMapping


def test_bom_header_detection_with_metadata_rows(tmp_path):
    file_path = str(tmp_path / "metadata_bom.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOM"

    # Row 1: Company Title
    ws.append(["Acme Corporation - Hardware Division"])
    # Row 2: Document Info
    ws.append(["Project: SuperSensor", "Rev: 2.1", "Author: Engineering", "Date: 2026-08-17"])
    # Row 3: Blank
    ws.append([])
    # Row 4: Actual Header Row
    ws.append(["Item #", "Designator", "Manufacturer Part Number", "Quantity", "Description", "Footprint"])
    # Row 5+: Data rows
    ws.append([1, "R1, R2", "RC0603FR-0710KL", 2, "RES 10K OHM 1%", "0603"])
    ws.append([2, "C1", "GRM188R71C104KA01D", 1, "CAP 100NF 16V", "0603"])
    ws.append([3, "U1", "STM32F401RET6", 1, "MCU 32BIT", "LQFP-64"])

    wb.save(file_path)
    wb.close()

    parser = BomParser()
    bom_file = parser.load_file(file_path)

    assert bom_file.header_row_index == 4
    assert bom_file.row_count == 3
    assert "Manufacturer Part Number" in bom_file.headers
    assert "Quantity" in bom_file.headers
    assert bom_file.column_mapping is not None
    assert bom_file.column_mapping.is_valid()
    assert bom_file.column_mapping.mpn == 2
    assert bom_file.column_mapping.quantity == 3

    # Parse items
    items = parser.parse_bom_items(bom_file)
    assert len(items) == 3
    assert items[0].mpn == "RC0603FR-0710KL"
    assert items[0].quantity == 2
    assert items[1].mpn == "GRM188R71C104KA01D"
    assert items[1].quantity == 1
    assert items[2].mpn == "STM32F401RET6"
    assert items[2].quantity == 1
