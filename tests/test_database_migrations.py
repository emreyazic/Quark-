import sqlite3
import pytest
from core.database_manager import DatabaseManager, parse_retention_days


def test_parse_retention_days_valid_and_edge_cases():
    assert parse_retention_days(30) == 30
    assert parse_retention_days("45") == 45
    assert parse_retention_days(12.7) == 12
    # Invalid / non-numeric fallback to default (365 or passed default)
    assert parse_retention_days(None) == 365
    assert parse_retention_days(None, default=30) == 30
    assert parse_retention_days("") == 365
    assert parse_retention_days("invalid") == 365
    assert parse_retention_days(float("nan")) == 365
    assert parse_retention_days(float("inf")) == 365
    # Negative
    assert parse_retention_days(-5) == 365
    assert parse_retention_days(0) == 0
    # Excessive values capped at 3650
    assert parse_retention_days(10000) == 3650
    assert parse_retention_days("5000") == 3650


def test_migration_version_3_idempotent(tmp_path):
    db_path = str(tmp_path / "test_migration.db")
    
    # 1. Initialize schema v1/v2 manually
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE internal_mappings (
            comment_code TEXT PRIMARY KEY,
            mpn TEXT NOT NULL,
            lcsc_code TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            updated_at REAL
        )
    """)
    cur.execute("""
        CREATE TABLE app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("INSERT INTO app_metadata (key, value) VALUES ('mapping_data_version', '2')")
    cur.execute("""
        INSERT INTO internal_mappings (comment_code, mpn, lcsc_code, approved, updated_at)
        VALUES ('CODE1', 'MPN100', 'C100', 0, 1000.0)
    """)
    conn.commit()
    conn.close()

    # 2. Open via DatabaseManager (runs migrations up to version 3)
    db = DatabaseManager(db_path)
    with sqlite3.connect(db_path) as verify_conn:
        cur = verify_conn.cursor()
        cur.execute("SELECT value FROM app_metadata WHERE key = 'mapping_data_version'")
        ver = cur.fetchone()[0]
        assert ver == "3"

    # 3. Second open must be strictly idempotent
    db2 = DatabaseManager(db_path)
    with sqlite3.connect(db_path) as verify_conn2:
        cur2 = verify_conn2.cursor()
        cur2.execute("SELECT value FROM app_metadata WHERE key = 'mapping_data_version'")
        ver2 = cur2.fetchone()[0]
        assert ver2 == "3"


def test_separate_supplier_approval_and_rejection(tmp_path):
    db_path = str(tmp_path / "test_supplier_approval.db")
    db = DatabaseManager(db_path)

    # Initial mapping with approved JLCPCB and DigiKey
    db.upsert_internal_mapping("RES_10K", "RC0603FR-0710KL", "C12345", approved=True, digikey_code="DK-10K")

    # Verify initial state
    mapping = db.get_internal_mapping("RES_10K")
    assert mapping is not None
    assert mapping["lcsc_code"] == "C12345"
    assert mapping["lcsc_approved"] == 1
    assert mapping["digikey_code"] == "DK-10K"
    assert mapping["digikey_approved"] == 1

    # Stage a pending observation change for JLCPCB only
    changed = db.refresh_mapping_codes("RES_10K", lcsc_code="C99999", digikey_code=None)
    assert changed is True

    mapping_after_obs = db.get_internal_mapping("RES_10K")
    assert mapping_after_obs is not None
    assert mapping_after_obs["lcsc_code"] == "C12345"  # Approved code preserved
    assert mapping_after_obs["last_found_lcsc"] == "C99999"
    assert mapping_after_obs["lcsc_pending_change"] == 1
    assert mapping_after_obs["digikey_pending_change"] == 0  # DigiKey has no pending change

    # Reject JLCPCB pending change
    db.reject_supplier_pending_change("RES_10K", "jlcpcb")
    
    mapping_after_reject = db.get_internal_mapping("RES_10K")
    assert mapping_after_reject is not None
    assert mapping_after_reject["lcsc_code"] == "C12345"
    assert mapping_after_reject["lcsc_approved"] == 1
    assert mapping_after_reject["lcsc_pending_change"] == 0
    assert mapping_after_reject["digikey_code"] == "DK-10K"
    assert mapping_after_reject["digikey_approved"] == 1
