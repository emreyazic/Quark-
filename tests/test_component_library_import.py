from openpyxl import Workbook

from core.database_manager import DatabaseManager
from ui.main_window import ComponentLibraryImportWorker


def test_component_library_reader_uses_altium_columns_and_skips_invalid_rows(tmp_path):
    path = tmp_path / "library.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Components"
    sheet.append(["LIBRARYREFERENCE", "MANUFACTURER PART NUMBER", "DIGI-KEY PART NUMBER"])
    sheet.append(["CAP010001", "GRM123", "OLD-DK"])
    sheet.append(["CAP010002", "*", "-"])
    sheet.append(["", "VALID-BUT-NO-CODE", ""])
    sheet.append(["CAP010001", "GRM123", "OLD-DK"])
    workbook.save(path)

    worker = ComponentLibraryImportWorker(str(path), DatabaseManager(str(tmp_path / "db.sqlite")))
    components, skipped = worker._read_components()

    assert components == [("CAP010001", "GRM123")]
    assert skipped == 3


def test_component_library_reader_accepts_comment_as_internal_code(tmp_path):
    path = tmp_path / "library.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["COMMENT", "MANUFACTURER PART NUMBER"])
    sheet.append(["IC010001", "STM32F103"])
    workbook.save(path)

    worker = ComponentLibraryImportWorker(str(path), DatabaseManager(str(tmp_path / "db.sqlite")))
    components, skipped = worker._read_components()

    assert components == [("IC010001", "STM32F103")]
    assert skipped == 0
