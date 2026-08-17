import pytest
from models.bom_item import BomItem
from models.project import Project, ProjectItem
from services.project_aggregation import (
    aggregate_project,
    build_component_key,
    calculate_build_quantities,
    aggregate_workspace,
)


def test_build_component_key():
    # 1. MPN priority
    item1 = BomItem(mpn="12345", manufacturer="Yageo", value="10k", footprint="0603")
    assert build_component_key(item1) == "MPN:12345"

    # 2. JLCPCB priority over Value
    item2 = BomItem(jlcpcb_part_number="C123", value="10k", footprint="0603")
    assert build_component_key(item2) == "JLC:C123"

    # 3. Value + Footprint grouping
    item3 = BomItem(value="10k", footprint="0603")
    item4 = BomItem(value=" 10k ", footprint="0603")
    assert build_component_key(item3) == build_component_key(item4)
    assert build_component_key(item3) == "VAL:10K_PKG:0603_CMT:"

    # Value alone should not group if there's no package or comment
    item5 = BomItem(value="10k")
    item6 = BomItem(value="10k")
    key5 = build_component_key(item5)
    key6 = build_component_key(item6)
    assert key5 != key6
    assert key5.startswith("UNKNOWN:")


def test_build_component_key_stable_unknown():
    # Calling it twice on the same BomItem must yield the exact same key
    item = BomItem(description="")  # Empty
    key1 = build_component_key(item)
    key2 = build_component_key(item)
    
    assert key1 == key2
    assert key1.startswith("UNKNOWN:")
    
def test_build_component_key_different_unknowns():
    # Two different empty BomItems must get different keys
    item1 = BomItem(description="")
    item2 = BomItem(description="")
    
    key1 = build_component_key(item1)
    key2 = build_component_key(item2)
    
    assert key1 != key2
    assert key1.startswith("UNKNOWN:")
    assert key2.startswith("UNKNOWN:")

def test_single_board_multiplication():
    project = Project("Test Project")
    board = ProjectItem("path1", "Board 1", board_quantity=2)
    board.bom_items = [
        BomItem(mpn="R123", quantity=5),
        BomItem(mpn="C123", quantity=10),
    ]
    project.add_board(board)

    result = aggregate_project(project)
    
    assert len(result.components) == 2
    assert result.skipped_count == 0
    
    comp_r = next(c for c in result.components if c.component_key == "MPN:R123")
    comp_c = next(c for c in result.components if c.component_key == "MPN:C123")
    
    assert comp_r.total_quantity == 10
    assert comp_c.total_quantity == 20


@pytest.mark.parametrize(
    "value",
    [1.5, float("nan"), float("inf"), True, 0, -1],
)
def test_project_item_rejects_invalid_board_quantities(value):
    with pytest.raises(ValueError):
        ProjectItem("path", "Board", board_quantity=value)


@pytest.mark.parametrize(("value", "expected"), [(5, 5), (5.0, 5), ("5", 5), ("5.0", 5)])
def test_project_item_uses_shared_positive_integer_quantity_policy(value, expected):
    board = ProjectItem("path", "Board", board_quantity=value)

    assert board.board_quantity == expected
    assert isinstance(board.board_quantity, int)


def test_multi_board_grouping():
    project = Project("Test Project")
    
    # Board A qty 2, uses 5 of R123
    board_a = ProjectItem("path_a", "Board A", board_quantity=2)
    board_a.bom_items = [BomItem(mpn="R123", quantity=5)]
    
    # Board B qty 8, uses 3 of R123
    board_b = ProjectItem("path_b", "Board B", board_quantity=8)
    board_b.bom_items = [BomItem(mpn="R123", quantity=3)]
    
    project.add_board(board_a)
    project.add_board(board_b)

    result = aggregate_project(project)
    
    assert len(result.components) == 1
    comp = result.components[0]
    
    # 2*5 + 8*3 = 10 + 24 = 34
    assert comp.total_quantity == 34
    assert len(comp.usages) == 2
    assert comp.usages[0].board_quantity == 2
    assert comp.usages[1].board_quantity == 8


