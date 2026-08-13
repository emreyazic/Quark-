import openpyxl

from core.excel_writer import ExcelWriter
from models.bom_item import BomItem


def test_output_contains_digikey_code_and_separate_supplier_rows(tmp_path):
    item = BomItem(
        mpn="MPN1",
        comment="IC1",
        quantity=5,
        required_stock=60,
        jlcpcb_part_number="C123",
        digikey_part_number="DK-MPN1-CT-ND",
        available_stock_qty=100,
        digikey_stock_qty=200,
        unit_price=0.10,
        digikey_unit_price=0.20,
        status="Found",
    )
    path = tmp_path / "output.xlsx"
    ExcelWriter([item]).write(str(path))

    workbook = openpyxl.load_workbook(path, data_only=False)
    enriched = workbook["Enriched BOM"]
    headers = [cell.value for cell in enriched[1]]
    row = [cell.value for cell in enriched[2]]
    assert row[headers.index("DigiKey Part Number")] == "DK-MPN1-CT-ND"

    supplier_rows = list(workbook["Supplier Stock"].iter_rows(min_row=2, values_only=True))
    assert len(supplier_rows) == 2
    assert supplier_rows[0][2:6] == ("JLCPCB", "C123", 100, 0.10)
    assert supplier_rows[1][2:6] == ("DigiKey", "DK-MPN1-CT-ND", 200, 0.20)
