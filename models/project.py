from dataclasses import dataclass, field
from typing import List, Optional
from models.bom_item import BomItem


@dataclass
class ProjectItem:
    """Represents one board/BOM inside a larger product project."""
    
    file_path: str
    board_name: str
    board_quantity: int = 1
    bom_items: List[BomItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.file_path.strip():
            raise ValueError("file_path cannot be empty.")
        if not self.board_name.strip():
            raise ValueError("board_name cannot be empty.")
        if self.board_quantity < 1:
            raise ValueError("board_quantity must be at least 1.")


@dataclass
class Project:
    """Represents a full product or project, which may contain multiple boards."""
    
    project_name: str
    board_items: List[ProjectItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.project_name.strip():
            raise ValueError("project_name cannot be empty.")

    def add_board(self, item: ProjectItem) -> None:
        """
        Adds a new board to the project. 
        If a board with the same file_path already exists, it is replaced with the new one.
        """
        for i, existing_item in enumerate(self.board_items):
            if existing_item.file_path == item.file_path:
                self.board_items[i] = item
                return
        
        self.board_items.append(item)

    def remove_board(self, file_path: str) -> None:
        """
        Removes a board from the project matching the given file_path.
        Safely does nothing if no matching board exists.
        """
        self.board_items = [board for board in self.board_items if board.file_path != file_path]

    def get_board_by_file_path(self, file_path: str) -> Optional[ProjectItem]:
        """
        Retrieves a board from the project by its file_path.
        Returns the ProjectItem if found, or None if not found.
        """
        for board in self.board_items:
            if board.file_path == file_path:
                return board
        return None

    def get_all_bom_items(self) -> List[BomItem]:
        """
        Returns a flat list of all raw BomItems across all boards in the project.
        Does not apply board quantity multiplication.
        """
        all_items: List[BomItem] = []
        for board in self.board_items:
            all_items.extend(board.bom_items)
        return all_items