def test_exact_mpn_groups_across_different_or_missing_manufacturer_names():
    project = Project("Test Project")

    board_a = ProjectItem("path_a", "Board A", board_quantity=1)
    board_a.bom_items = [
        BomItem(mpn="ABC123", manufacturer="KEMET", quantity=1)
    ]
    board_b = ProjectItem("path_b", "Board B", board_quantity=1)
    board_b.bom_items = [
        BomItem(mpn="abc123", manufacturer="Kemet Electronics", quantity=2)
    ]
    board_c = ProjectItem("path_c", "Board C", board_quantity=1)
    board_c.bom_items = [
        BomItem(mpn="  ABC123  ", manufacturer="", quantity=3)
    ]
    project.add_board(board_a)
    project.add_board(board_b)
    project.add_board(board_c)

    result = aggregate_project(project)

    assert len(result.components) == 1
    assert result.components[0].component_key == "MPN:ABC123"
    assert result.components[0].total_quantity == 6
    assert len(result.components[0].usages) == 3


def test_value_only_items_do_not_falsely_group():
    project = Project("Test Project")
    board = ProjectItem("path1", "Board 1", board_quantity=1)
    
    # These share value but lack footprint/package/mpn
    board.bom_items = [
        BomItem(value="10k", quantity=1),
        BomItem(value="10k", quantity=1),
    ]
    project.add_board(board)

    result = aggregate_project(project)
    
    # They should NOT group
    assert len(result.components) == 2


def test_invalid_quantities_handled_gracefully():
    project = Project("Test Project")
    board = ProjectItem("path1", "Board 1", board_quantity=1)
    
    board.bom_items = [
        BomItem(mpn="R1", quantity=5),
        BomItem(mpn="R2", quantity=""),       # Invalid
        BomItem(mpn="R3", quantity="abc"),    # Invalid
        BomItem(mpn="R4", quantity=None),     # Invalid
        BomItem(mpn="R5", quantity=0),        # Invalid
        BomItem(mpn="R6", quantity=1.5),      # Invalid
        BomItem(mpn="R7", quantity=float("nan")),  # Invalid
        BomItem(mpn="R8", quantity=float("inf")),  # Invalid
    ]
    project.add_board(board)

    result = aggregate_project(project)
    
    assert len(result.components) == 1
    assert result.components[0].component_key == "MPN:R1"
    assert result.skipped_count == 7
    assert len(result.warnings) == 7


def test_calculate_build_quantities():
    project = Project("Test Project")
    board = ProjectItem("path1", "Board 1", board_quantity=2)
    board.bom_items = [BomItem(mpn="R123", quantity=6)]
    project.add_board(board)

    result = aggregate_project(project)
    comp = result.components[0]  # total_quantity = 12
    
    builds = calculate_build_quantities(comp)
    
    assert builds[1] == 12
    assert builds[5] == 60
    assert builds[10] == 120
    assert builds[50] == 600
    assert builds[100] == 1200


from models.workspace import Workspace

def test_aggregate_workspace_multiple_projects():
    ws = Workspace("Test WS")
    
    p1 = Project("P1")
    b1 = ProjectItem("path1", "B1", board_quantity=2)
    b1.bom_items = [BomItem(mpn="R1", quantity=5)]
    p1.add_board(b1)
    ws.add_project(p1)
    
    p2 = Project("P2")
    b2 = ProjectItem("path2", "B2", board_quantity=3)
    b2.bom_items = [BomItem(mpn="C1", quantity=2)]
    p2.add_board(b2)
    ws.add_project(p2)
    
    result = aggregate_workspace(ws)
    assert len(result.components) == 2
    assert len(result.mutual_components) == 0
    assert result.skipped_count == 0
    
    r1 = next(c for c in result.components if c.component_key == "MPN:R1")
    assert r1.total_quantity == 10
    
    c1 = next(c for c in result.components if c.component_key == "MPN:C1")
    assert c1.total_quantity == 6


def test_aggregate_workspace_mutual_component_across_projects():
    ws = Workspace("Test WS")
    
    p1 = Project("P1")
    b1 = ProjectItem("path1", "B1", board_quantity=2)
    b1.bom_items = [BomItem(mpn="R1", quantity=5)]
    p1.add_board(b1)
    ws.add_project(p1)
    
    p2 = Project("P2")
    b2 = ProjectItem("path2", "B2", board_quantity=3)
    b2.bom_items = [BomItem(mpn="R1", quantity=10)]
    p2.add_board(b2)
    ws.add_project(p2)
    
    result = aggregate_workspace(ws)
    assert len(result.components) == 1
    assert len(result.mutual_components) == 1
    
    comp = result.components[0]
    assert comp.total_quantity == 40  # 2*5 + 3*10
    assert len(comp.usages) == 2
    
    mut = result.mutual_components[0]
    assert mut.shared_across_projects is True
    assert mut.project_count == 2
    assert mut.source_location_count == 2


