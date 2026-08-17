import tempfile
import json
import shutil
import zipfile
from pathlib import Path
import pytest
from models.project import Project, ProjectItem
from models.workspace import Workspace
from services.project_storage import (
    project_to_dict,
    project_from_dict,
    save_project,
    load_project,
    workspace_to_dict,
    workspace_from_dict,
    save_workspace,
    load_workspace,
    export_workspace_package,
)

def test_project_storage():
    # 1. Create a sample project
    project = Project(project_name="Test Product")
    project.add_board(ProjectItem(
        file_path="/tmp/tx.xlsx",
        board_name="Tx Card",
        board_quantity=2
    ))
    project.add_board(ProjectItem(
        file_path="/tmp/rx.xlsx",
        board_name="Rx Card",
        board_quantity=8
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_file = Path(tmpdir) / "test_project.json"
        
        # 2. Save the project
        save_project(project, str(temp_file))
        
        # Verify file exists and has correct basic structure
        assert temp_file.exists()
        
        with temp_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["version"] == 1
            assert data["project_name"] == "Test Product"
            assert len(data["board_items"]) == 2

        # 3. Load the project back
        loaded_project = load_project(str(temp_file))
        
        # 4. Verify preserved properties
        assert loaded_project.project_name == "Test Product"
        assert len(loaded_project.board_items) == 2
        
        board_1 = loaded_project.board_items[0]
        assert board_1.file_path == "/tmp/tx.xlsx"
        assert board_1.board_name == "Tx Card"
        assert board_1.board_quantity == 2
        assert len(board_1.bom_items) == 0
        
        board_2 = loaded_project.board_items[1]
        assert board_2.file_path == "/tmp/rx.xlsx"
        assert board_2.board_name == "Rx Card"
        assert board_2.board_quantity == 8
        assert len(board_2.bom_items) == 0

def test_workspace_round_trip():
    ws = Workspace("Multi Project Workspace")
    
    p1 = Project("Proj1")
    p1.add_board(ProjectItem("/path/b1.xlsx", "Board1", 1))
    p1.add_board(ProjectItem("/path/b2.xlsx", "Board2", 2))
    
    p2 = Project("Proj2")
    p2.add_board(ProjectItem("/path/b3.xlsx", "Board3", 3))
    
    p3 = Project("Proj3")
    
    ws.add_project(p1)
    ws.add_project(p2)
    ws.add_project(p3)

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_file = Path(tmpdir) / "test_workspace.json"
        
        save_workspace(ws, temp_file)
        assert temp_file.exists()
        
        loaded_ws = load_workspace(temp_file)
        assert loaded_ws.workspace_name == "Multi Project Workspace"
        assert len(loaded_ws.projects) == 3
        
        loaded_p1 = loaded_ws.get_project("Proj1")
        assert loaded_p1 is not None
        assert len(loaded_p1.board_items) == 2
        assert loaded_p1.board_items[0].file_path == "/path/b1.xlsx"
        assert loaded_p1.board_items[0].board_quantity == 1
        
        loaded_p2 = loaded_ws.get_project("Proj2")
        assert loaded_p2 is not None
        assert len(loaded_p2.board_items) == 1
        
        loaded_p3 = loaded_ws.get_project("Proj3")
        assert loaded_p3 is not None
        assert len(loaded_p3.board_items) == 0

def test_load_workspace_version_1():
    # Simulate loading an old project
    data = {
        "version": 1,
        "project_name": "Old Project",
        "board_items": [
            {
                "file_path": "/path/old.xlsx",
                "board_name": "Old Board",
                "board_quantity": 5
            }
        ]
    }
    
    ws = workspace_from_dict(data)
    assert ws.workspace_name == "Old Project"
    assert len(ws.projects) == 1
    p = ws.projects[0]
    assert p.project_name == "Old Project"
    assert len(p.board_items) == 1
    assert p.board_items[0].board_name == "Old Board"

def test_workspace_from_dict_validation():
    # Not a dict
    with pytest.raises(ValueError):
        workspace_from_dict([])

    # Missing version
    with pytest.raises(ValueError):
        workspace_from_dict({"workspace_name": "Test"})

    # Invalid version
    with pytest.raises(ValueError):
        workspace_from_dict({"version": 99, "workspace_name": "Test"})

    # Missing workspace_name in v2
    with pytest.raises(ValueError):
        workspace_from_dict({"version": 2, "projects": []})

    # Missing projects list
    with pytest.raises(ValueError):
        workspace_from_dict({"version": 2, "workspace_name": "Test"})

    # Missing board_items in project
    with pytest.raises(ValueError):
        workspace_from_dict({
            "version": 2,
            "workspace_name": "Test",
            "projects": [
                {"project_name": "P1"}
            ]
        })

    # Duplicate project names
    with pytest.raises(ValueError):
        workspace_from_dict({
            "version": 2,
            "workspace_name": "Test",
            "projects": [
                {"project_name": "P1", "board_items": []},
                {"project_name": "P1", "board_items": []}
            ]
        })

    # Invalid board_quantity (0 is invalid via ProjectItem __post_init__)
    with pytest.raises(ValueError):
        workspace_from_dict({
            "version": 2,
            "workspace_name": "Test",
            "projects": [
                {
                    "project_name": "P1",
                    "board_items": [
                        {
                            "file_path": "/path.xlsx",
                            "board_name": "B1",
                            "board_quantity": 0
                        }
                    ]
                }
            ]
        })

def test_load_workspace_version_1_from_disk():
    project = Project("Old Project")
    project.add_board(ProjectItem("/path/old.xlsx", "Old Board", 5))
    
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_file = Path(tmpdir) / "test_old_project.json"
        save_project(project, str(temp_file))
        
        ws = load_workspace(temp_file)
        assert ws.workspace_name == "Old Project"
        assert len(ws.projects) == 1
        assert ws.projects[0].project_name == "Old Project"
        assert len(ws.projects[0].board_items) == 1
        assert ws.projects[0].board_items[0].board_quantity == 5

def test_workspace_to_dict_format():
    ws = Workspace("WS1")
    p1 = Project("P1")
    p1.add_board(ProjectItem("/p1.xlsx", "B1", 1))
    ws.add_project(p1)
    
    data = workspace_to_dict(ws)
    assert data["version"] == 2
    assert data["workspace_name"] == "WS1"
    assert len(data["projects"]) == 1
    assert data["projects"][0]["project_name"] == "P1"
    assert len(data["projects"][0]["board_items"]) == 1
    assert data["projects"][0]["board_items"][0]["file_path"] == "/p1.xlsx"
    assert data["projects"][0]["board_items"][0]["board_name"] == "B1"
    assert data["projects"][0]["board_items"][0]["board_quantity"] == 1


def test_saved_workspace_paths_follow_workspace_when_folder_is_moved(tmp_path):
    package = tmp_path / "package"
    bom_dir = package / "boms"
    bom_dir.mkdir(parents=True)
    bom_path = bom_dir / "main.xlsx"
    bom_path.write_bytes(b"placeholder")

    workspace = Workspace("Portable")
    project = Project("Main")
    project.add_board(ProjectItem(str(bom_path), "Main Board", 1))
    workspace.add_project(project)

    workspace_path = package / "workspace.json"
    save_workspace(workspace, workspace_path)
    stored = json.loads(workspace_path.read_text(encoding="utf-8"))
    stored_board = stored["projects"][0]["board_items"][0]
    assert stored_board["file_path"] == str(Path("boms") / "main.xlsx")
    assert stored_board["file_path_relative"] is True

    moved_package = tmp_path / "moved" / "package"
    moved_package.parent.mkdir()
    shutil.move(str(package), str(moved_package))

    loaded = load_workspace(moved_package / "workspace.json")
    assert loaded.projects[0].board_items[0].file_path == str(
        (moved_package / "boms" / "main.xlsx").resolve()
    )


def test_portable_workspace_package_contains_loadable_relative_boms(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    shared_bom = source_dir / "shared.xlsx"
    shared_bom.write_bytes(b"xlsx-placeholder")

    workspace = Workspace("Portable")
    for project_name in ("Project A", "Project B"):
        project = Project(project_name)
        project.add_board(ProjectItem(str(shared_bom), f"{project_name} Board", 1))
        workspace.add_project(project)

    package_path = tmp_path / "portable.zip"
    export_workspace_package(workspace, package_path)

    with zipfile.ZipFile(package_path) as package:
        names = package.namelist()
        assert "workspace.json" in names
        assert "README.txt" in names
        assert names.count("bom_files/shared.xlsx") == 1
        extract_dir = tmp_path / "extracted"
        package.extractall(extract_dir)

    loaded = load_workspace(extract_dir / "workspace.json")
    expected_path = str((extract_dir / "bom_files" / "shared.xlsx").resolve())
    assert loaded.projects[0].board_items[0].file_path == expected_path
    assert loaded.projects[1].board_items[0].file_path == expected_path
