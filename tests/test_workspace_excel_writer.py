import os
import pytest
import openpyxl

from models.bom_item import BomItem
from models.project import Project, ProjectItem
from models.workspace import Workspace
from services.project_aggregation import aggregate_workspace
from core.workspace_excel_writer import WorkspaceExcelWriter

def test_workbook_structure_and_sheet_names(tmp_path):
    workspace = Workspace("Test WS")
    
    # Project 1
    p1 = Project("P1")
    p1.add_board(ProjectItem("path_a/board.xlsx", "Board A", 1))
    workspace.add_project(p1)
    
    # Project 2 (Same board name to test collision)
    p2 = Project("P2")
    p2.add_board(ProjectItem("path_b/board.xlsx", "Board A", 2))
    workspace.add_project(p2)
    
    agg = aggregate_workspace(workspace)
    writer = WorkspaceExcelWriter(workspace, agg, [], [])
    
    out_path = tmp_path / "out.xlsx"
    writer.write(str(out_path))
    
    wb = openpyxl.load_workbook(out_path)
    names = wb.sheetnames
    
    # Required Sheets
    assert "All Aggregated" in names
    assert "Mutual Components" in names
    
    # Projects
    assert "P P1" in names
    assert "P P2" in names
    
    # Name constraints validation
    for name in names:
        assert len(name) <= 31
        for invalid_char in [':', '\\', '/', '?', '*', '[', ']']:
            assert invalid_char not in name

def test_strict_validation(tmp_path):
    workspace = Workspace("Test")
    p1 = Project("P1")
    b = ProjectItem("path", "B", 1)
    b.bom_items = [BomItem(mpn="R1", quantity=1)]
    p1.add_board(b)
    workspace.add_project(p1)
    
    agg = aggregate_workspace(workspace)
    c_keys = [c.component_key for c in agg.components]
    
    enriched = [BomItem(mpn="R1")]
    
    # Success
    WorkspaceExcelWriter(workspace, agg, enriched, c_keys)
    
    # Length mismatch
    with pytest.raises(ValueError, match="Length mismatch"):
        WorkspaceExcelWriter(workspace, agg, enriched, c_keys + ["extra"])
        
    # Duplicates in component keys
    with pytest.raises(ValueError, match="Duplicate component keys"):
        WorkspaceExcelWriter(workspace, agg, enriched + enriched, c_keys + c_keys)
        
    # Missing / Extra (Mismatched from aggregation)
    with pytest.raises(ValueError, match="Component key mismatch"):
        WorkspaceExcelWriter(workspace, agg, enriched, ["WRONG_KEY"])


