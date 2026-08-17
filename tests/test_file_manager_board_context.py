from types import SimpleNamespace

from models.bom_item import BomFile, BomItem
from ui.file_manager_widget import FileManagerWidget


class _Parser:
    def __init__(self):
        self.seen_files = []

    def parse_bom_items(self, bom_file):
        self.seen_files.append(bom_file)
        return [BomItem(mpn="ABC123", board_name=bom_file.board_name)]


def test_shared_file_is_parsed_into_independent_board_item_contexts():
    parser = _Parser()
    manager = SimpleNamespace(_parser=parser)
    cached_file = BomFile(file_path="shared.xlsx", board_name="Cache Name")

    project_a_items = FileManagerWidget._parse_items_for_board(
        manager, cached_file, "Project A Board"
    )
    project_b_items = FileManagerWidget._parse_items_for_board(
        manager, cached_file, "Project B Board"
    )

    assert cached_file.board_name == "Cache Name"
    assert parser.seen_files[0] is not parser.seen_files[1]
    assert project_a_items is not project_b_items
    assert project_a_items[0] is not project_b_items[0]
    assert project_a_items[0].board_name == "Project A Board"
    assert project_b_items[0].board_name == "Project B Board"


def test_contextual_bom_file_does_not_mutate_path_cache():
    cached_file = BomFile(file_path="shared.xlsx", board_name="Cache Name")
    manager = SimpleNamespace(_parsed_boms={"shared.xlsx": cached_file})

    contextual = FileManagerWidget.get_bom_file_for_board(
        manager, "shared.xlsx", "Project Board"
    )

    assert contextual is not cached_file
    assert contextual.board_name == "Project Board"
    assert cached_file.board_name == "Cache Name"
