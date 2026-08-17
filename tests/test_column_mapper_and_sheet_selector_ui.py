import pytest
from PyQt6.QtWidgets import QApplication

from models.bom_item import BomFile, ColumnMapping
from ui.column_mapper_widget import ColumnMapperDialog
from ui.sheet_selector_dialog import SheetSelectorDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_column_mapper_dialog_disallows_duplicate_columns(qapp):
    bom_file = BomFile(
        file_path="sample.xlsx",
        board_name="sample",
        sheet_name="Sheet1",
        headers=["Part Number", "Qty", "Description"],
        preview_rows=[["MPN-1", "1", "Test Part"]],
        column_mapping=ColumnMapping(mpn=0, quantity=1),
        row_count=1,
    )

    dlg = ColumnMapperDialog(bom_file)

    # Initial state should be valid
    assert dlg._validate() is True
    assert dlg._btn_ok.isEnabled() is True
    assert dlg._error_label.isHidden() is True

    # Now set both MPN and Quantity to column 0 ("Part Number")
    # Finding index for column 0 in quantity combo
    qty_combo = dlg._combos["quantity"]
    for i in range(qty_combo.count()):
        if qty_combo.itemData(i) == 0:
            qty_combo.setCurrentIndex(i)
            break

    # Now validation should fail because of duplicate column 0
    assert dlg._validate() is False
    assert dlg._btn_ok.isEnabled() is False
    assert dlg._error_label.isHidden() is False
    assert "mapped to multiple fields" in dlg._error_label.text()

    # Now map Quantity back to column 1 ("Qty")
    for i in range(qty_combo.count()):
        if qty_combo.itemData(i) == 1:
            qty_combo.setCurrentIndex(i)
            break

    assert dlg._validate() is True
    assert dlg._btn_ok.isEnabled() is True
    assert dlg._error_label.isHidden() is True


def test_sheet_selector_dialog_unselects_duplicates_by_default(qapp):
    sheet1 = BomFile(
        file_path="multi.xlsx",
        board_name="Board 1",
        sheet_name="Board 1",
        headers=["MPN", "Qty"],
        preview_rows=[["PART-A", "2"]],
        column_mapping=ColumnMapping(mpn=0, quantity=1),
        row_count=1,
        is_valid=True,
    )
    sheet2 = BomFile(
        file_path="multi.xlsx",
        board_name="Board 1 (Copy)",
        sheet_name="Board 1 (Copy)",
        headers=["MPN", "Qty"],
        preview_rows=[["PART-A", "2"]],
        column_mapping=ColumnMapping(mpn=0, quantity=1),
        row_count=1,
        is_valid=True,
        duplicate_of="Board 1",
    )
    sheet3 = BomFile(
        file_path="multi.xlsx",
        board_name="Board 2",
        sheet_name="Board 2",
        headers=["MPN", "Qty"],
        preview_rows=[["PART-B", "5"]],
        column_mapping=ColumnMapping(mpn=0, quantity=1),
        row_count=1,
        is_valid=True,
    )

    dlg = SheetSelectorDialog([sheet1, sheet2, sheet3])

    # Check default selections: sheet1 and sheet3 should be checked, sheet2 (duplicate) should be unchecked
    assert dlg._checkboxes[0].isChecked() is True
    assert dlg._checkboxes[1].isChecked() is False
    assert dlg._checkboxes[2].isChecked() is True

    selected = dlg.get_selected_sheets()
    assert len(selected) == 2
    assert selected[0].sheet_name == "Board 1"
    assert selected[1].sheet_name == "Board 2"

    # Select all
    dlg._select_all()
    assert all(cb.isChecked() for cb in dlg._checkboxes)
    assert len(dlg.get_selected_sheets()) == 3

    # Deselect all
    dlg._deselect_all()
    assert all(not cb.isChecked() for cb in dlg._checkboxes)
    assert len(dlg.get_selected_sheets()) == 0
    assert dlg._btn_ok.isEnabled() is False

    # Select non-duplicates
    dlg._select_non_duplicates()
    assert dlg._checkboxes[0].isChecked() is True
    assert dlg._checkboxes[1].isChecked() is False
    assert dlg._checkboxes[2].isChecked() is True
    assert len(dlg.get_selected_sheets()) == 2
