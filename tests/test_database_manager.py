from core.database_manager import DatabaseManager
import sqlite3


def test_managers_for_same_database_share_lock_and_enable_wal(tmp_path):
    path = tmp_path / "shared.sqlite"
    first = DatabaseManager(str(path))
    second = DatabaseManager(str(path))

    assert first._lock is second._lock
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_manual_override_is_not_reopened_when_auto_result_is_unchanged(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    db.insert_pending_suggestion("R1", "MPN1", "C11111", "DK111")
    db.upsert_internal_mapping("R1", "MPN1", "C22222", True, "DK222")

    assert db.refresh_mapping_codes("R1", "C11111", "DK111") is False
    mapping = db.get_internal_mapping("R1")
    assert mapping["approved"] == 1
    assert mapping["lcsc_code"] == "C22222"
    assert mapping["last_found_lcsc"] == "C11111"
    assert mapping["lcsc_status"] == "MANUAL_OVERRIDE"


def test_changed_auto_result_preserves_approved_and_records_review_history(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    db.insert_pending_suggestion("R1", "MPN1", "C11111", "DK111")
    db.upsert_internal_mapping("R1", "MPN1", "C22222", True, "DK222")

    assert db.refresh_mapping_codes("R1", "C33333", "DK111") is True
    mapping = db.get_internal_mapping("R1")
    assert mapping["approved"] == 1
    assert mapping["lcsc_approved"] == 1
    assert mapping["digikey_approved"] == 1
    assert mapping["lcsc_pending_change"] == 1
    assert mapping["digikey_pending_change"] == 0
    assert mapping["lcsc_code"] == "C22222"
    assert mapping["previous_found_lcsc"] == "C11111"
    assert mapping["last_found_lcsc"] == "C33333"
    assert mapping["lcsc_status"] == "PENDING_REVIEW"


def test_not_found_transitions_use_automatic_history(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    db.insert_pending_suggestion("R1", "MPN1", "", "")
    db.upsert_internal_mapping("R1", "MPN1", "MANUAL", True, "")

    assert db.refresh_mapping_codes("R1", "", "") is False
    assert db.get_internal_mapping("R1")["lcsc_code"] == "MANUAL"

    assert db.refresh_mapping_codes("R1", "C123", "") is True
    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "MANUAL"
    assert mapping["previous_found_lcsc"] == ""
    assert mapping["last_found_lcsc"] == "C123"


def test_lcsc_and_digikey_changes_are_tracked_independently(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    db.insert_pending_suggestion("R1", "MPN1", "C1", "DK1")
    db.upsert_internal_mapping("R1", "MPN1", "C1", True, "MANUAL-DK")

    assert db.refresh_mapping_codes("R1", "C1", "DK2") is True
    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_status"] == "AUTO_APPROVED"
    assert mapping["digikey_status"] == "PENDING_REVIEW"
    assert mapping["lcsc_approved"] == 1
    assert mapping["digikey_approved"] == 1
    assert mapping["lcsc_pending_change"] == 0
    assert mapping["digikey_pending_change"] == 1
    assert mapping["digikey_code"] == "MANUAL-DK"
    assert mapping["previous_found_digikey"] == "DK1"
    assert mapping["last_found_digikey"] == "DK2"


def test_failed_lookup_does_not_change_history(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    db.insert_pending_suggestion("R1", "MPN1", "C1", "DK1")
    db.upsert_internal_mapping("R1", "MPN1", "C1", True, "DK1")

    assert db.refresh_mapping_codes("R1", None, None) is False
    mapping = db.get_internal_mapping("R1")
    assert mapping["approved"] == 1
    assert mapping["last_found_lcsc"] == "C1"
    assert mapping["last_found_digikey"] == "DK1"


def test_pending_suggestions_never_populate_approved_columns(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    db.insert_pending_suggestion("R1", "MPN1", "C1", "DK1")
    db.insert_pending_suggestion("R1", "MPN1", "C2", "DK2")

    mapping = db.get_internal_mapping("R1")
    assert mapping["approved"] == 0
    assert mapping["lcsc_code"] == ""
    assert mapping["digikey_code"] == ""
    assert mapping["previous_found_lcsc"] == "C1"
    assert mapping["last_found_lcsc"] == "C2"
    assert mapping["previous_found_digikey"] == "DK1"
    assert mapping["last_found_digikey"] == "DK2"


def test_approval_pending_accept_reject_and_reopen_are_independent(tmp_path):
    path = tmp_path / "mappings.sqlite"
    db = DatabaseManager(str(path))

    # First approval.
    db.insert_pending_suggestion("R1", "MPN1", "C1", "DK1")
    db.upsert_internal_mapping("R1", "MPN1", "C1", True, "DK1")

    # A different automatic result is pending while approved values survive.
    assert db.refresh_mapping_codes("R1", "C2", "DK2") is True
    mapping = db.get_internal_mapping("R1")
    assert (mapping["lcsc_code"], mapping["digikey_code"]) == ("C1", "DK1")
    assert (mapping["lcsc_approved"], mapping["digikey_approved"]) == (1, 1)
    assert (mapping["lcsc_pending_change"], mapping["digikey_pending_change"]) == (1, 1)

    # Rejecting only dismisses candidates, including after reopening the DB.
    db.reject_pending_changes("R1")
    reopened = DatabaseManager(str(path))
    mapping = reopened.get_internal_mapping("R1")
    assert (mapping["lcsc_code"], mapping["digikey_code"]) == ("C1", "DK1")
    assert (mapping["lcsc_pending_change"], mapping["digikey_pending_change"]) == (0, 0)

    # A later candidate can be accepted and remains accepted after reopening.
    assert reopened.refresh_mapping_codes("R1", "C3", "DK3") is True
    reopened.upsert_internal_mapping("R1", "MPN1", "C3", True, "DK3")
    final = DatabaseManager(str(path)).get_internal_mapping("R1")
    assert (final["lcsc_code"], final["digikey_code"]) == ("C3", "DK3")
    assert (final["lcsc_pending_change"], final["digikey_pending_change"]) == (0, 0)


def test_legacy_pending_codes_are_migrated_to_auto_history(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE internal_mappings (
                   comment_code TEXT PRIMARY KEY,
                   mpn TEXT NOT NULL,
                   lcsc_code TEXT NOT NULL,
                   approved INTEGER NOT NULL DEFAULT 0,
                   updated_at REAL,
                   digikey_code TEXT NOT NULL DEFAULT ''
               )"""
        )
        connection.execute(
            "INSERT INTO internal_mappings VALUES ('R1', 'MPN1', 'C1', 0, 0, 'DK1')"
        )

    db = DatabaseManager(str(path))
    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == ""
    assert mapping["digikey_code"] == ""
    assert mapping["last_found_lcsc"] == "C1"
    assert mapping["last_found_digikey"] == "DK1"


def test_mpn_lookup_cache_tracks_supplier_freshness_independently(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))
    assert db.get_mpn_lookup_cache("MPN1", 60) is None

    db.upsert_mpn_lookup_cache("MPN1", lcsc_code="C1")
    cached = db.get_mpn_lookup_cache("mpn1", 60)
    assert cached["lcsc_code"] == "C1"
    assert cached["lcsc_fresh"] is True
    assert cached["digikey_fresh"] is False

    db.upsert_mpn_lookup_cache("MPN1", digikey_code="DK1")
    cached = db.get_mpn_lookup_cache("MPN1", 60)
    assert cached["lcsc_code"] == "C1"
    assert cached["digikey_code"] == "DK1"
    assert cached["lcsc_fresh"] is True
    assert cached["digikey_fresh"] is True


def test_local_library_lookup_uses_exact_mpn_without_manufacturer_context(tmp_path):
    db = DatabaseManager(str(tmp_path / "library.sqlite"))
    db.bulk_upsert_library([
        {
            "lcsc_code": " C123 ",
            "mpn": " ABC123 ",
            "manufacturer": "KEMET",
        }
    ])

    assert db.lookup_lcsc_by_mpn("abc123") == "C123"
    assert db.lookup_lcsc_by_mpn("ABC12") is None
    assert db.lookup_lcsc_by_mpn("ABC123-X") is None


def test_first_manual_approval_does_not_seed_automatic_history(tmp_path):
    db = DatabaseManager(str(tmp_path / "mappings.sqlite"))

    db.upsert_internal_mapping("R1", "MPN1", "MANUAL-LCSC", True, "MANUAL-DK")

    mapping = db.get_internal_mapping("R1")
    assert mapping["lcsc_code"] == "MANUAL-LCSC"
    assert mapping["digikey_code"] == "MANUAL-DK"
    assert mapping["last_found_lcsc"] == ""
    assert mapping["last_found_digikey"] == ""
    assert mapping["lcsc_status"] == "MANUAL_OVERRIDE"
    assert mapping["digikey_status"] == "MANUAL_OVERRIDE"