def test_mutual_components_sheet(tmp_path):
    workspace = Workspace("Test")
    
    p1 = Project("P1")
    b1 = ProjectItem("path1", "B1", 1)
    b1.bom_items = [
        BomItem(mpn="MUTUAL", quantity=2), 
        BomItem(mpn="P1_ONLY", quantity=1)
    ]
    p1.add_board(b1)
    workspace.add_project(p1)
    
    p2 = Project("P2")
    b2 = ProjectItem("path2", "B2", 3)
    b2.bom_items = [
        BomItem(mpn="MUTUAL", quantity=1)
    ]
    p2.add_board(b2)
    workspace.add_project(p2)
    
    agg = aggregate_workspace(workspace)
    c_keys = [c.component_key for c in agg.components]
    enriched = []
    for k in c_keys:
        if "MUTUAL" in k:
            enriched.append(BomItem(mpn="MUTUAL", manufacturer="MFG"))
        else:
            enriched.append(BomItem(mpn="P1_ONLY"))
            
    writer = WorkspaceExcelWriter(workspace, agg, enriched, c_keys, build_multipliers=[1])
    out_path = tmp_path / "out.xlsx"
    writer.write(str(out_path))
    
    wb = openpyxl.load_workbook(out_path)
    assert "Mutual Components" in wb.sheetnames
    
    ws = wb["Mutual Components"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    
    # MUTUAL now merged into 1 row
    assert len(rows) == 1
    
    headers = [cell.value for cell in ws[1]]
    board_col_idx = headers.index("Board Names")
    qty_col_idx = headers.index("Quantity (Per Board / Total)")
    mpn_col_idx = headers.index("MPN")

    assert rows[0][mpn_col_idx] == "MUTUAL"
    
    assert rows[0][board_col_idx] == "B1\nB2"
    assert rows[0][qty_col_idx] == "2 / 2\n1 / 3"



def test_pricing_fallback_and_unpriced(tmp_path):
    workspace = Workspace("Test")
    p1 = Project("P1")
    b = ProjectItem("path", "B", 1)
    b.bom_items = [
        BomItem(mpn="UNPRICED", quantity=1),
        BomItem(mpn="FALLBACK", quantity=1),
    ]
    p1.add_board(b)
    workspace.add_project(p1)
    
    agg = aggregate_workspace(workspace)
    c_keys = [c.component_key for c in agg.components]
    
    enriched = []
    for k in c_keys:
        if "UNPRICED" in k:
            enriched.append(BomItem(mpn="UNPRICED", status="Not found"))
        else:
            enriched.append(BomItem(
                mpn="FALLBACK", 
                jlcpcb_part_number="C1",
                status="Insufficient JLCPCB stock",
                jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 0.10}]',
                digikey_price_breaks=[(1, 0.50)]
            ))
            
    writer = WorkspaceExcelWriter(workspace, agg, enriched, c_keys, build_multipliers=[1])
    out_path = tmp_path / "out.xlsx"
    writer.write(str(out_path))
    
    wb = openpyxl.load_workbook(out_path)
    ws = wb["All Aggregated"]
    
    # Headers
    headers = [cell.value for cell in ws[1]]
    
    # Regression test for absent bloated headers
    assert "Selected Source @ 5x" not in headers
    assert "Selected Source @ 1x" not in headers
    assert "JLCPCB Extended Cost @ 5x" not in headers
    
    mpn_col_idx = headers.index("MPN")
    jlc_part_col_idx = headers.index("JLCPCB Part Number")
    status_col_idx = headers.index("Status")
    
    found_unpriced = False
    found_fallback = False
    
    for row in ws.iter_rows(min_row=2, values_only=True):
        mpn = row[mpn_col_idx]
        if mpn == "UNPRICED":
            assert row[status_col_idx] == "Not found"
            found_unpriced = True
        elif mpn == "FALLBACK":
            assert row[jlc_part_col_idx] == "C1"
            assert row[status_col_idx] == "Insufficient JLCPCB stock"
            found_fallback = True
            
    assert found_unpriced
    assert found_fallback

def test_workspace_multiboard_quantity_formatting_and_auto_width(tmp_path):
    long_desc = "Precision resistor network for controller power measurement"
    workspace = Workspace("Test")
    
    project = Project("P1")
    board_a = ProjectItem("path_a", "Board A", 2)
    board_a.bom_items = [
        BomItem(mpn="R1", quantity=2.0, description=long_desc, designator="R1")
    ]
    board_b = ProjectItem("path_b", "Board B", 1)
    board_b.bom_items = [
        BomItem(mpn="R1", quantity="3.0", description=long_desc, designator="R2")
    ]
    project.add_board(board_a)
    project.add_board(board_b)
    workspace.add_project(project)
    
    agg = aggregate_workspace(workspace)
    c_keys = [c.component_key for c in agg.components]
    enriched = [
        BomItem(mpn="R1", description=long_desc)
    ]
    
    writer = WorkspaceExcelWriter(workspace, agg, enriched, c_keys, build_multipliers=[1])
    out_path = tmp_path / "out.xlsx"
    writer.write(str(out_path))
    
    wb = openpyxl.load_workbook(out_path)
    all_ws = wb["All Aggregated"]
    assert all_ws["A1"].value == "Board Name"
    assert all_ws["A2"].value == "Board A"
    assert all_ws["C2"].value == "R1"
    assert all_ws["D2"].value == "2 / 4"
    assert all_ws["A3"].value == "Board B"
    assert all_ws["C3"].value == "R2"
    assert all_ws["D3"].value == "3 / 3"
    assert all_ws.column_dimensions["B"].width == 35
    
    project_ws = wb["P P1"]
    project_rows = list(project_ws.iter_rows(values_only=True))
    project_component_row_a = next(row for row in project_rows if row[0] == "Board A")
    assert project_component_row_a[2] == "R1"
    assert project_component_row_a[3] == "2 / 4"
    
    project_component_row_b = next(row for row in project_rows if row[0] == "Board B")
    assert project_component_row_b[2] == "R2"
    assert project_component_row_b[3] == "3 / 3"
    

