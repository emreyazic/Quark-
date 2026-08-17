import openpyxl
import pytest

from core.bom_parser import BomParser


def test_parser_reads_all_visible_bom_sheets_with_independent_mappings(tmp_path):
    path = tmp_path / "multi-sheet.xlsx"
    workbook = openpyxl.Workbook()

    summary = workbook.active
    summary.title = "Summary"
    summary.append(["Project", "Example"])

    board_a = workbook.create_sheet("Board A")
    board_a.append(["MPN", "Quantity", "Description"])
    board_a.append(["MPN-A", 2, "Part A"])

    board_b = workbook.create_sheet("Board B")
    board_b.append(["Description", "Qty", "Manufacturer Part Number"])
    board_b.append(["Part B", 3, "MPN-B"])

    hidden_helper = workbook.create_sheet("Helper")
    hidden_helper.append(["MPN", "Quantity"])
    hidden_helper.append(["SHOULD-NOT-LOAD", 1])
    hidden_helper.sheet_state = "hidden"
    workbook.save(path)

    parser = BomParser()
    bom_file = parser.load_file(str(path))
    items = parser.parse_bom_items(bom_file)

    assert bom_file.sheet_name == "Board A"
    assert [(item.mpn, item.quantity) for item in items] == [
        ("MPN-A", 2),
        ("MPN-B", 3),
    ]


def test_parser_rejects_legacy_xls_with_clear_message(tmp_path):
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"not-an-xls-workbook")

    with pytest.raises(ValueError, match=r"Legacy \.xls files are not supported"):
        BomParser().load_file(str(path))