def test_aggregate_workspace_mutual_component_same_project():
    ws = Workspace("Test WS")
    p1 = Project("P1")
    
    b1 = ProjectItem("path1", "B1", board_quantity=1)
    b1.bom_items = [BomItem(mpn="R1", quantity=1)]
    p1.add_board(b1)
    
    b2 = ProjectItem("path2", "B2", board_quantity=1)
    b2.bom_items = [BomItem(mpn="R1", quantity=1)]
    p1.add_board(b2)
    
    ws.add_project(p1)
    
    result = aggregate_workspace(ws)
    assert len(result.mutual_components) == 1
    mut = result.mutual_components[0]
    assert mut.shared_across_projects is False
    assert mut.project_count == 1
    assert mut.source_location_count == 2


def test_aggregate_workspace_quantity_math():
    ws = Workspace("WS")
    p1 = Project("P1")
    b1 = ProjectItem("p1", "B1", board_quantity=5)
    b1.bom_items = [BomItem(mpn="R1", quantity=2)]
    p1.add_board(b1)
    ws.add_project(p1)
    
    result = aggregate_workspace(ws)
    comp = result.components[0]
    assert comp.total_quantity == 10
    assert comp.usages[0].total_quantity == 10
    assert comp.usages[0].board_quantity == 5
    assert comp.usages[0].bom_line_quantity == 2


def test_aggregate_workspace_source_tracking():
    ws = Workspace("WS")
    p1 = Project("ProjectX")
    b1 = ProjectItem("file_x.xlsx", "BoardX", board_quantity=1)
    item = BomItem(mpn="R1", quantity=1)
    b1.bom_items = [item]
    p1.add_board(b1)
    ws.add_project(p1)
    
    result = aggregate_workspace(ws)
    comp = result.components[0]
    usage = comp.usages[0]
    
    assert usage.project_name == "ProjectX"
    assert usage.board_name == "BoardX"
    assert usage.board_file_path == "file_x.xlsx"
    assert usage.item == item
    
    assert comp.source_projects == ["ProjectX"]
    assert comp.source_locations == ["ProjectX::file_x.xlsx::BoardX"]
    assert comp.source_board_names == ["BoardX"]


def test_aggregate_workspace_invalid_quantity():
    ws = Workspace("WS")
    p1 = Project("P1")
    b1 = ProjectItem("p1", "B1", board_quantity=1)
    b1.bom_items = [
        BomItem(mpn="R1", quantity=5),
        BomItem(mpn="R2", quantity=""),
        BomItem(mpn="R3", quantity="abc"),
        BomItem(mpn="R4", quantity=None),
        BomItem(mpn="R5", quantity=0),
        BomItem(mpn="R6", quantity=1.5),
        BomItem(mpn="R7", quantity=float("nan")),
        BomItem(mpn="R8", quantity=float("inf")),
    ]
    p1.add_board(b1)
    ws.add_project(p1)
    
    result = aggregate_workspace(ws)
    assert len(result.components) == 1
    assert result.skipped_count == 7
    assert len(result.warnings) == 7


def test_aggregate_workspace_empty_handling():
    ws = Workspace("Empty WS")
    result = aggregate_workspace(ws)
    assert len(result.components) == 0
    assert len(result.mutual_components) == 0
    
    ws.add_project(Project("Empty Project"))
    result2 = aggregate_workspace(ws)
    assert len(result2.components) == 0


def test_aggregate_workspace_same_file_path():
    ws = Workspace("WS")
    p1 = Project("P1")
    b1 = ProjectItem("shared_path.xlsx", "B1", board_quantity=1)
    b1.bom_items = [BomItem(mpn="R1", quantity=1)]
    p1.add_board(b1)
    ws.add_project(p1)
    
    p2 = Project("P2")
    b2 = ProjectItem("shared_path.xlsx", "B2", board_quantity=1)
    b2.bom_items = [BomItem(mpn="R1", quantity=1)]
    p2.add_board(b2)
    ws.add_project(p2)
    
    result = aggregate_workspace(ws)
    assert len(result.components) == 1
    mut = result.mutual_components[0]
    assert mut.source_location_count == 2
    assert mut.project_count == 2
    assert mut.shared_across_projects is True
