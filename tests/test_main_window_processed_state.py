from types import SimpleNamespace

from ui.main_window import MainWindow


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
