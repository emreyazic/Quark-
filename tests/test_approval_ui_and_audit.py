import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLineEdit, QPushButton, QMessageBox

from core.database_manager import DatabaseManager
from ui.approval_dialog import ApprovalDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def mock_message_boxes(monkeypatch):
    """Prevent QMessageBox modal popups from blocking headless tests."""
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)


# ==============================================================================
# TAB ORDER & BASIC VISIBILITY TESTS
# ==============================================================================

def test_tabs_structure_and_order(tmp_path, qapp):
    db_path = str(tmp_path / "test_tabs.sqlite")
    db = DatabaseManager(db_path)
    dlg = ApprovalDialog(db)

    assert dlg.tabs.count() == 3
    assert dlg.tabs.tabText(0) == "Pending"
    assert dlg.tabs.tabText(1) == "Approved"
    assert dlg.tabs.tabText(2) == "History"


# ==============================================================================
# PENDING TAB TESTS
# ==============================================================================

def test_approve_unchanged_candidate_records_accept_candidate(tmp_path, qapp):
    db_path = str(tmp_path / "test_approve_unchanged.sqlite")
    db = DatabaseManager(db_path)
    db.insert_pending_suggestion("R1", "RC0603", "C98765", "DK100")

    dlg = ApprovalDialog(db)
    assert dlg.table_pending.rowCount() == 2  # JLCPCB + DigiKey rows

    # Find JLCPCB row
    jlc_row = None
    for r in range(dlg.table_pending.rowCount()):
        if dlg.table_pending.item(r, 2).text() == "JLCPCB":
            jlc_row = r
            break
    assert jlc_row is not None

    line_edit = dlg.table_pending.cellWidget(jlc_row, 4)
    assert isinstance(line_edit, QLineEdit)
    assert line_edit.text() == "C98765"
    assert line_edit.property("edited") == "false"

    action_widget = dlg.table_pending.cellWidget(jlc_row, 5)
    btn_approve = action_widget.findChild(QPushButton, "actionApproveBtn")
    assert btn_approve is not None
    btn_approve.click()

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C98765"
    assert mapping["lcsc_approved"] == 1
    assert mapping["lcsc_pending_change"] == 0
    assert mapping["lcsc_status"] == "AUTO_APPROVED"
    assert mapping["digikey_pending_change"] == 1  # DigiKey still pending

    history = db.get_mapping_audit_history(comment_code="R1", supplier="JLCPCB")
    assert len(history) == 1
    assert history[0]["action"] == "ACCEPT_CANDIDATE"
    assert history[0]["candidate_code"] == "C98765"
    assert history[0]["selected_code"] == "C98765"
    assert history[0]["previous_code"] == ""


def test_approve_edited_candidate_records_manual_edit_with_all_fields(tmp_path, qapp):
    db_path = str(tmp_path / "test_approve_edited.sqlite")
    db = DatabaseManager(db_path)
    db.upsert_internal_mapping("R1", "RC0603", "C12345", True, "")
    db.refresh_mapping_codes("R1", "C98765", None)

    dlg = ApprovalDialog(db)
    assert dlg.table_pending.rowCount() == 1

    item_curr = dlg.table_pending.item(0, 3)
    assert item_curr.text() == "C12345"

    line_edit = dlg.table_pending.cellWidget(0, 4)
    assert isinstance(line_edit, QLineEdit)
    assert line_edit.text() == "C98765"

    # User edits the input
    line_edit.setText("C55555")
    assert line_edit.property("edited") == "true"
    assert "Edited: C55555" in line_edit.toolTip()

    action_widget = dlg.table_pending.cellWidget(0, 5)
    btn_approve = action_widget.findChild(QPushButton, "actionApproveBtn")
    btn_approve.click()

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C55555"
    assert mapping["lcsc_approved"] == 1
    assert mapping["lcsc_pending_change"] == 0
    assert mapping["lcsc_status"] == "MANUAL_OVERRIDE"

    history = db.get_mapping_audit_history(comment_code="R1", supplier="JLCPCB")
    assert len(history) == 1
    assert history[0]["action"] == "MANUAL_EDIT"
    assert history[0]["previous_code"] == "C12345"
    assert history[0]["candidate_code"] == "C98765"
    assert history[0]["selected_code"] == "C55555"


