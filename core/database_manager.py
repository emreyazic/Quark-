import sqlite3
import os
import threading
import time
import math
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_HISTORY_RETENTION_DAYS = 365
MAX_HISTORY_RETENTION_DAYS = 3650


def parse_retention_days(value: Any, default: int = DEFAULT_HISTORY_RETENTION_DAYS) -> int:
    """Parse and validate history retention days safely."""
    if value is None or str(value).strip() == "":
        return default
    try:
        f_val = float(value)
        if not math.isfinite(f_val) or f_val < 0:
            logger.warning("Invalid SUPPLIER_HISTORY_RETENTION_DAYS value %r; using default %d", value, default)
            return default
        i_val = int(f_val)
        if i_val > MAX_HISTORY_RETENTION_DAYS:
            logger.warning("SUPPLIER_HISTORY_RETENTION_DAYS %r exceeds maximum; capped at %d", value, MAX_HISTORY_RETENTION_DAYS)
            return MAX_HISTORY_RETENTION_DAYS
        return i_val
    except (ValueError, TypeError):
        logger.warning("Non-numeric SUPPLIER_HISTORY_RETENTION_DAYS %r; using default %d", value, default)
        return default

class DatabaseManager:
    """Manages local SQLite database for caching and internal part mappings."""

    _lock_registry_guard = threading.Lock()
    _locks_by_path: dict[str, threading.RLock] = {}

    @classmethod
    def _shared_lock_for(cls, db_path: str) -> threading.RLock:
        canonical_path = os.path.normcase(os.path.abspath(db_path))
        with cls._lock_registry_guard:
            return cls._locks_by_path.setdefault(canonical_path, threading.RLock())

    def __init__(self, db_path: Optional[str] = None, history_retention_days: Optional[int] = None):
        """Initialize the database manager with a path to the sqlite file."""
        if db_path is not None:
            self.db_path = db_path
        else:
            env_path = os.getenv("BOM_TOOL_DB_PATH")
            if env_path:
                self.db_path = env_path
            else:
                project_root = Path(__file__).resolve().parent.parent
                default_saves = project_root / "saves"
                try:
                    default_saves.mkdir(parents=True, exist_ok=True)
                    test_file = default_saves / ".write_test"
                    test_file.touch()
                    test_file.unlink()
                    self.db_path = str(default_saves / "database.sqlite")
                except (OSError, PermissionError):
                    user_dir = (
                        Path(os.getenv("LOCALAPPDATA") or Path.home() / ".jlcpcb_bom_tool")
                        / "saves"
                    )
                    user_dir.mkdir(parents=True, exist_ok=True)
                    self.db_path = str(user_dir / "database.sqlite")

        self._lock = self._shared_lock_for(self.db_path)
        configured_retention = os.getenv("SUPPLIER_HISTORY_RETENTION_DAYS", "365")
        self.history_retention_days = parse_retention_days(
            configured_retention if history_retention_days is None else history_retention_days
        )
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        
        self._configure_database()
        self._create_tables()

    def _get_connection(self) -> sqlite3.Connection:
        """Returns a new connection to the sqlite database."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        # synchronous is connection-local; apply it to every short-lived
        # worker connection so WAL writes do not force a full fsync per row.
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _configure_database(self) -> None:
        """Enable safe concurrent readers and bounded write contention."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("PRAGMA journal_mode = WAL")

    def _create_tables(self) -> None:
        """Creates the necessary tables if they do not exist."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Table 1: internal_mappings
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS internal_mappings (
                        comment_code TEXT PRIMARY KEY,
                        mpn TEXT NOT NULL,
                        lcsc_code TEXT NOT NULL,
                        approved INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL
                    )
                ''')
                cursor.execute("PRAGMA table_info(internal_mappings)")
                columns = [info[1] for info in cursor.fetchall()]
                if 'digikey_code' not in columns:
                    cursor.execute("ALTER TABLE internal_mappings ADD COLUMN digikey_code TEXT NOT NULL DEFAULT ''")
                if 'updated_at' not in columns:
                    cursor.execute("ALTER TABLE internal_mappings ADD COLUMN updated_at REAL")
                # Keep the user-approved value separate from automatic lookup history.
                # Legacy rows are seeded from their current value so the first refresh
                # after upgrading does not create a false change.
                mapping_columns = {
                    'last_found_lcsc': "TEXT NOT NULL DEFAULT ''",
                    'last_found_digikey': "TEXT NOT NULL DEFAULT ''",
                    'previous_found_lcsc': "TEXT NOT NULL DEFAULT ''",
                    'previous_found_digikey': "TEXT NOT NULL DEFAULT ''",
                    'lcsc_source': "TEXT NOT NULL DEFAULT 'AUTO'",
                    'digikey_source': "TEXT NOT NULL DEFAULT 'AUTO'",
                    'lcsc_status': "TEXT NOT NULL DEFAULT 'AUTO_APPROVED'",
                    'digikey_status': "TEXT NOT NULL DEFAULT 'AUTO_APPROVED'",
                }
                history_columns_added = False
                for column, definition in mapping_columns.items():
                    if column not in columns:
                        cursor.execute(f"ALTER TABLE internal_mappings ADD COLUMN {column} {definition}")
                        history_columns_added = True
                if history_columns_added:
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET last_found_lcsc = lcsc_code,
                               last_found_digikey = digikey_code
                           WHERE last_found_lcsc = '' AND last_found_digikey = ''
                             AND (lcsc_code != '' OR digikey_code != '')"""
                    )

                # Supplier approvals are independent. ``approved`` remains as
                # a compatibility summary and is true only when both fields
                # are approved.
                field_approval_columns_added = False
                for column in ("lcsc_approved", "digikey_approved"):
                    if column not in columns:
                        cursor.execute(
                            f"ALTER TABLE internal_mappings ADD COLUMN {column} "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                        field_approval_columns_added = True
                if field_approval_columns_added:
                    if history_columns_added:
                        cursor.execute(
                            """UPDATE internal_mappings
                               SET lcsc_approved = approved,
                                   digikey_approved = approved"""
                        )
                    else:
                        cursor.execute(
                            """UPDATE internal_mappings
                               SET lcsc_approved = CASE
                                       WHEN approved = 1 OR lcsc_status != 'PENDING_REVIEW' THEN 1 ELSE 0 END,
                                   digikey_approved = CASE
                                       WHEN approved = 1 OR digikey_status != 'PENDING_REVIEW' THEN 1 ELSE 0 END"""
                        )

                # An approved value and a newer automatic candidate are two
                # independent facts. Keep explicit, supplier-specific pending
                # flags so observing a candidate never revokes an approval.
                pending_columns_added = False
                for column in ("lcsc_pending_change", "digikey_pending_change"):
                    if column not in columns:
                        cursor.execute(
                            f"ALTER TABLE internal_mappings ADD COLUMN {column} "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                        pending_columns_added = True
                if pending_columns_added:
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET lcsc_pending_change = CASE WHEN lcsc_status = 'PENDING_REVIEW' THEN 1 ELSE 0 END,
                               digikey_pending_change = CASE WHEN digikey_status = 'PENDING_REVIEW' THEN 1 ELSE 0 END"""
                    )
                # Track timestamp of when each supplier mapping was approved.
                for column in ("lcsc_approved_at", "digikey_approved_at"):
                    if column not in columns:
                        cursor.execute(f"ALTER TABLE internal_mappings ADD COLUMN {column} REAL")

                # Versioned data migration: older pending rows stored automatic
                # suggestions in the approved-value columns. Move those values
                # to lookup history once, then keep approved values empty until
                # the user explicitly approves them.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS app_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                ''')
                cursor.execute("SELECT value FROM app_metadata WHERE key = 'mapping_data_version'")
                version_row = cursor.fetchone()
                mapping_data_version = int(version_row["value"]) if version_row else 0
                if mapping_data_version < 2:
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET last_found_lcsc = CASE
                                   WHEN last_found_lcsc = '' THEN lcsc_code ELSE last_found_lcsc END,
                               last_found_digikey = CASE
                                   WHEN last_found_digikey = '' THEN digikey_code ELSE last_found_digikey END,
                               lcsc_code = '',
                               digikey_code = '',
                               lcsc_source = 'AUTO',
                               digikey_source = 'AUTO',
                               lcsc_status = 'PENDING_REVIEW',
                               digikey_status = 'PENDING_REVIEW'
                           WHERE approved = 0"""
                    )
                    cursor.execute(
                        """INSERT INTO app_metadata (key, value) VALUES ('mapping_data_version', '2')
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
                    )
                if mapping_data_version < 3:
                    # Builds that briefly coupled approval and pending state
                    # may have cleared supplier approval while retaining the
                    # approved code. Recover those unambiguous values.
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET lcsc_approved = CASE
                                   WHEN lcsc_pending_change = 1 AND lcsc_code != '' THEN 1
                                   ELSE lcsc_approved END,
                               digikey_approved = CASE
                                   WHEN digikey_pending_change = 1 AND digikey_code != '' THEN 1
                                   ELSE digikey_approved END"""
                    )
                    cursor.execute(
                        """INSERT INTO app_metadata (key, value) VALUES ('mapping_data_version', '3')
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
                    )
                
                # Table 2: api_cache
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS api_cache (
                        lcsc_code TEXT PRIMARY KEY,
                        stock INTEGER NOT NULL,
                        price_breaks_raw TEXT,
                        package TEXT,
                        category TEXT,
                        source TEXT NOT NULL DEFAULT '',
                        timestamp REAL NOT NULL
                    )
                ''')
                cursor.execute("PRAGMA table_info(api_cache)")
                api_cache_columns = [info[1] for info in cursor.fetchall()]
                if 'source' not in api_cache_columns:
                    cursor.execute("ALTER TABLE api_cache ADD COLUMN source TEXT NOT NULL DEFAULT ''")
                if 'availability' not in api_cache_columns:
                    cursor.execute(
                        "ALTER TABLE api_cache ADD COLUMN availability "
                        "TEXT NOT NULL DEFAULT 'UNKNOWN'"
                    )
                
                # Table 3: local_jlc_library  (MPN -> LCSC kalıcı eşleme, JOP API'den senkronize)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS local_jlc_library (
                        lcsc_code TEXT PRIMARY KEY,
                        mpn TEXT NOT NULL,
                        manufacturer TEXT,
                        description TEXT,
                        package TEXT,
                        category TEXT,
                        subcategory TEXT,
                        synced_at REAL NOT NULL DEFAULT 0
                    )
                ''')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_lib_mpn ON local_jlc_library (mpn COLLATE NOCASE)')

                # Reusable MPN lookup cache for bulk component-library imports.
                # Empty codes are valid cached "not found" results; NULL
                # timestamps mean that supplier has not been checked yet.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS mpn_lookup_cache (
                        mpn TEXT PRIMARY KEY COLLATE NOCASE,
                        lcsc_code TEXT NOT NULL DEFAULT '',
                        digikey_code TEXT NOT NULL DEFAULT '',
                        lcsc_checked_at REAL,
                        digikey_checked_at REAL
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS supplier_observation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        mpn TEXT NOT NULL COLLATE NOCASE,
                        supplier TEXT NOT NULL,
                        part_number TEXT NOT NULL DEFAULT '',
                        stock INTEGER,
                        unit_price REAL,
                        data_source TEXT NOT NULL DEFAULT '',
                        observed_at REAL NOT NULL,
                        UNIQUE(run_id, mpn, supplier)
                    )
                ''')
                cursor.execute("PRAGMA table_info(supplier_observation_history)")
                observation_columns = {info[1] for info in cursor.fetchall()}
                if "observation_type" not in observation_columns:
                    cursor.execute(
                        "ALTER TABLE supplier_observation_history ADD COLUMN "
                        "observation_type TEXT NOT NULL DEFAULT 'FOUND'"
                    )
                if "error_message" not in observation_columns:
                    cursor.execute(
                        "ALTER TABLE supplier_observation_history ADD COLUMN "
                        "error_message TEXT NOT NULL DEFAULT ''"
                    )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_supplier_history_lookup "
                    "ON supplier_observation_history "
                    "(mpn COLLATE NOCASE, supplier, observed_at DESC)"
                )
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS mapping_audit_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        comment_code TEXT NOT NULL,
                        mpn TEXT NOT NULL DEFAULT '',
                        supplier TEXT NOT NULL,
                        previous_code TEXT NOT NULL DEFAULT '',
                        candidate_code TEXT NOT NULL DEFAULT '',
                        selected_code TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                ''')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_comment ON mapping_audit_history (comment_code)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_created ON mapping_audit_history (created_at DESC)')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mapping_lcsc_pending "
                    "ON internal_mappings (lcsc_pending_change, comment_code)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mapping_digikey_pending "
                    "ON internal_mappings (digikey_pending_change, comment_code)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mapping_lcsc_approved "
                    "ON internal_mappings (lcsc_approved, lcsc_approved_at DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mapping_digikey_approved "
                    "ON internal_mappings (digikey_approved, digikey_approved_at DESC)"
                )

                conn.commit()

    # =========================================================
    # INTERNAL MAPPINGS METHODS
    # =========================================================

    def get_internal_mapping(self, comment_code: str) -> Optional[Dict[str, Any]]:
        """Retrieves an internal mapping by its comment code."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM internal_mappings WHERE comment_code = ?",
                    (comment_code,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

    def upsert_internal_mapping(self, comment_code: str, mpn: str, lcsc_code: str, approved: bool, digikey_code: str = "") -> None:
        """Inserts or updates an internal mapping (full overwrite — use for approvals)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()
                last_found_lcsc = existing["last_found_lcsc"] if existing else ""
                last_found_digikey = existing["last_found_digikey"] if existing else ""
                lcsc_source = "MANUAL" if lcsc_code != last_found_lcsc else "AUTO"
                digikey_source = "MANUAL" if digikey_code != last_found_digikey else "AUTO"
                lcsc_status = "MANUAL_OVERRIDE" if lcsc_source == "MANUAL" else "AUTO_APPROVED"
                digikey_status = "MANUAL_OVERRIDE" if digikey_source == "MANUAL" else "AUTO_APPROVED"
                cursor.execute('''
                    INSERT INTO internal_mappings
                        (comment_code, mpn, lcsc_code, digikey_code, approved, updated_at,
                         last_found_lcsc, last_found_digikey, lcsc_source, digikey_source,
                         lcsc_status, digikey_status, lcsc_approved, digikey_approved)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(comment_code) DO UPDATE SET
                        mpn = excluded.mpn,
                        lcsc_code = excluded.lcsc_code,
                        digikey_code = excluded.digikey_code,
                        approved = excluded.approved,
                        lcsc_source = excluded.lcsc_source,
                        digikey_source = excluded.digikey_source,
                        lcsc_status = excluded.lcsc_status,
                        digikey_status = excluded.digikey_status,
                        lcsc_approved = excluded.lcsc_approved,
                        digikey_approved = excluded.digikey_approved,
                        previous_found_lcsc = '',
                        previous_found_digikey = '',
                        lcsc_pending_change = 0,
                        digikey_pending_change = 0,
                        updated_at = excluded.updated_at
                ''', (comment_code, mpn, lcsc_code, digikey_code, 1 if approved else 0, time.time(),
                      last_found_lcsc, last_found_digikey,
                      lcsc_source, digikey_source, lcsc_status, digikey_status,
                      1 if approved else 0, 1 if approved else 0))
                conn.commit()

    def insert_pending_suggestion(
        self,
        comment_code: str,
        mpn: str = "",
        lcsc_code: str = "",
        digikey_code: str = "",
        *,
        lcsc_pending_enabled: Optional[bool] = None,
        digikey_pending_enabled: Optional[bool] = None,
    ) -> None:
        """Creates a new pending record OR fills in ONLY empty suggestion fields on an existing pending record.
        
        This preserves any manual edits the user has made in ApprovalDialog.
        Never overwrites an approved record.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                lcsc_enabled = True if lcsc_pending_enabled is None else bool(lcsc_pending_enabled)
                digikey_enabled = True if digikey_pending_enabled is None else bool(digikey_pending_enabled)
                # Önce mevcut kaydı kontrol et
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()

                if existing is None:
                    # Yeni kayıt — direkt ekle
                    cursor.execute(
                        """INSERT INTO internal_mappings
                           (comment_code, mpn, lcsc_code, digikey_code, approved, updated_at,
                            last_found_lcsc, last_found_digikey, lcsc_status, digikey_status,
                            lcsc_approved, digikey_approved,
                            lcsc_pending_change, digikey_pending_change)
                           VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
                        (
                            comment_code, mpn, "", "", time.time(),
                            (lcsc_code or "").strip() if lcsc_enabled else "",
                            (digikey_code or "").strip() if digikey_enabled else "",
                            "PENDING_REVIEW" if lcsc_enabled else "NOT_SEARCHED",
                            "PENDING_REVIEW" if digikey_enabled else "NOT_SEARCHED",
                            int(lcsc_enabled), int(digikey_enabled),
                        )
                    )
                else:
                    # Pending suggestions update automatic history only. The
                    # approved columns remain untouched until Approve is clicked.
                    new_mpn = existing["mpn"] or mpn
                    new_lcsc = (lcsc_code or "").strip() if lcsc_enabled else existing["last_found_lcsc"]
                    new_dk = (digikey_code or "").strip() if digikey_enabled else existing["last_found_digikey"]
                    changed_lcsc = lcsc_enabled and new_lcsc != existing["last_found_lcsc"]
                    changed_dk = digikey_enabled and new_dk != existing["last_found_digikey"]
                    if new_mpn != existing["mpn"] or changed_lcsc or changed_dk:
                        cursor.execute(
                            """UPDATE internal_mappings
                               SET mpn = ?,
                                   previous_found_lcsc = CASE WHEN ? THEN last_found_lcsc ELSE previous_found_lcsc END,
                                   previous_found_digikey = CASE WHEN ? THEN last_found_digikey ELSE previous_found_digikey END,
                                   last_found_lcsc = ?,
                                   last_found_digikey = ?,
                                   lcsc_status = CASE WHEN ? THEN 'PENDING_REVIEW' ELSE lcsc_status END,
                                   digikey_status = CASE WHEN ? THEN 'PENDING_REVIEW' ELSE digikey_status END,
                                   lcsc_pending_change = CASE WHEN ? THEN 1 ELSE lcsc_pending_change END,
                                   digikey_pending_change = CASE WHEN ? THEN 1 ELSE digikey_pending_change END,
                                   updated_at = ?
                               WHERE comment_code = ?""",
                            (new_mpn, changed_lcsc, changed_dk, new_lcsc, new_dk,
                             changed_lcsc, changed_dk, changed_lcsc, changed_dk,
                             time.time(), comment_code)
                        )
                # approved == 1 ise hiçbir şey yapma — onaylı kaydı koru
                conn.commit()

    def bulk_insert_new_pending_suggestions(self, records: list[tuple[str, str, str, str]]) -> int:
        """Insert previously unknown pending mappings in one transaction."""
        if not records:
            return 0

        # Deduplicate incoming records by comment_code (preserve first unique occurrence)
        seen_codes = set()
        unique_records = []
        for record in records:
            code = record[0].strip()
            if code and code not in seen_codes:
                seen_codes.add(code)
                unique_records.append(record)

        if not unique_records:
            return 0

        with self._lock:
            with self._get_connection() as conn:
                before = conn.total_changes
                conn.executemany(
                    """INSERT OR IGNORE INTO internal_mappings
                       (comment_code, mpn, lcsc_code, digikey_code, approved, updated_at,
                        last_found_lcsc, last_found_digikey, lcsc_status, digikey_status,
                        lcsc_approved, digikey_approved,
                        lcsc_pending_change, digikey_pending_change)
                       VALUES (?, ?, '', '', 0, ?, ?, ?,
                               'PENDING_REVIEW', 'PENDING_REVIEW', 0, 0, 1, 1)""",
                    [
                        (
                            comment_code,
                            mpn,
                            time.time(),
                            (lcsc_code or "").strip(),
                            (digikey_code or "").strip(),
                        )
                        for comment_code, mpn, lcsc_code, digikey_code in unique_records
                    ],
                )
                conn.commit()
                return conn.total_changes - before

    def delete_internal_mapping(self, comment_code: str) -> None:
        """Deletes an internal mapping by its comment code."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                conn.commit()

    def reset_all_internal_mappings(self) -> None:
        """Deletes all internal mappings, forcing a fresh search on next run."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM internal_mappings")
                conn.commit()

    def refresh_mapping_codes(self, comment_code: str, lcsc_code: Optional[str] = "", digikey_code: Optional[str] = "") -> bool:
        """Compare completed automatic lookups with history, never approved values.

        ``None`` means the lookup failed and is ignored; an empty string is a
        successful lookup that found no supplier code.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM internal_mappings WHERE comment_code = ?",
                    (comment_code,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    return False

                new_lcsc = lcsc_code.strip() if lcsc_code is not None else existing["last_found_lcsc"]
                new_digikey = digikey_code.strip() if digikey_code is not None else existing["last_found_digikey"]
                changed_lcsc = lcsc_code is not None and new_lcsc != existing["last_found_lcsc"]
                changed_digikey = digikey_code is not None and new_digikey != existing["last_found_digikey"]
                if not changed_lcsc and not changed_digikey:
                    return False

                cursor.execute(
                    """UPDATE internal_mappings
                       SET previous_found_lcsc = CASE WHEN ? THEN last_found_lcsc ELSE previous_found_lcsc END,
                           previous_found_digikey = CASE WHEN ? THEN last_found_digikey ELSE previous_found_digikey END,
                           last_found_lcsc = ?, last_found_digikey = ?,
                           lcsc_status = CASE WHEN ? THEN 'PENDING_REVIEW' ELSE lcsc_status END,
                           digikey_status = CASE WHEN ? THEN 'PENDING_REVIEW' ELSE digikey_status END,
                           lcsc_pending_change = CASE WHEN ? THEN 1 ELSE lcsc_pending_change END,
                           digikey_pending_change = CASE WHEN ? THEN 1 ELSE digikey_pending_change END,
                           updated_at = ?
                       WHERE comment_code = ?""",
                    (changed_lcsc, changed_digikey, new_lcsc, new_digikey,
                     changed_lcsc, changed_digikey, changed_lcsc, changed_digikey,
                     time.time(), comment_code),
                )
                conn.commit()
                return True

    def reject_pending_changes(self, comment_code: str) -> None:
        """Dismiss automatic candidates without changing approved values."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """UPDATE internal_mappings
                       SET lcsc_pending_change = 0,
                           digikey_pending_change = 0,
                           lcsc_status = CASE WHEN lcsc_approved = 1 THEN 'AUTO_APPROVED' ELSE lcsc_status END,
                           digikey_status = CASE WHEN digikey_approved = 1 THEN 'AUTO_APPROVED' ELSE digikey_status END,
                           updated_at = ?
                       WHERE comment_code = ?""",
                    (time.time(), comment_code),
                )
                conn.commit()

    def record_mapping_audit(
        self,
        comment_code: str,
        mpn: str,
        supplier: str,
        previous_code: str,
        candidate_code: str,
        selected_code: str,
        action: str,
        created_at: Optional[float] = None,
    ) -> None:
        """Record an audit entry for internal mapping approval/keep/edit."""
        now = time.time() if created_at is None else created_at
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """INSERT INTO mapping_audit_history
                       (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        comment_code.strip(),
                        (mpn or "").strip(),
                        supplier.strip().upper(),
                        (previous_code or "").strip(),
                        (candidate_code or "").strip(),
                        (selected_code or "").strip(),
                        action.strip().upper(),
                        now,
                    ),
                )
                conn.commit()

    def get_mapping_audit_history(
        self,
        comment_code: Optional[str] = None,
        supplier: Optional[str] = None,
        *,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[Dict[str, Any]]:
        """Retrieve audit history entries ordered newest first."""
        with self._lock:
            with self._get_connection() as conn:
                query = (
                    "SELECT id, comment_code, mpn, supplier, previous_code, "
                    "candidate_code, selected_code, action, created_at "
                    "FROM mapping_audit_history"
                )
                conditions = []
                params = []
                if comment_code:
                    conditions.append("comment_code = ?")
                    params.append(comment_code.strip())
                if supplier:
                    conditions.append("supplier = ?")
                    params.append(supplier.strip().upper())
                if search:
                    pattern = f"%{search.strip()}%"
                    conditions.append(
                        "(comment_code LIKE ? OR mpn LIKE ? OR supplier LIKE ? OR "
                        "previous_code LIKE ? OR candidate_code LIKE ? OR "
                        "selected_code LIKE ? OR action LIKE ?)"
                    )
                    params.extend([pattern] * 7)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY created_at DESC, id DESC"
                if limit is not None:
                    query += " LIMIT ? OFFSET ?"
                    params.extend((max(0, int(limit)), max(0, int(offset))))
                rows = conn.execute(query, params).fetchall()
                return [dict(row) for row in rows]

    def count_mapping_audit_history(self, search: Optional[str] = None) -> int:
        """Count audit rows, optionally using the same filter as the history model."""
        with self._lock:
            with self._get_connection() as conn:
                params: list[Any] = []
                query = "SELECT COUNT(*) FROM mapping_audit_history"
                if search:
                    pattern = f"%{search.strip()}%"
                    query += (
                        " WHERE comment_code LIKE ? OR mpn LIKE ? OR supplier LIKE ? OR "
                        "previous_code LIKE ? OR candidate_code LIKE ? OR "
                        "selected_code LIKE ? OR action LIKE ?"
                    )
                    params.extend([pattern] * 7)
                return int(conn.execute(query, params).fetchone()[0])

    def get_pending_supplier_mappings(self, search: Optional[str] = None) -> list[Dict[str, Any]]:
        """Return only supplier rows which currently need a decision."""
        params: list[Any] = []
        filters = ""
        if search:
            pattern = f"%{search.strip()}%"
            filters = (
                " AND (comment_code LIKE ? OR mpn LIKE ? OR supplier LIKE ? "
                "OR current_code LIKE ? OR candidate_code LIKE ?)"
            )
            params.extend([pattern] * 5)
        query = f"""
            SELECT comment_code, mpn, supplier, current_code, candidate_code,
                   has_approved
            FROM (
                SELECT comment_code, mpn, 'JLCPCB' AS supplier,
                       lcsc_code AS current_code,
                       last_found_lcsc AS candidate_code,
                       CASE WHEN lcsc_approved = 1 AND lcsc_code != '' THEN 1 ELSE 0 END AS has_approved
                FROM internal_mappings WHERE lcsc_pending_change = 1
                UNION ALL
                SELECT comment_code, mpn, 'DigiKey' AS supplier,
                       digikey_code AS current_code,
                       last_found_digikey AS candidate_code,
                       CASE WHEN digikey_approved = 1 AND digikey_code != '' THEN 1 ELSE 0 END AS has_approved
                FROM internal_mappings WHERE digikey_pending_change = 1
            ) AS pending
            WHERE 1 = 1 {filters}
            ORDER BY comment_code COLLATE NOCASE, supplier
        """
        with self._lock:
            with self._get_connection() as conn:
                return [dict(row) for row in conn.execute(query, params).fetchall()]

    def approve_supplier_mapping(
        self,
        comment_code: str,
        supplier: str,
        code: str,
        mpn: Optional[str] = None,
        candidate_code: Optional[str] = None,
    ) -> None:
        """Approve or manually override a mapping for a single specific supplier without affecting the other supplier."""
        supplier_norm = supplier.upper().strip()
        clean_code = (code or "").strip()
        if not clean_code:
            raise ValueError("Approved code cannot be empty.")

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()
                now = time.time()
                new_mpn = mpn.strip() if mpn is not None else (existing["mpn"] if existing else "")

                if supplier_norm in ("JLCPCB", "LCSC"):
                    last_found = (
                        candidate_code.strip()
                        if candidate_code is not None
                        else ((existing["last_found_lcsc"] or "").strip() if existing else "")
                    )
                    prev_code = (existing["lcsc_code"] or "").strip() if existing else ""
                    action = "ACCEPT_CANDIDATE" if clean_code == last_found else "MANUAL_EDIT"
                    source = "AUTO" if action == "ACCEPT_CANDIDATE" else "MANUAL"
                    status = "AUTO_APPROVED" if source == "AUTO" else "MANUAL_OVERRIDE"

                    if existing:
                        cursor.execute(
                            """UPDATE internal_mappings
                               SET mpn = CASE WHEN ? != '' THEN ? ELSE mpn END,
                                   lcsc_code = ?,
                                   lcsc_source = ?,
                                   lcsc_status = ?,
                                   lcsc_approved = 1,
                                   lcsc_approved_at = ?,
                                   lcsc_pending_change = 0,
                                   previous_found_lcsc = '',
                                   approved = 1,
                                   updated_at = ?
                               WHERE comment_code = ?""",
                            (new_mpn, new_mpn, clean_code, source, status, now, now, comment_code)
                        )
                    else:
                        cursor.execute(
                            """INSERT INTO internal_mappings
                               (comment_code, mpn, lcsc_code, digikey_code, approved, updated_at,
                                last_found_lcsc, last_found_digikey, lcsc_source, digikey_source,
                                lcsc_status, digikey_status, lcsc_approved, digikey_approved,
                                lcsc_approved_at, digikey_approved_at,
                                lcsc_pending_change, digikey_pending_change)
                               VALUES (?, ?, ?, '', 1, ?, ?, '', ?, 'AUTO', ?, 'PENDING_REVIEW', 1, 0, ?, NULL, 0, 0)""",
                            (comment_code, new_mpn, clean_code, now, clean_code, source, status, now)
                        )
                elif supplier_norm == "DIGIKEY":
                    last_found = (
                        candidate_code.strip()
                        if candidate_code is not None
                        else ((existing["last_found_digikey"] or "").strip() if existing else "")
                    )
                    prev_code = (existing["digikey_code"] or "").strip() if existing else ""
                    action = "ACCEPT_CANDIDATE" if clean_code == last_found else "MANUAL_EDIT"
                    source = "AUTO" if action == "ACCEPT_CANDIDATE" else "MANUAL"
                    status = "AUTO_APPROVED" if source == "AUTO" else "MANUAL_OVERRIDE"

                    if existing:
                        cursor.execute(
                            """UPDATE internal_mappings
                               SET mpn = CASE WHEN ? != '' THEN ? ELSE mpn END,
                                   digikey_code = ?,
                                   digikey_source = ?,
                                   digikey_status = ?,
                                   digikey_approved = 1,
                                   digikey_approved_at = ?,
                                   digikey_pending_change = 0,
                                   previous_found_digikey = '',
                                   approved = 1,
                                   updated_at = ?
                               WHERE comment_code = ?""",
                            (new_mpn, new_mpn, clean_code, source, status, now, now, comment_code)
                        )
                    else:
                        cursor.execute(
                            """INSERT INTO internal_mappings
                               (comment_code, mpn, lcsc_code, digikey_code, approved, updated_at,
                                last_found_lcsc, last_found_digikey, lcsc_source, digikey_source,
                                lcsc_status, digikey_status, lcsc_approved, digikey_approved,
                                lcsc_approved_at, digikey_approved_at,
                                lcsc_pending_change, digikey_pending_change)
                                VALUES (?, ?, '', ?, 1, ?, '', ?, 'AUTO', ?, 'PENDING_REVIEW', ?, 0, 1, NULL, ?, 0, 0)""",
                            (comment_code, new_mpn, clean_code, now, clean_code, source, status, now)
                        )
                else:
                    raise ValueError(f"Unsupported supplier: {supplier}")

                cursor.execute(
                    """INSERT INTO mapping_audit_history
                       (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (comment_code, new_mpn, supplier_norm, prev_code, last_found, clean_code, action, now),
                )
                conn.commit()

    def update_approved_supplier_code(
        self,
        comment_code: str,
        supplier: str,
        new_code: str,
        mpn: Optional[str] = None,
    ) -> None:
        """Manually update an existing approved mapping for a supplier and record MANUAL_EDIT audit history.

        Does not touch pending flags or the other supplier's values.
        """
        supplier_norm = supplier.upper().strip()
        clean_code = (new_code or "").strip()
        if not clean_code:
            raise ValueError("Approved code cannot be empty.")

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()
                if not existing:
                    raise ValueError(f"Mapping for '{comment_code}' does not exist.")

                now = time.time()
                new_mpn = mpn.strip() if mpn is not None else (existing["mpn"] or "")

                if supplier_norm in ("JLCPCB", "LCSC"):
                    prev_code = (existing["lcsc_code"] or "").strip()
                    last_found = (existing["last_found_lcsc"] or "").strip()
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET mpn = CASE WHEN ? != '' THEN ? ELSE mpn END,
                               lcsc_code = ?,
                               lcsc_source = 'MANUAL',
                               lcsc_status = 'MANUAL_OVERRIDE',
                               lcsc_approved = 1,
                               lcsc_approved_at = ?,
                               approved = 1,
                               updated_at = ?
                           WHERE comment_code = ?""",
                        (new_mpn, new_mpn, clean_code, now, now, comment_code),
                    )
                elif supplier_norm == "DIGIKEY":
                    prev_code = (existing["digikey_code"] or "").strip()
                    last_found = (existing["last_found_digikey"] or "").strip()
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET mpn = CASE WHEN ? != '' THEN ? ELSE mpn END,
                               digikey_code = ?,
                               digikey_source = 'MANUAL',
                               digikey_status = 'MANUAL_OVERRIDE',
                               digikey_approved = 1,
                               digikey_approved_at = ?,
                               approved = 1,
                               updated_at = ?
                           WHERE comment_code = ?""",
                        (new_mpn, new_mpn, clean_code, now, now, comment_code),
                    )
                else:
                    raise ValueError(f"Unsupported supplier: {supplier}")

                cursor.execute(
                    """INSERT INTO mapping_audit_history
                       (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'MANUAL_EDIT', ?)""",
                    (comment_code, new_mpn, supplier_norm, prev_code, last_found, clean_code, now),
                )
                conn.commit()

    def get_approved_supplier_mappings(
        self,
        search: Optional[str] = None,
        supplier: Optional[str] = None,
        *,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[Dict[str, Any]]:
        """Retrieve all approved supplier records as individual supplier entries.

        Each component can yield up to two entries (one for JLCPCB, one for DigiKey).
        Ordered newest approved first.
        """
        params: list[Any] = []
        conditions = ["approved_code != ''"]
        if supplier and supplier != "All Suppliers":
            conditions.append("supplier = ?")
            params.append(supplier.strip())
        if search:
            pattern = f"%{search.strip()}%"
            conditions.append(
                "(comment_code LIKE ? OR mpn LIKE ? OR approved_code LIKE ?)"
            )
            params.extend([pattern] * 3)
        query = """
            SELECT comment_code, mpn, supplier, approved_code, approved_at
            FROM (
                SELECT comment_code, mpn, 'JLCPCB' AS supplier,
                       lcsc_code AS approved_code, lcsc_approved_at AS approved_at
                FROM internal_mappings WHERE lcsc_approved = 1
                UNION ALL
                SELECT comment_code, mpn, 'DigiKey' AS supplier,
                       digikey_code AS approved_code, digikey_approved_at AS approved_at
                FROM internal_mappings WHERE digikey_approved = 1
            ) AS approved_rows
            WHERE """ + " AND ".join(conditions) + """
            ORDER BY approved_at IS NOT NULL DESC, approved_at DESC,
                     comment_code COLLATE NOCASE, supplier
        """
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(0, int(limit)), max(0, int(offset))))
        with self._lock:
            with self._get_connection() as conn:
                return [dict(row) for row in conn.execute(query, params).fetchall()]

    def count_approved_supplier_mappings(
        self, search: Optional[str] = None, supplier: Optional[str] = None
    ) -> int:
        """Count supplier-specific approved rows without loading their payload."""
        params: list[Any] = []
        conditions = ["approved_code != ''"]
        if supplier and supplier != "All Suppliers":
            conditions.append("supplier = ?")
            params.append(supplier.strip())
        if search:
            pattern = f"%{search.strip()}%"
            conditions.append("(comment_code LIKE ? OR mpn LIKE ? OR approved_code LIKE ?)")
            params.extend([pattern] * 3)
        query = """
            SELECT COUNT(*) FROM (
                SELECT comment_code, mpn, 'JLCPCB' AS supplier, lcsc_code AS approved_code
                FROM internal_mappings WHERE lcsc_approved = 1
                UNION ALL
                SELECT comment_code, mpn, 'DigiKey' AS supplier, digikey_code AS approved_code
                FROM internal_mappings WHERE digikey_approved = 1
            ) AS approved_rows WHERE """ + " AND ".join(conditions)
        with self._lock:
            with self._get_connection() as conn:
                return int(conn.execute(query, params).fetchone()[0])

    def keep_supplier_current_mapping(self, comment_code: str, supplier: str) -> None:
        """Keep current approved code, dismiss candidate, and record KEEP_CURRENT audit entry.

        If no current approved code exists, raises ValueError to avoid silently losing the candidate without an approved value.
        """
        supplier_norm = supplier.upper().strip()
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()
                if not existing:
                    raise ValueError(f"Mapping '{comment_code}' does not exist.")

                now = time.time()
                mpn = existing["mpn"] or ""
                if supplier_norm in ("JLCPCB", "LCSC"):
                    current_code = (existing["lcsc_code"] or "").strip()
                    if not current_code or not existing["lcsc_approved"]:
                        raise ValueError("Cannot keep current mapping: no approved JLCPCB code exists.")
                    candidate = (existing["last_found_lcsc"] or "").strip()
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET lcsc_pending_change = 0,
                               lcsc_status = CASE WHEN lcsc_source = 'MANUAL' THEN 'MANUAL_OVERRIDE' ELSE 'AUTO_APPROVED' END,
                               updated_at = ?
                           WHERE comment_code = ?""",
                        (now, comment_code),
                    )
                elif supplier_norm == "DIGIKEY":
                    current_code = (existing["digikey_code"] or "").strip()
                    if not current_code or not existing["digikey_approved"]:
                        raise ValueError("Cannot keep current mapping: no approved DigiKey code exists.")
                    candidate = (existing["last_found_digikey"] or "").strip()
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET digikey_pending_change = 0,
                               digikey_status = CASE WHEN digikey_source = 'MANUAL' THEN 'MANUAL_OVERRIDE' ELSE 'AUTO_APPROVED' END,
                               updated_at = ?
                           WHERE comment_code = ?""",
                        (now, comment_code),
                    )
                else:
                    raise ValueError(f"Unsupported supplier: {supplier}")

                cursor.execute(
                    """INSERT INTO mapping_audit_history
                       (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (comment_code, mpn, supplier_norm, current_code, candidate, current_code, "KEEP_CURRENT", now),
                )
                conn.commit()

    def reject_supplier_pending_change(self, comment_code: str, supplier: str) -> None:
        """Dismiss automatic candidates for a single specific supplier without changing the other supplier or approved values."""
        supplier_norm = supplier.upper().strip()
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()
                now = time.time()
                if supplier_norm in ("JLCPCB", "LCSC"):
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET lcsc_pending_change = 0,
                               lcsc_status = CASE WHEN lcsc_approved = 1 THEN 'AUTO_APPROVED' ELSE lcsc_status END,
                               updated_at = ?
                           WHERE comment_code = ?""",
                        (now, comment_code),
                    )
                    if existing and (existing["lcsc_code"] or "").strip() and existing["lcsc_approved"]:
                        cursor.execute(
                            """INSERT INTO mapping_audit_history
                               (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, 'KEEP_CURRENT', ?)""",
                            (comment_code, existing["mpn"] or "", supplier_norm, existing["lcsc_code"], existing["last_found_lcsc"] or "", existing["lcsc_code"], now),
                        )
                elif supplier_norm == "DIGIKEY":
                    cursor.execute(
                        """UPDATE internal_mappings
                           SET digikey_pending_change = 0,
                               digikey_status = CASE WHEN digikey_approved = 1 THEN 'AUTO_APPROVED' ELSE digikey_status END,
                               updated_at = ?
                           WHERE comment_code = ?""",
                        (now, comment_code),
                    )
                    if existing and (existing["digikey_code"] or "").strip() and existing["digikey_approved"]:
                        cursor.execute(
                            """INSERT INTO mapping_audit_history
                               (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, 'KEEP_CURRENT', ?)""",
                            (comment_code, existing["mpn"] or "", supplier_norm, existing["digikey_code"], existing["last_found_digikey"] or "", existing["digikey_code"], now),
                        )
                else:
                    raise ValueError(f"Unsupported supplier: {supplier}")
                conn.commit()

    def invalidate_lcsc_preorder_candidate(
        self, comment_code: str, candidate_code: str = ""
    ) -> bool:
        """Remove a live pre-order candidate without changing an approved mapping."""
        with self._lock:
            with self._get_connection() as conn:
                existing = conn.execute(
                    "SELECT comment_code, mpn, lcsc_code, lcsc_approved, "
                    "last_found_lcsc, lcsc_pending_change FROM internal_mappings "
                    "WHERE comment_code = ?",
                    (comment_code,),
                ).fetchone()
                if existing is None:
                    return False
                current_candidate = (existing["last_found_lcsc"] or "").strip()
                target = (candidate_code or current_candidate).strip()
                if not existing["lcsc_pending_change"] and not current_candidate:
                    return False
                now = time.time()
                conn.execute(
                    """UPDATE internal_mappings
                       SET last_found_lcsc = '', lcsc_pending_change = 0,
                           lcsc_status = CASE WHEN lcsc_approved = 1
                               THEN 'NEEDS_REVIEW_PREORDER'
                               ELSE 'AUTO_INVALIDATED_PREORDER' END,
                           updated_at = ?
                       WHERE comment_code = ?""",
                    (now, comment_code),
                )
                conn.execute(
                    """INSERT INTO mapping_audit_history
                       (comment_code, mpn, supplier, previous_code, candidate_code,
                        selected_code, action, created_at)
                       VALUES (?, ?, 'JLCPCB', ?, ?, ?, 'AUTO_INVALIDATED_PREORDER', ?)""",
                    (
                        comment_code,
                        existing["mpn"] or "",
                        existing["lcsc_code"] or "",
                        target,
                        existing["lcsc_code"] if existing["lcsc_approved"] else "",
                        now,
                    ),
                )
                conn.commit()
                return True

    def get_all_internal_mappings(self) -> list[Dict[str, Any]]:
        """Retrieves all internal mappings."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM internal_mappings")
                return [dict(row) for row in cursor.fetchall()]

    # =========================================================
    # API CACHE METHODS
    # =========================================================

    def get_api_cache(self, lcsc_code: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached API data for a specific LCSC code."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT lcsc_code, stock, price_breaks_raw, package, category, source, "
                    "availability, timestamp FROM api_cache WHERE lcsc_code = ?",
                    (lcsc_code,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

    def upsert_api_cache(
        self,
        lcsc_code: str,
        stock: int,
        price_breaks_raw: str,
        package: str,
        category: str,
        timestamp: float,
        source: str = "",
        availability: str = "UNKNOWN",
    ) -> None:
        """Inserts or updates cached API data for an LCSC code."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO api_cache (lcsc_code, stock, price_breaks_raw, package, category, source, availability, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lcsc_code) DO UPDATE SET
                        stock = excluded.stock,
                        price_breaks_raw = excluded.price_breaks_raw,
                        package = excluded.package,
                        category = excluded.category,
                        source = excluded.source,
                        availability = excluded.availability,
                        timestamp = excluded.timestamp
                ''', (lcsc_code, stock, price_breaks_raw, package, category, source, availability, timestamp))
                conn.commit()

    def clear_old_cache(self, max_age_seconds: float) -> None:
        """Clears cache entries older than the specified max age in seconds."""
        import time
        cutoff_timestamp = time.time() - max_age_seconds
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM api_cache WHERE timestamp < ?", (cutoff_timestamp,))
                conn.commit()

    def get_mpn_lookup_cache(self, mpn: str, max_age_seconds: float) -> Optional[Dict[str, Any]]:
        """Return supplier results with independent freshness flags for an MPN."""
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM mpn_lookup_cache WHERE mpn = ? COLLATE NOCASE",
                    (mpn.strip(),),
                ).fetchone()
                if row is None:
                    return None
                result = dict(row)
                now = time.time()
                result["lcsc_fresh"] = bool(
                    result["lcsc_checked_at"] is not None
                    and now - result["lcsc_checked_at"] <= max_age_seconds
                )
                result["digikey_fresh"] = bool(
                    result["digikey_checked_at"] is not None
                    and now - result["digikey_checked_at"] <= max_age_seconds
                )
                return result

    def upsert_mpn_lookup_cache(
        self,
        mpn: str,
        lcsc_code: Optional[str] = None,
        digikey_code: Optional[str] = None,
    ) -> None:
        """Cache completed supplier lookups; ``None`` leaves that source unchanged."""
        if lcsc_code is None and digikey_code is None:
            return
        now = time.time()
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO mpn_lookup_cache (mpn) VALUES (?)",
                    (mpn.strip(),),
                )
                if lcsc_code is not None:
                    conn.execute(
                        "UPDATE mpn_lookup_cache SET lcsc_code = ?, lcsc_checked_at = ? WHERE mpn = ? COLLATE NOCASE",
                        (lcsc_code.strip(), now, mpn.strip()),
                    )
                if digikey_code is not None:
                    conn.execute(
                        "UPDATE mpn_lookup_cache SET digikey_code = ?, digikey_checked_at = ? WHERE mpn = ? COLLATE NOCASE",
                        (digikey_code.strip(), now, mpn.strip()),
                    )
                conn.commit()

    def bulk_upsert_mpn_lookup_cache(self, records: list[tuple[str, Optional[str], Optional[str]]]) -> None:
        """Cache multiple completed lookups in one write transaction."""
        if not records:
            return
        now = time.time()
        with self._lock:
            with self._get_connection() as conn:
                for mpn, lcsc_code, digikey_code in records:
                    normalized_mpn = mpn.strip()
                    conn.execute(
                        "INSERT OR IGNORE INTO mpn_lookup_cache (mpn) VALUES (?)",
                        (normalized_mpn,),
                    )
                    if lcsc_code is not None:
                        conn.execute(
                            "UPDATE mpn_lookup_cache SET lcsc_code = ?, lcsc_checked_at = ? "
                            "WHERE mpn = ? COLLATE NOCASE",
                            (lcsc_code.strip(), now, normalized_mpn),
                        )
                    if digikey_code is not None:
                        conn.execute(
                            "UPDATE mpn_lookup_cache SET digikey_code = ?, digikey_checked_at = ? "
                            "WHERE mpn = ? COLLATE NOCASE",
                            (digikey_code.strip(), now, normalized_mpn),
                        )
                conn.commit()

    def record_supplier_observation(
        self,
        run_id: str,
        mpn: str,
        supplier: str,
        part_number: str,
        stock: Optional[int],
        unit_price: Optional[float],
        data_source: str = "",
        observation_type: str = "FOUND",
        error_message: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Store one observation and return the preceding completed run value."""
        return self.record_supplier_observations([
            (
                run_id,
                mpn,
                supplier,
                part_number,
                stock,
                unit_price,
                data_source,
                observation_type,
                error_message,
            )
        ])[0]

    def record_supplier_observations(
        self,
        records: list[
            tuple[
                str,
                str,
                str,
                str,
                Optional[int],
                Optional[float],
                str,
                str,
                str,
            ]
        ],
    ) -> list[Optional[Dict[str, Any]]]:
        """Store a refresh run in one transaction and return prior values."""
        if not records:
            return []
        previous_values: list[Optional[Dict[str, Any]]] = []
        with self._lock:
            with self._get_connection() as conn:
                observed_at = time.time()
                for (
                    run_id,
                    mpn,
                    supplier,
                    part_number,
                    stock,
                    unit_price,
                    data_source,
                    observation_type,
                    error_message,
                ) in records:
                    normalized_mpn = mpn.strip()
                    normalized_supplier = supplier.strip().upper()
                    previous_row = conn.execute(
                        """SELECT * FROM supplier_observation_history
                           WHERE mpn = ? COLLATE NOCASE AND supplier = ? AND run_id != ?
                           ORDER BY observed_at DESC, id DESC LIMIT 1""",
                        (normalized_mpn, normalized_supplier, run_id),
                    ).fetchone()
                    previous_values.append(
                        dict(previous_row) if previous_row else None
                    )
                    conn.execute(
                        """INSERT INTO supplier_observation_history
                           (run_id, mpn, supplier, part_number, stock, unit_price,
                            data_source, observed_at, observation_type, error_message)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(run_id, mpn, supplier) DO UPDATE SET
                               part_number = excluded.part_number,
                               stock = excluded.stock,
                               unit_price = excluded.unit_price,
                               data_source = excluded.data_source,
                               observation_type = excluded.observation_type,
                               error_message = excluded.error_message,
                               observed_at = excluded.observed_at""",
                        (
                            run_id,
                            normalized_mpn,
                            normalized_supplier,
                            part_number.strip(),
                            stock,
                            unit_price,
                            data_source,
                            observed_at,
                            observation_type.strip().upper(),
                            error_message,
                        ),
                    )
                if self.history_retention_days:
                    conn.execute(
                        "DELETE FROM supplier_observation_history WHERE observed_at < ?",
                        (observed_at - self.history_retention_days * 86400,),
                    )
                conn.commit()
        return previous_values

    def get_supplier_observation_history(
        self, mpn: str, supplier: Optional[str] = None
    ) -> list[Dict[str, Any]]:
        """Return newest-first price/stock observations for diagnostics."""
        with self._lock:
            with self._get_connection() as conn:
                if supplier:
                    rows = conn.execute(
                        """SELECT * FROM supplier_observation_history
                           WHERE mpn = ? COLLATE NOCASE AND supplier = ?
                           ORDER BY observed_at DESC, id DESC""",
                        (mpn.strip(), supplier.strip().upper()),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM supplier_observation_history
                           WHERE mpn = ? COLLATE NOCASE
                           ORDER BY observed_at DESC, id DESC""",
                        (mpn.strip(),),
                    ).fetchall()
                return [dict(row) for row in rows]

    # =========================================================
    # LOCAL JLC LIBRARY METHODS
    # =========================================================

    def lookup_lcsc_by_mpn(self, mpn: str) -> Optional[str]:
        """Return the single exact MPN→LCSC mapping from the local library.

        Manufacturer text is irrelevant. The library contract is one exact
        MPN per product; ordering only makes legacy duplicate rows
        deterministic instead of depending on SQLite row order.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT lcsc_code
                       FROM local_jlc_library
                       WHERE mpn = ? COLLATE NOCASE
                       ORDER BY synced_at DESC, lcsc_code COLLATE NOCASE ASC
                       LIMIT 1""",
                    (mpn.strip(),)
                )
                row = cursor.fetchone()
                return row["lcsc_code"] if row else None

    def get_library_count(self) -> int:
        """Returns the number of components in the local library."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM local_jlc_library")
                return cursor.fetchone()[0]

    def bulk_upsert_library(self, records: list) -> int:
        """Bulk inserts or replaces library records. Returns the number of records written.
        
        Each record must be a dict with keys:
            lcsc_code, mpn, manufacturer, description, package, category, subcategory, synced_at
        """
        if not records:
            return 0
        import time
        now = time.time()
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                data = [
                    (
                        str(r.get("lcsc_code", "")).strip(),
                        str(r.get("mpn", "")).strip(),
                        r.get("manufacturer", ""),
                        r.get("description", ""),
                        r.get("package", ""),
                        r.get("category", ""),
                        r.get("subcategory", ""),
                        now,
                    )
                    for r in records
                    if r.get("lcsc_code") and r.get("mpn")
                ]
                cursor.executemany('''
                    INSERT INTO local_jlc_library
                        (lcsc_code, mpn, manufacturer, description, package, category, subcategory, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lcsc_code) DO UPDATE SET
                        mpn        = excluded.mpn,
                        manufacturer = excluded.manufacturer,
                        description  = excluded.description,
                        package      = excluded.package,
                        category     = excluded.category,
                        subcategory  = excluded.subcategory,
                        synced_at    = excluded.synced_at
                ''', data)
                conn.commit()
                return len(data)
