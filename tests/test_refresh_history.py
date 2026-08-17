import openpyxl

from core.database_manager import DatabaseManager
from core.excel_writer import ExcelWriter
from core.jlcpcb_searcher import SearchWorker
from models.bom_item import BomItem


def test_refresh_records_history_and_reports_only_changed_observations(tmp_path):
    database = DatabaseManager(str(tmp_path / "history.sqlite"))
    database.record_supplier_observation(
        "run-1", "MPN1", "JLCPCB", "C1", 100, 2.0, "JOP"
    )

    item = BomItem(
        mpn="MPN1",
        jlcpcb_part_number="C1",
        available_stock_qty=125,
        unit_price=1.75,
    )
    worker = SearchWorker(
        [item],
        "app",
        "access",
        "secret",
        force_refresh=True,
        _allow_parallel=False,
        _observation_run_id="run-2",
    )
    worker.db_manager = database
    worker._record_supplier_history(item)

    assert len(item.supplier_changes) == 1
    change = item.supplier_changes[0]
    assert change["previous_stock"] == 100
    assert change["current_stock"] == 125
    assert change["stock_change"] == 25
    assert change["previous_unit_price"] == 2.0
    assert change["current_unit_price"] == 1.75
    assert change["unit_price_change"] == -0.25

    history = database.get_supplier_observation_history("mpn1", "jlcpcb")
    assert [entry["run_id"] for entry in history] == ["run-2", "run-1"]

    output_path = tmp_path / "refresh-report.xlsx"
    ExcelWriter([item]).write(str(output_path))
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    sheet = workbook["Refresh Changes"]
    headers = [cell.value for cell in sheet[1]]
    row = [cell.value for cell in sheet[2]]
    assert row[headers.index("MPN")] == "MPN1"
    assert row[headers.index("Supplier")] == "JLCPCB"
    assert row[headers.index("Stock Change")] == 25
    assert row[headers.index("Unit Price Change")] == -0.25


def test_found_then_successful_not_found_is_recorded_and_reported(tmp_path):
    database = DatabaseManager(str(tmp_path / "history.sqlite"))
    database.record_supplier_observation(
        "run-1", "MPN1", "JLCPCB", "C123", 100, 2.0, "JOP", "FOUND"
    )
    item = BomItem(mpn="MPN1", jlcpcb_status="not_found", jlcpcb_source="JOP")
    worker = SearchWorker(
        [item], "app", "access", "secret", force_refresh=True,
        _allow_parallel=False, _observation_run_id="run-2",
    )
    worker.db_manager = database
    worker._record_supplier_history(item)

    history = database.get_supplier_observation_history("MPN1", "JLCPCB")
    assert [row["observation_type"] for row in history] == ["NOT_FOUND", "FOUND"]
    assert history[0]["part_number"] == ""
    assert item.supplier_changes[0]["current_observation_type"] == "NOT_FOUND"

    output_path = tmp_path / "not-found-change.xlsx"
    ExcelWriter([item]).write(str(output_path))
    sheet = openpyxl.load_workbook(output_path)["Refresh Changes"]
    headers = [cell.value for cell in sheet[1]]
    row = [cell.value for cell in sheet[2]]
    assert row[headers.index("Current Result")] == "NOT_FOUND"


def test_api_error_history_is_not_not_found(tmp_path):
    database = DatabaseManager(str(tmp_path / "history.sqlite"))
    item = BomItem(
        mpn="MPN1", jlcpcb_status="error", jlcpcb_error="503",
        jlcpcb_source="JOP",
    )
    worker = SearchWorker(
        [item], "app", "access", "secret", force_refresh=True,
        _allow_parallel=False, _observation_run_id="run-error",
    )
    worker.db_manager = database
    worker._record_supplier_history(item)

    history = database.get_supplier_observation_history("MPN1", "JLCPCB")
    assert history[0]["observation_type"] == "API_ERROR"
    assert history[0]["error_message"] == "503"


def test_history_retention_cleanup_is_configurable(tmp_path):
    database = DatabaseManager(str(tmp_path / "history.sqlite"), history_retention_days=30)
    database.record_supplier_observation("old", "MPN1", "JLCPCB", "C1", 1, 1.0)
    with database._get_connection() as conn:
        conn.execute("UPDATE supplier_observation_history SET observed_at = 0")
        conn.commit()
    database.record_supplier_observation("new", "MPN1", "JLCPCB", "C1", 1, 1.0)
    assert [row["run_id"] for row in database.get_supplier_observation_history("MPN1")] == ["new"]