def test_approve_empty_or_invalid_code_rejected(tmp_path, qapp):
    db_path = str(tmp_path / "test_approve_empty.sqlite")
    db = DatabaseManager(db_path)
    db.insert_pending_suggestion("R1", "RC0603", "C98765", "")

    dlg = ApprovalDialog(db)
    line_edit = dlg.table_pending.cellWidget(0, 4)
    line_edit.setText("   ")  # Whitespace only

    action_widget = dlg.table_pending.cellWidget(0, 5)
    btn_approve = action_widget.findChild(QPushButton, "actionApproveBtn")
    btn_approve.click()

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == ""
    assert mapping["lcsc_pending_change"] == 1

    with pytest.raises(ValueError, match="cannot be empty"):
        db.approve_supplier_mapping("R1", "JLCPCB", "   ")


def test_keep_preserves_current_and_clears_pending_with_audit(tmp_path, qapp):
    db_path = str(tmp_path / "test_keep.sqlite")
    db = DatabaseManager(db_path)
    db.upsert_internal_mapping("R1", "RC0603", "C12345", True, "")
    db.refresh_mapping_codes("R1", "C98765", None)

    dlg = ApprovalDialog(db)
    assert dlg.table_pending.rowCount() == 1

    action_widget = dlg.table_pending.cellWidget(0, 5)
    btn_keep = action_widget.findChild(QPushButton, "actionKeepBtn")
    assert btn_keep.isEnabled() is True
    btn_keep.click()

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C12345"
    assert mapping["lcsc_approved"] == 1
    assert mapping["lcsc_pending_change"] == 0

    history = db.get_mapping_audit_history(comment_code="R1", supplier="JLCPCB")
    assert len(history) == 1
    assert history[0]["action"] == "KEEP_CURRENT"
    assert history[0]["previous_code"] == "C12345"
    assert history[0]["candidate_code"] == "C98765"
    assert history[0]["selected_code"] == "C12345"


def test_keep_disabled_and_prevented_when_no_current_code(tmp_path, qapp):
    db_path = str(tmp_path / "test_keep_disabled.sqlite")
    db = DatabaseManager(db_path)
    db.insert_pending_suggestion("R1", "RC0603", "C98765", "")

    dlg = ApprovalDialog(db)
    action_widget = dlg.table_pending.cellWidget(0, 5)
    btn_keep = action_widget.findChild(QPushButton, "actionKeepBtn")
    assert btn_keep.isEnabled() is False
    assert "no previously approved code exists" in btn_keep.toolTip()

    with pytest.raises(ValueError, match="no approved JLCPCB code exists"):
        db.keep_supplier_current_mapping("R1", "JLCPCB")


def test_supplier_action_isolation(tmp_path, qapp):
    db_path = str(tmp_path / "test_isolation.sqlite")
    db = DatabaseManager(db_path)
    db.insert_pending_suggestion("R1", "RC0603", "C100", "DK200")

    dlg = ApprovalDialog(db)
    assert dlg.table_pending.rowCount() == 2

    # Find and approve JLCPCB row only
    jlc_row = None
    for r in range(dlg.table_pending.rowCount()):
        if dlg.table_pending.item(r, 2).text() == "JLCPCB":
            jlc_row = r
            break
    assert jlc_row is not None

    action_widget = dlg.table_pending.cellWidget(jlc_row, 5)
    action_widget.findChild(QPushButton, "actionApproveBtn").click()

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C100"
    assert mapping["lcsc_approved"] == 1
    assert mapping["lcsc_pending_change"] == 0

    # DigiKey is completely untouched
    assert mapping["digikey_code"] == ""
    assert mapping["digikey_approved"] == 0
    assert mapping["digikey_pending_change"] == 1

    # Dialog table should now have only 1 row remaining (DigiKey)
    assert dlg.table_pending.rowCount() == 1
    assert dlg.table_pending.item(0, 2).text() == "DigiKey"


