import openpyxl

from core.excel_writer import UnavailableReportWriter
from models.bom_item import BomItem


def test_digikey_found_item_is_not_unavailable_when_jlcpcb_is_not_found():
    items = [
        BomItem(
            mpn="DK-ONLY",
            status="JLCPCB not found",
            digikey_part_number="DK123-ND",
        ),
        BomItem(
            mpn="JLC-WARNING",
            status="Warning [JLCPCB_OPENAPI_V2_CONFLICT]",
            jlcpcb_part_number="C123",
        ),
        BomItem(mpn="MISSING", status="JLCPCB not found"),
        BomItem(mpn="NO-STATUS-BUT-MISSING", status=""),
    ]

    writer = UnavailableReportWriter(items)

    assert [item.mpn for item in writer.unavailable_items] == [
        "MISSING",
        "NO-STATUS-BUT-MISSING",
    ]


def test_unavailable_workbook_contains_only_items_missing_from_both_suppliers(tmp_path):
    writer = UnavailableReportWriter([
        BomItem(mpn="DK-ONLY", status="JLCPCB not found", digikey_part_number="DK1"),
        BomItem(mpn="MISSING", status="JLCPCB not found"),
    ])
    output_path = tmp_path / "unavailable.xlsx"

    writer.write(str(output_path))

    workbook = openpyxl.load_workbook(output_path)
    rows = list(workbook["Action Required"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1
    assert rows[0][4] == "MISSING"
