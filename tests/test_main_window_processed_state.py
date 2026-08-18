from types import SimpleNamespace

from models.bom_item import BomItem
from models.project import Project, ProjectItem
from models.workspace import Workspace
from services.project_aggregation import aggregate_workspace
from ui.main_window import MainWindow


class _Button:
    def __init__(self):
        self.enabled = None
        self.visible = None

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setVisible(self, visible):
        self.visible = visible

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class _Progress:
    def __init__(self):
        self.cancelled = False
        self._log = SimpleNamespace(appendPlainText=lambda message: None)

    def set_cancelled(self):
        self.cancelled = True


def _processed_state(input_revision=3, processed_revision=3):
    return SimpleNamespace(
        _all_items=[object()],
        _search_item_component_keys=["component"],
        _processed_workspace=object(),
        _workspace_aggregation_result=object(),
        _processed_project=object(),
        _project_aggregation_result=object(),
        _input_revision=input_revision,
        _processed_input_revision=processed_revision,
    )


def test_clear_processed_state_removes_items_and_aggregation_snapshot():
    state = _processed_state()

    MainWindow._clear_processed_state(state)

    assert state._all_items == []
    assert state._search_item_component_keys == []
    assert state._processed_workspace is None
    assert state._workspace_aggregation_result is None
    assert state._processed_project is None
    assert state._project_aggregation_result is None
    assert state._processed_input_revision is None


def test_changed_input_revision_invalidates_refresh_snapshot():
    current = _processed_state(input_revision=4, processed_revision=4)
    stale = _processed_state(input_revision=5, processed_revision=4)

    assert MainWindow._has_processed_bom(current)
    assert not MainWindow._has_processed_bom(stale)


def test_refresh_stock_prices_reuses_snapshot_with_force_refresh():
    calls = []
    state = _processed_state()
    state._search_worker = None
    state._has_processed_bom = lambda: MainWindow._has_processed_bom(state)
    state._start_search_worker = lambda force_refresh: calls.append(force_refresh)

    MainWindow._refresh_stock_prices(state)

    assert calls == [True]


def test_cancel_processing_cancels_worker_and_updates_ui():
    worker = SimpleNamespace(
        cancelled=False,
        isRunning=lambda: True,
    )
    worker.cancel = lambda: setattr(worker, "cancelled", True)
    progress = _Progress()
    back_button = _Button()
    partial_banner = _Button()
    state = SimpleNamespace(
        _search_worker=worker,
        _progress_widget=progress,
        _btn_back_setup=back_button,
        _partial_banner=partial_banner,
    )

    MainWindow._cancel_processing(state)

    assert worker.cancelled is True
    assert progress.cancelled is True
    assert back_button.visible is True
    assert partial_banner.visible is True


def test_stale_worker_result_is_discarded_after_input_change():
    progress = _Progress()
    back_button = _Button()
    view_button = _Button()
    state = SimpleNamespace(
        _input_revision=8,
        _processed_input_revision=7,
        _all_items=["last-valid-result"],
        _progress_widget=progress,
        _btn_back_setup=back_button,
        _btn_view_results=view_button,
        _validate_ready=lambda: None,
    )

    MainWindow._on_search_finished(state, ["stale-result"])

    assert state._all_items == ["last-valid-result"]
    assert progress.cancelled is True
    assert back_button.visible is True
    assert view_button.visible is False


def test_file_input_change_increments_revision_and_clears_processed_state():
    process_button = _Button()
    refresh_button = _Button()
    map_button = _Button()
    input_summary = SimpleNamespace(text=None)
    input_summary.setText = lambda text: setattr(input_summary, "text", text)
    state = _processed_state(input_revision=3, processed_revision=3)
    state._file_manager = SimpleNamespace(
        has_files=lambda: False,
        get_bom_files=lambda: [],
    )
    state._btn_map_columns = map_button
    state._btn_process = process_button
    state._btn_refresh = refresh_button
    state._input_summary = input_summary
    state._clear_processed_state = lambda: MainWindow._clear_processed_state(state)
    state._validate_ready = lambda: None

    MainWindow._on_files_changed(state)

    assert state._input_revision == 4
    assert state._all_items == []
    assert state._processed_input_revision is None
    assert refresh_button.enabled is False
    assert input_summary.text == "Select a BOM file to begin"


def test_approved_internal_mpn_mapping_is_applied_before_aggregation():
    workspace = Workspace("WS")
    project = Project("P1")
    board = ProjectItem("bom.xlsx", "Board")
    board.bom_items = [
        BomItem(mpn="OLD-MPN", comment="INTERNAL-1", quantity=4),
        BomItem(mpn="NEW-MPN", quantity=6),
    ]
    project.add_board(board)
    workspace.add_project(project)
    state = SimpleNamespace(
        _database_manager=SimpleNamespace(
            get_internal_mapping=lambda code: {
                "comment_code": code,
                "mpn": "NEW-MPN",
                "approved": 1,
            }
        )
    )

    MainWindow._apply_approved_internal_mpn_mappings(state, workspace)
    result = aggregate_workspace(workspace)

    assert board.bom_items[0].mpn == "NEW-MPN"
    assert len(result.components) == 1
    assert result.components[0].component_key == "MPN:NEW-MPN"
    assert result.components[0].total_quantity == 10


def test_pending_internal_mpn_mapping_is_not_applied_before_aggregation():
    workspace = Workspace("WS")
    project = Project("P1")
    board = ProjectItem("bom.xlsx", "Board")
    board.bom_items = [BomItem(mpn="OLD-MPN", comment="INTERNAL-1", quantity=1)]
    project.add_board(board)
    workspace.add_project(project)
    state = SimpleNamespace(
        _database_manager=SimpleNamespace(
            get_internal_mapping=lambda code: {"mpn": "NEW-MPN", "approved": 0}
        )
    )

    MainWindow._apply_approved_internal_mpn_mappings(state, workspace)

    assert board.bom_items[0].mpn == "OLD-MPN"