def test_closing_dialog_without_approve_does_not_modify_db(tmp_path, qapp):
    db_path = str(tmp_path / "test_close_no_save.sqlite")
    db = DatabaseManager(db_path)
    db.insert_pending_suggestion("R1", "RC0603", "C98765", "")

    dlg = ApprovalDialog(db)
    line_edit = dlg.table_pending.cellWidget(0, 4)
    line_edit.setText("C99999_MODIFIED")
    dlg.close()

    db_fresh = DatabaseManager(db_path)
    mapping = db_fresh.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == ""
    assert mapping["lcsc_pending_change"] == 1
    assert mapping["last_found_lcsc"] == "C98765"


# ==============================================================================
# APPROVED TAB TESTS
# ==============================================================================

def test_only_jlcpcb_approved_record_listed_correctly(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_jlc.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")

    dlg = ApprovalDialog(db)
    assert dlg.table_approved.rowCount() == 1

    assert dlg.table_approved.item(0, 0).text() == "R1"
    assert dlg.table_approved.item(0, 1).text() == "RC0603"
    assert dlg.table_approved.item(0, 2).text() == "JLCPCB"

    widget = dlg.table_approved.cellWidget(0, 3)
    assert isinstance(widget, QLineEdit)
    assert widget.text() == "C1111"

    assert "Approved: 1" in dlg.lbl_approved_count.text()


def test_dual_supplier_approved_component_appears_as_two_rows(tmp_path, qapp):
    db_path = str(tmp_path / "test_dual_app.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")
    db.approve_supplier_mapping("R1", "DIGIKEY", "DK-2222", "RC0603")

    dlg = ApprovalDialog(db)
    assert dlg.table_approved.rowCount() == 2
    assert "Approved: 2" in dlg.lbl_approved_count.text()

    suppliers = {dlg.table_approved.item(r, 2).text() for r in range(2)}
    assert suppliers == {"JLCPCB", "DigiKey"}


def test_approved_record_remains_when_pending_candidate_exists(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_pending_coexist.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")
    # A newer candidate is found
    db.refresh_mapping_codes("R1", "C9999", None)

    dlg = ApprovalDialog(db)
    # Visible in Pending
    assert dlg.table_pending.rowCount() == 1
    assert dlg.table_pending.item(0, 3).text() == "C1111"
    assert dlg.table_pending.cellWidget(0, 4).text() == "C9999"

    # Also still active in Approved tab!
    assert dlg.table_approved.rowCount() == 1
    assert dlg.table_approved.cellWidget(0, 3).text() == "C1111"


def test_approved_search_filters_by_code_mpn_and_approved_code(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_search.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RES-10K")
    db.approve_supplier_mapping("C1", "JLCPCB", "C2222", "CAP-100NF")
    db.approve_supplier_mapping("U1", "DIGIKEY", "DK-3333", "STM32F4")

    dlg = ApprovalDialog(db)
    assert dlg.table_approved.rowCount() == 3

    def get_row_by_code(code):
        for r in range(dlg.table_approved.rowCount()):
            if dlg.table_approved.item(r, 0).text() == code:
                return r
        return -1

    row_r1 = get_row_by_code("R1")
    row_c1 = get_row_by_code("C1")
    row_u1 = get_row_by_code("U1")

    # Search by Internal Code
    dlg.search_approved.setText("R1")
    assert not dlg.table_approved.isRowHidden(row_r1)
    assert dlg.table_approved.isRowHidden(row_c1)
    assert dlg.table_approved.isRowHidden(row_u1)
    assert "Approved: 1 of 3" in dlg.lbl_approved_count.text()

    # Search by MPN
    dlg.search_approved.setText("CAP-100NF")
    assert dlg.table_approved.isRowHidden(row_r1)
    assert not dlg.table_approved.isRowHidden(row_c1)
    assert dlg.table_approved.isRowHidden(row_u1)

    # Search by Approved Code
    dlg.search_approved.setText("DK-3333")
    assert dlg.table_approved.isRowHidden(row_r1)
    assert dlg.table_approved.isRowHidden(row_c1)
    assert not dlg.table_approved.isRowHidden(row_u1)


def test_approved_supplier_filter_all_jlcpcb_digikey(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_filter.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")
    db.approve_supplier_mapping("R2", "DIGIKEY", "DK-2222", "RC0603")

    dlg = ApprovalDialog(db)
    assert dlg.table_approved.rowCount() == 2

    def get_row_by_code(code):
        for r in range(dlg.table_approved.rowCount()):
            if dlg.table_approved.item(r, 0).text() == code:
                return r
        return -1

    row_r1 = get_row_by_code("R1")
    row_r2 = get_row_by_code("R2")

    # Filter JLCPCB
    dlg.combo_supplier_filter.setCurrentText("JLCPCB")
    assert not dlg.table_approved.isRowHidden(row_r1)
    assert dlg.table_approved.isRowHidden(row_r2)

    # Filter DigiKey
    dlg.combo_supplier_filter.setCurrentText("DigiKey")
    assert dlg.table_approved.isRowHidden(row_r1)
    assert not dlg.table_approved.isRowHidden(row_r2)

    # Filter All
    dlg.combo_supplier_filter.setCurrentText("All Suppliers")
    assert not dlg.table_approved.isRowHidden(row_r1)
    assert not dlg.table_approved.isRowHidden(row_r2)


def test_approved_code_edit_and_save_updates_only_target_supplier(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_edit_save.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")
    db.approve_supplier_mapping("R1", "DIGIKEY", "DK-ORIG", "RC0603")

    dlg = ApprovalDialog(db)

    # Find JLCPCB row in approved table
    jlc_row = None
    for r in range(dlg.table_approved.rowCount()):
        if dlg.table_approved.item(r, 2).text() == "JLCPCB":
            jlc_row = r
            break
    assert jlc_row is not None

    line_edit = dlg.table_approved.cellWidget(jlc_row, 3)
    action_widget = dlg.table_approved.cellWidget(jlc_row, 5)
    btn_save = action_widget.findChild(QPushButton, "actionSaveBtn")
    btn_cancel = action_widget.findChild(QPushButton, "actionCancelBtn")

    assert btn_save.isEnabled() is False
    assert btn_cancel.isEnabled() is False

    # Edit code
    line_edit.setText("C9999_EDITED")
    assert btn_save.isEnabled() is True
    assert btn_cancel.isEnabled() is True
    assert line_edit.property("edited") == "true"

    # Click Save
    btn_save.click()

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C9999_EDITED"
    assert mapping["lcsc_approved"] == 1
    assert mapping["lcsc_status"] == "MANUAL_OVERRIDE"

    # DigiKey is untouched
    assert mapping["digikey_code"] == "DK-ORIG"
    assert mapping["digikey_approved"] == 1


def test_approved_save_creates_manual_edit_audit_record(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_audit.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")

    dlg = ApprovalDialog(db)
    line_edit = dlg.table_approved.cellWidget(0, 3)
    line_edit.setText("C8888")

    action_widget = dlg.table_approved.cellWidget(0, 5)
    action_widget.findChild(QPushButton, "actionSaveBtn").click()

    history = db.get_mapping_audit_history(comment_code="R1", supplier="JLCPCB")
    # 2 records: initial approve (ACCEPT_CANDIDATE or MANUAL_EDIT) + new MANUAL_EDIT
    assert history[0]["action"] == "MANUAL_EDIT"
    assert history[0]["previous_code"] == "C1111"
    assert history[0]["selected_code"] == "C8888"


def test_approved_cancel_reverts_edit_without_saving_to_db(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_cancel.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")

    dlg = ApprovalDialog(db)
    line_edit = dlg.table_approved.cellWidget(0, 3)
    line_edit.setText("C_UNSAVED")

    action_widget = dlg.table_approved.cellWidget(0, 5)
    btn_save = action_widget.findChild(QPushButton, "actionSaveBtn")
    btn_cancel = action_widget.findChild(QPushButton, "actionCancelBtn")

    btn_cancel.click()
    assert line_edit.text() == "C1111"
    assert btn_save.isEnabled() is False
    assert btn_cancel.isEnabled() is False
    assert line_edit.property("edited") == "false"

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C1111"


def test_approved_empty_or_invalid_code_cannot_be_saved(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_empty.sqlite")
    db = DatabaseManager(db_path)
    db.approve_supplier_mapping("R1", "JLCPCB", "C1111", "RC0603")

    dlg = ApprovalDialog(db)
    line_edit = dlg.table_approved.cellWidget(0, 3)
    line_edit.setText("   ")

    action_widget = dlg.table_approved.cellWidget(0, 5)
    btn_save = action_widget.findChild(QPushButton, "actionSaveBtn")
    btn_save.click()

    # DB not updated
    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "C1111"

    with pytest.raises(ValueError, match="cannot be empty"):
        db.update_approved_supplier_code("R1", "JLCPCB", "   ")


def test_approved_long_codes_accessible_and_copyable(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_long.sqlite")
    db = DatabaseManager(db_path)
    long_code = "LONG_SUPPLIER_PART_NUMBER_ABCDEF1234567890_XYZ"
    db.approve_supplier_mapping("R1", "JLCPCB", long_code, "MPN_VERY_LONG")

    dlg = ApprovalDialog(db)
    line_edit = dlg.table_approved.cellWidget(0, 3)
    assert line_edit.text() == long_code
    assert line_edit.toolTip() == long_code

    line_edit.selectAll()
    assert line_edit.selectedText() == long_code


def test_approved_at_missing_shows_dash_no_fake_date(tmp_path, qapp):
    db_path = str(tmp_path / "test_app_no_date.sqlite")
    db = DatabaseManager(db_path)
    # Insert raw row without approved_at
    db.insert_pending_suggestion("R1", "RC0603", "C1", "")
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE internal_mappings SET lcsc_code = 'C1', lcsc_approved = 1, lcsc_approved_at = NULL WHERE comment_code = 'R1'"
        )
        conn.commit()

    dlg = ApprovalDialog(db)
    assert dlg.table_approved.rowCount() == 1
    assert dlg.table_approved.item(0, 4).text() == "—"


# ==============================================================================
# HISTORY TAB TESTS
# ==============================================================================

def test_history_tab_displays_records_and_search(tmp_path, qapp):
    db_path = str(tmp_path / "test_hist_tab.sqlite")
    db = DatabaseManager(db_path)
    db.record_mapping_audit("R1", "MPN1", "JLCPCB", "", "C1", "C1", "ACCEPT_CANDIDATE")
    db.record_mapping_audit("R2", "MPN2", "DIGIKEY", "DK1", "DK2", "DK1", "KEEP_CURRENT")
    db.record_mapping_audit("R3", "MPN3", "JLCPCB", "C2", "C3", "C4", "MANUAL_EDIT")

    dlg = ApprovalDialog(db)
    assert dlg.table_history.rowCount() == 3

    # Newest first
    actions = [dlg.table_history.item(r, 7).text() for r in range(3)]
    assert actions == ["Edited", "Kept", "Approved"]

    # Search filter
    dlg.search_history.setText("DIGIKEY")
    assert not dlg.table_history.isRowHidden(1)
    assert dlg.table_history.isRowHidden(0)
    assert dlg.table_history.isRowHidden(2)
    assert "Records: 1 of 3" in dlg.lbl_history_count.text()
