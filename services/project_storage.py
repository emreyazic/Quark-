import copy
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Union

from models.project import Project, ProjectItem
from models.workspace import Workspace
from core.atomic_io import atomic_write_json

def project_to_dict(project: Project) -> Dict[str, Any]:
    """
    Converts a Project object into a JSON-serializable dictionary.
    Serializes only the project layout (project name, board metadata),
    not the parsed BOM data.
    """
    return {
        "version": 1,
        "project_name": project.project_name,
        "board_items": [
            {
                "file_path": item.file_path,
                "board_name": item.board_name,
                "board_quantity": item.board_quantity,
            }
            for item in project.board_items
        ],
    }

def project_from_dict(data: Dict[str, Any]) -> Project:
    """
    Converts a dictionary loaded from JSON back into a Project object.
    Restores project metadata only. Does not populate bom_items.
    """
    if not isinstance(data, dict):
        raise ValueError("Project data must be a dictionary.")

    if "version" not in data:
        raise ValueError("Missing 'version' in project data.")
    
    if data["version"] != 1:
        raise ValueError(f"Unsupported project version: {data['version']}")

    if "project_name" not in data or not str(data["project_name"]).strip():
        raise ValueError("Invalid or missing 'project_name' in project data.")

    if "board_items" not in data or not isinstance(data["board_items"], list):
        raise ValueError("Invalid or missing 'board_items' in project data.")

    project = Project(project_name=data["project_name"])
    
    for item_data in data["board_items"]:
        if not isinstance(item_data, dict):
            raise ValueError("Each board item must be a dictionary.")
            
        if "file_path" not in item_data or "board_name" not in item_data or "board_quantity" not in item_data:
            raise ValueError("Board item is missing required fields (file_path, board_name, board_quantity).")
            
        project_item = ProjectItem(
            file_path=item_data["file_path"],
            board_name=item_data["board_name"],
            board_quantity=item_data["board_quantity"]
        )
        project.add_board(project_item)

    return project

def save_project(project: Project, output_path: str) -> None:
    """
    Saves a Project object to a JSON file.
    Creates parent directories if they do not exist.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    data = project_to_dict(project)
    
    atomic_write_json(data, path)

def load_project(input_path: str) -> Project:
    """
    Loads a project JSON file and returns a Project object.
    Loads project layout only, does not parse BOM files.
    """
    path = Path(input_path)
    
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
        
    return project_from_dict(data)

def workspace_to_dict(
    workspace: Workspace,
    workspace_dir: Union[str, Path, None] = None,
) -> Dict[str, Any]:
    """
    Converts a Workspace object into a JSON-serializable dictionary.
    Serializes only the workspace and project layouts, not parsed BOM data.
    """
    base_dir = Path(workspace_dir).resolve() if workspace_dir is not None else None

    def serialize_board(item: ProjectItem) -> Dict[str, Any]:
        file_path = item.file_path
        path_is_relative = False
        if base_dir is not None:
            source_path = Path(file_path)
            if source_path.exists():
                try:
                    file_path = os.path.relpath(source_path.resolve(), base_dir)
                    path_is_relative = True
                except ValueError:
                    file_path = str(source_path.resolve())

        result = {
            "file_path": file_path,
            "board_name": item.board_name,
            "board_quantity": item.board_quantity,
        }
        if path_is_relative:
            result["file_path_relative"] = True
        return result

    return {
        "version": 2,
        "workspace_name": workspace.workspace_name,
        "projects": [
            {
                "project_name": project.project_name,
                "board_items": [serialize_board(item) for item in project.board_items],
            }
            for project in workspace.projects
        ],
    }

def workspace_from_dict(data: Dict[str, Any]) -> Workspace:
    """
    Converts a dictionary loaded from JSON back into a Workspace object.
    Supports version 2 format and falls back to version 1 single-project format.
    """
    if not isinstance(data, dict):
        raise ValueError("Workspace data must be a dictionary.")

    version = data.get("version")
    if version not in (1, 2):
        raise ValueError(f"Unsupported workspace version: {version}")

    if version == 1:
        # Load as single project and wrap in workspace
        project = project_from_dict(data)
        return Workspace(workspace_name=project.project_name, projects=[project])

    # Version 2 processing
    workspace_name = data.get("workspace_name")
    if not workspace_name or not str(workspace_name).strip():
        raise ValueError("Invalid or missing 'workspace_name' in workspace data.")

    projects_data = data.get("projects")
    if not isinstance(projects_data, list):
        raise ValueError("Invalid or missing 'projects' list in workspace data.")

    workspace = Workspace(workspace_name=str(workspace_name).strip())

    for proj_data in projects_data:
        if not isinstance(proj_data, dict):
            raise ValueError("Each project entry must be a dictionary.")
            
        proj_name = proj_data.get("project_name")
        if not proj_name or not str(proj_name).strip():
            raise ValueError("Invalid or missing 'project_name' in project entry.")
            
        project = Project(project_name=str(proj_name).strip())
        
        if "board_items" not in proj_data or not isinstance(proj_data["board_items"], list):
            raise ValueError(f"Invalid or missing 'board_items' in project '{proj_name}'.")
        board_items_data = proj_data["board_items"]
            
        for item_data in board_items_data:
            if not isinstance(item_data, dict):
                raise ValueError("Each board item must be a dictionary.")
                
            if "file_path" not in item_data or "board_name" not in item_data or "board_quantity" not in item_data:
                raise ValueError("Board item is missing required fields (file_path, board_name, board_quantity).")
                
            project_item = ProjectItem(
                file_path=str(item_data["file_path"]),
                board_name=str(item_data["board_name"]),
                board_quantity=item_data["board_quantity"]
            )
            project.add_board(project_item)
            
        workspace.add_project(project)
        
    return workspace

def save_workspace(workspace: Workspace, output_path: Union[str, Path]) -> None:
    """
    Saves a Workspace object to a JSON file.
    Creates parent directories if they do not exist.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    data = workspace_to_dict(workspace, path.parent)
    
    atomic_write_json(data, path)

