import pytest
from models.project import Project, ProjectItem
from models.workspace import Workspace

def test_workspace_creation():
    ws = Workspace("My Workspace")
    assert ws.workspace_name == "My Workspace"
    assert len(ws.projects) == 0

def test_workspace_empty_name():
    with pytest.raises(ValueError):
        Workspace("")
    with pytest.raises(ValueError):
        Workspace("   ")

def test_add_project():
    ws = Workspace("Test WS")
    p1 = Project("Proj1")
    ws.add_project(p1)
    assert len(ws.projects) == 1
    assert ws.get_project("Proj1") == p1

def test_add_duplicate_project():
    ws = Workspace("Test WS")
    p1 = Project("Proj1")
    p2 = Project(" Proj1 ")
    ws.add_project(p1)
    with pytest.raises(ValueError):
        ws.add_project(p2)

def test_get_project_not_found():
    ws = Workspace("Test WS")
    assert ws.get_project("Unknown") is None

def test_remove_project():
    ws = Workspace("Test WS")
    p1 = Project("Proj1")
    ws.add_project(p1)
    ws.remove_project("Proj1")
    assert len(ws.projects) == 0

def test_remove_nonexistent_project():
    ws = Workspace("Test WS")
    with pytest.raises(ValueError):
        ws.remove_project("Unknown")

def test_rename_project():
    ws = Workspace("Test WS")
    p1 = Project("Proj1")
    ws.add_project(p1)
    ws.rename_project("Proj1", "Proj2")
    assert p1.project_name == "Proj2"
    assert ws.get_project("Proj1") is None
    assert ws.get_project("Proj2") == p1

def test_rename_nonexistent_project():
    ws = Workspace("Test WS")
    with pytest.raises(ValueError):
        ws.rename_project("Unknown", "Proj2")

def test_rename_to_duplicate_project():
    ws = Workspace("Test WS")
    ws.add_project(Project("Proj1"))
    ws.add_project(Project("Proj2"))
    with pytest.raises(ValueError):
        ws.rename_project("Proj1", " Proj2 ")

def test_rename_empty_name():
    ws = Workspace("Test WS")
    ws.add_project(Project("Proj1"))
    with pytest.raises(ValueError):
        ws.rename_project("Proj1", "  ")

def test_rename_same_name():
    ws = Workspace("Test WS")
    ws.add_project(Project("Proj1"))
    ws.rename_project("Proj1", "Proj1") # Should not raise
    assert ws.get_project("Proj1") is not None

def test_add_board_to_project():
    ws = Workspace("Test WS")
    ws.add_project(Project("Proj1"))
    item = ProjectItem(file_path="test.xlsx", board_name="Board1")
    ws.add_board_to_project("Proj1", item)
    p = ws.get_project("Proj1")
    assert len(p.board_items) == 1
    assert p.board_items[0] == item

def test_add_board_to_nonexistent_project():
    ws = Workspace("Test WS")
    item = ProjectItem(file_path="test.xlsx", board_name="Board1")
    with pytest.raises(ValueError):
        ws.add_board_to_project("Unknown", item)

def test_has_boards_and_get_all_boards():
    ws = Workspace("Test WS")
    assert ws.has_boards() is False
    assert len(ws.get_all_boards()) == 0

    ws.add_project(Project("Proj1"))
    assert ws.has_boards() is False
    assert len(ws.get_all_boards()) == 0

    item1 = ProjectItem(file_path="test1.xlsx", board_name="Board1")
    ws.add_board_to_project("Proj1", item1)
    
    assert ws.has_boards() is True
    assert len(ws.get_all_boards()) == 1
    assert ws.get_all_boards() == [item1]

    ws.add_project(Project("Proj2"))
    item2 = ProjectItem(file_path="test2.xlsx", board_name="Board2")
    ws.add_board_to_project("Proj2", item2)

    assert ws.has_boards() is True
    assert len(ws.get_all_boards()) == 2
    assert item1 in ws.get_all_boards()
    assert item2 in ws.get_all_boards()

def test_workspace_initial_projects_duplicate():
    p1 = Project("Proj1")
    p2 = Project("Proj1")
    with pytest.raises(ValueError):
        Workspace("Test WS", projects=[p1, p2])

def test_workspace_initial_projects_whitespace_duplicate():
    p1 = Project("Proj1")
    p2 = Project(" Proj1 ")
    with pytest.raises(ValueError):
        Workspace("Test WS", projects=[p1, p2])

def test_multiple_boards_in_one_project():
    ws = Workspace("Test WS")
    ws.add_project(Project("Proj1"))
    
    item1 = ProjectItem(file_path="board1.xlsx", board_name="Board1")
    item2 = ProjectItem(file_path="board2.xlsx", board_name="Board2")
    item3 = ProjectItem(file_path="board3.xlsx", board_name="Board3")
    
    ws.add_board_to_project("Proj1", item1)
    ws.add_board_to_project("Proj1", item2)
    ws.add_board_to_project("Proj1", item3)
    
    p = ws.get_project("Proj1")
    assert len(p.board_items) == 3
    assert item1 in p.board_items
    assert item2 in p.board_items
    assert item3 in p.board_items
    
    assert len(ws.get_all_boards()) == 3

def test_add_project_normalizes_name():
    ws = Workspace("Test WS")
    p1 = Project(" Proj1 ")
    ws.add_project(p1)
    assert p1.project_name == "Proj1"
    assert ws.projects[0].project_name == "Proj1"

def test_workspace_initial_projects_normalizes_name():
    p1 = Project(" Proj1 ")
    ws = Workspace("Test WS", projects=[p1])
    assert p1.project_name == "Proj1"
    assert ws.projects[0].project_name == "Proj1"
