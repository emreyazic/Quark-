from dataclasses import dataclass, field
from typing import List, Optional

from models.project import Project, ProjectItem


@dataclass
class Workspace:
    """Represents a workspace containing multiple projects."""
    
    workspace_name: str
    projects: List[Project] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.workspace_name.strip():
            raise ValueError("workspace_name cannot be empty.")
            
        # Ensure initial projects list doesn't have duplicates or empty names
        seen_names = set()
        for project in self.projects:
            norm_name = project.project_name.strip()
            if not norm_name:
                raise ValueError("Project names inside a Workspace cannot be empty.")
            if norm_name in seen_names:
                raise ValueError(f"Duplicate project name '{norm_name}' in workspace.")
            seen_names.add(norm_name)
            project.project_name = norm_name

    def add_project(self, project: Project) -> None:
        norm_name = project.project_name.strip()
        if not norm_name:
            raise ValueError("Project names inside a Workspace cannot be empty.")
        if self.get_project(norm_name):
            raise ValueError(f"Duplicate project name '{norm_name}' in workspace.")
        project.project_name = norm_name
        self.projects.append(project)

    def remove_project(self, project_name: str) -> None:
        project = self.get_project(project_name)
        if not project:
            raise ValueError(f"Project '{project_name}' not found.")
        self.projects.remove(project)

    def get_project(self, project_name: str) -> Optional[Project]:
        norm_target = project_name.strip()
        for project in self.projects:
            if project.project_name.strip() == norm_target:
                return project
        return None

    def rename_project(self, old_name: str, new_name: str) -> None:
        norm_new_name = new_name.strip()
        if not norm_new_name:
            raise ValueError("New project name cannot be empty.")
            
        project = self.get_project(old_name)
        if not project:
            raise ValueError(f"Project '{old_name}' not found.")
            
        if old_name.strip() == norm_new_name:
            # If renaming "Proj1" to " Proj1 ", we can just update the string to the stripped version
            project.project_name = norm_new_name
            return
            
        if self.get_project(norm_new_name):
            raise ValueError(f"Duplicate project name '{norm_new_name}' in workspace.")
            
        project.project_name = norm_new_name

    def add_board_to_project(self, project_name: str, project_item: ProjectItem) -> None:
        project = self.get_project(project_name)
        if not project:
            raise ValueError(f"Project '{project_name}' not found.")
        project.add_board(project_item)

    def get_all_boards(self) -> List[ProjectItem]:
        boards: List[ProjectItem] = []
        for project in self.projects:
            boards.extend(project.board_items)
        return boards

    def has_boards(self) -> bool:
        for project in self.projects:
            if project.board_items:
                return True
        return False
