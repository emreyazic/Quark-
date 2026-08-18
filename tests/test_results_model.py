from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import openpyxl
from types import SimpleNamespace

from core.project_excel_writer import ProjectExcelWriter
from models.bom_item import BomItem
from models.project import Project, ProjectItem
from services.project_aggregation import aggregate_project
from services.project_pricing import calculate_project_pricing
from ui.main_window import MainWindow
from ui.results_model import ResultsFilterProxy, ResultsTableModel


def _row(status="Available", supplier="JLCPCB", shortage=False):
    return {
        "display": ("INT-1", "MPN-1", "A component", 4, supplier, "USD 1.00", "USD 4.00", 10, status),
        "search": "int-1 mpn-1 a component c123 dk-123",
        "status": status,
        "supplier": supplier,
        "shortage": shortage,
    }


def test_results_model_uses_no_cell_widgets_and_exposes_canonical_values():
    model = ResultsTableModel()
    model.set_rows([_row()])
    assert model.rowCount() == 1
    assert model.data(model.index(0, 3)) == 4
    assert model.data(model.index(0, 4)) == "JLCPCB"
    assert model.data(model.index(0, 6)) == "USD 4.00"
    assert not hasattr(model, "setCellWidget")
    assert model.data(model.index(0, 6), Qt.ItemDataRole.TextAlignmentRole)


def test_results_filter_searches_all_codes_and_combines_filters():
    model = ResultsTableModel()
    model.set_rows([_row(), _row("Needs Review", "DigiKey", True)])
    proxy = ResultsFilterProxy()
    proxy.setSourceModel(model)
    for term in ("INT-1", "MPN-1", "C123", "DK-123"):
        proxy.set_filters(search=term)
        assert proxy.rowCount() == 2
    proxy.set_filters(status="Needs Review", supplier="DigiKey", only_shortage=True)
    assert proxy.rowCount() == 1


def test_unknown_stock_is_displayed_as_unknown_not_zero():
    row = _row("Needs Review", "Needs Review")
    display = list(row["display"]); display[7] = "—"; row["display"] = tuple(display)
    model = ResultsTableModel(); model.set_rows([row])
    assert model.data(model.index(0, 7)) == "—"


def test_results_and_excel_use_the_same_canonical_supplier_and_total(tmp_path):
    app = QApplication.instance() or QApplication([])
    project = Project("P")
    board = ProjectItem("bom.xlsx", "Board")
    board.bom_items = [BomItem(mpn="X", quantity=2)]
    project.add_board(board)
    aggregation = aggregate_project(project)
    item = BomItem(
        mpn="X", quantity=2, jlcpcb_part_number="C1", available_stock_qty=100,
        jlcpcb_status="found", unit_price=2.0,
        digikey_part_number="DK1", digikey_stock_qty=100,
        digikey_status="found", digikey_unit_price=1.0,
    )
    key = aggregation.components[0].component_key
    canonical = calculate_project_pricing(aggregation, [item], [key], (1,))[0]

    window = MainWindow()
    window._workspace_aggregation_result = aggregation
    window._project_aggregation_result = None
    window._all_items = [item]
    window._search_item_component_keys = [key]
    window._build_quantity = 1
    window._populate_results()
    display = window._results_model.rows[0]["display"]
    assert display[4] == canonical.components[0].quote.supplier == "DigiKey"
    assert display[5] == f"USD {canonical.components[0].quote.unit_price:.6f}"
    assert display[6] == f"USD {canonical.components[0].quote.order_price:.6f}"

    output = tmp_path / "pricing-consistency.xlsx"
    ProjectExcelWriter(project, aggregation, [item], [key], build_multipliers=[1], pricing_mode="project").write(str(output))
    workbook = openpyxl.load_workbook(output, data_only=False)
    summary = next(row for row in workbook["Master Summary"].iter_rows(values_only=True) if row[0] == "1x")
    assert summary[3] == float(canonical.order_price_totals["USD"])
    window.close()
    app.processEvents()


def test_main_window_partial_and_input_summary_lifecycle():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window._input_summary.text() == "Select a BOM file to begin"
    assert not window._partial_banner.isVisible()
    worker = SimpleNamespace(isRunning=lambda: True, cancel=lambda: None)
    window._search_worker = worker
    window._cancel_processing()
    assert not window._partial_banner.isHidden()
    window._new_session()
    assert window._partial_banner.isHidden()
    window._on_files_changed()
    assert window._input_summary.text() == "Select a BOM file to begin"
    window._search_worker = None
    window.close(); app.processEvents()
    reopened = MainWindow()
    assert not reopened._partial_banner.isVisible()
    assert reopened._input_summary.text() == "Select a BOM file to begin"
    reopened.close(); app.processEvents()