def load_workspace(input_path: Union[str, Path]) -> Workspace:
    """
    Loads a workspace JSON file (version 1 or 2) and returns a Workspace object.
    Loads project layouts only, does not parse BOM files.
    """
    path = Path(input_path)
    
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    data = copy.deepcopy(data)
    for project_data in data.get("projects", []):
        for item_data in project_data.get("board_items", []):
            if item_data.get("file_path_relative"):
                item_data["file_path"] = str(
                    (path.parent / item_data["file_path"]).resolve()
                )

    return workspace_from_dict(data)


def export_workspace_package(
    workspace: Workspace, output_path: Union[str, Path]
) -> None:
    """Create a portable ZIP containing workspace.json and every source BOM."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = workspace_to_dict(workspace)
    archived_by_source: dict[str, str] = {}
    used_archive_names: set[str] = set()

    def safe_name(name: str) -> str:
        cleaned = re.sub(r'[^A-Za-z0-9._ -]+', "_", name).strip(" .")
        return cleaned or "bom.xlsx"

    source_entries: list[tuple[Path, str]] = []
    for project, project_data in zip(workspace.projects, data["projects"]):
        for board, board_data in zip(
            project.board_items, project_data["board_items"]
        ):
            source = Path(board.file_path).resolve()
            if not source.is_file():
                raise FileNotFoundError(
                    f"Cannot package missing BOM file: {board.file_path}"
                )
            source_key = os.path.normcase(str(source))
            archive_name = archived_by_source.get(source_key)
            if archive_name is None:
                base_name = safe_name(source.name)
                candidate = f"bom_files/{base_name}"
                counter = 2
                while candidate.lower() in used_archive_names:
                    stem = Path(base_name).stem
                    suffix = Path(base_name).suffix
                    candidate = f"bom_files/{stem}_{counter}{suffix}"
                    counter += 1
                archive_name = candidate
                archived_by_source[source_key] = archive_name
                used_archive_names.add(candidate.lower())
                source_entries.append((source, archive_name))
            board_data["file_path"] = archive_name
            board_data["file_path_relative"] = True

    fd, temp_path = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=".zip.tmp",
        dir=destination.parent,
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as package:
            package.writestr(
                "workspace.json",
                json.dumps(data, indent=2, ensure_ascii=False),
            )
            package.writestr(
                "README.txt",
                "Extract the complete package, then load workspace.json "
                "from BOM Tool. Do not move BOM files outside bom_files.\n",
            )
            for source, archive_name in source_entries:
                package.write(source, archive_name)
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
