import sqlite3
import os
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any

class DatabaseManager:
    """Manages local SQLite database for caching and internal part mappings."""

    def __init__(self, db_path: Optional[str] = None):
        """Initialize the database manager with a path to the sqlite file."""
        # Always keep the application database beside the project source, not
        # beside the shell's current directory. Search workers and the UI can
        # otherwise open different ``saves/database.sqlite`` files when the app
        # is started from a shortcut or a different terminal directory.
        project_root = Path(__file__).resolve().parent.parent
        self.db_path = str(project_root / "saves" / "database.sqlite") if db_path is None else db_path
        self._lock = threading.Lock()
        
        # Ensure the directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        
        self._create_tables()

    def _get_connection(self) -> sqlite3.Connection:
        """Returns a new connection to the sqlite database."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
                         lcsc_status, digikey_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(comment_code) DO UPDATE SET
                        mpn = excluded.mpn,
                        lcsc_code = excluded.lcsc_code,
                        digikey_code = excluded.digikey_code,
                        approved = excluded.approved,
                        lcsc_source = excluded.lcsc_source,
                        digikey_source = excluded.digikey_source,
                        lcsc_status = excluded.lcsc_status,
                        digikey_status = excluded.digikey_status,
                        previous_found_lcsc = '',
                        previous_found_digikey = '',
                        updated_at = excluded.updated_at
                ''', (comment_code, mpn, lcsc_code, digikey_code, 1 if approved else 0, time.time(),
                      last_found_lcsc, last_found_digikey,
                      lcsc_source, digikey_source, lcsc_status, digikey_status))
                conn.commit()

    def insert_pending_suggestion(self, comment_code: str, mpn: str = "", lcsc_code: str = "", digikey_code: str = "") -> None:
        """Creates a new pending record OR fills in ONLY empty suggestion fields on an existing pending record.
        
        This preserves any manual edits the user has made in ApprovalDialog.
        Never overwrites an approved record.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Önce mevcut kaydı kontrol et
                cursor.execute("SELECT * FROM internal_mappings WHERE comment_code = ?", (comment_code,))
                existing = cursor.fetchone()

                if existing is None:
                    # Yeni kayıt — direkt ekle
                    cursor.execute(
                        """INSERT INTO internal_mappings
                           (comment_code, mpn, lcsc_code, digikey_code, approved, updated_at,
                            last_found_lcsc, last_found_digikey, lcsc_status, digikey_status)
                           VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'PENDING_REVIEW', 'PENDING_REVIEW')""",
                        (comment_code, mpn, "", "", time.time(), lcsc_code, digikey_code)
                    )
                elif existing["approved"] == 0:
                    # Pending suggestions update automatic history only. The
                    # approved columns remain untouched until Approve is clicked.
                    new_mpn = existing["mpn"] or mpn
                    new_lcsc = (lcsc_code or "").strip()
                    new_dk = (digikey_code or "").strip()
                    changed_lcsc = new_lcsc != existing["last_found_lcsc"]
                    changed_dk = new_dk != existing["last_found_digikey"]
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
                                   updated_at = ?
                               WHERE comment_code = ?""",
                            (new_mpn, changed_lcsc, changed_dk, new_lcsc, new_dk,
                             changed_lcsc, changed_dk, time.time(), comment_code)
                        )
                # approved == 1 ise hiçbir şey yapma — onaylı kaydı koru
                conn.commit()

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
                           last_found_lcsc = ?, last_found_digikey = ?, approved = 0,
                           lcsc_status = CASE WHEN ? THEN 'PENDING_REVIEW' ELSE lcsc_status END,
                           digikey_status = CASE WHEN ? THEN 'PENDING_REVIEW' ELSE digikey_status END,
                           updated_at = ?
                       WHERE comment_code = ?""",
                    (changed_lcsc, changed_digikey, new_lcsc, new_digikey,
                     changed_lcsc, changed_digikey, time.time(), comment_code),
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
                    "SELECT lcsc_code, stock, price_breaks_raw, package, category, source, timestamp FROM api_cache WHERE lcsc_code = ?",
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
    ) -> None:
        """Inserts or updates cached API data for an LCSC code."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO api_cache (lcsc_code, stock, price_breaks_raw, package, category, source, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lcsc_code) DO UPDATE SET
                        stock = excluded.stock,
                        price_breaks_raw = excluded.price_breaks_raw,
                        package = excluded.package,
                        category = excluded.category,
                        source = excluded.source,
                        timestamp = excluded.timestamp
                ''', (lcsc_code, stock, price_breaks_raw, package, category, source, timestamp))
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

    # =========================================================
    # LOCAL JLC LIBRARY METHODS
    # =========================================================

    def lookup_lcsc_by_mpn(self, mpn: str) -> Optional[str]:
        """Returns the LCSC code for the given MPN from the local library (case-insensitive)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT lcsc_code FROM local_jlc_library WHERE mpn = ? COLLATE NOCASE LIMIT 1",
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
                        r.get("lcsc_code", ""),
                        r.get("mpn", ""),
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
