import sqlite3
import os
import threading
from typing import Optional, Dict, Any

class DatabaseManager:
    """Manages local SQLite database for caching and internal part mappings."""

    def __init__(self, db_path: str = "saves/database.sqlite"):
        """Initialize the database manager with a path to the sqlite file."""
        self.db_path = db_path
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
                        approved INTEGER NOT NULL DEFAULT 0
                    )
                ''')
                
                cursor.execute("PRAGMA table_info(internal_mappings)")
                columns = [info[1] for info in cursor.fetchall()]
                if 'digikey_code' not in columns:
                    cursor.execute("ALTER TABLE internal_mappings ADD COLUMN digikey_code TEXT NOT NULL DEFAULT ''")
                
                # Table 2: api_cache
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS api_cache (
                        lcsc_code TEXT PRIMARY KEY,
                        stock INTEGER NOT NULL,
                        price_breaks_raw TEXT,
                        package TEXT,
                        category TEXT,
                        timestamp REAL NOT NULL
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
                    "SELECT comment_code, mpn, lcsc_code, digikey_code, approved FROM internal_mappings WHERE comment_code = ?", 
                    (comment_code,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

    def upsert_internal_mapping(self, comment_code: str, mpn: str, lcsc_code: str, approved: bool, digikey_code: str = "") -> None:
        """Inserts or updates an internal mapping."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO internal_mappings (comment_code, mpn, lcsc_code, digikey_code, approved)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(comment_code) DO UPDATE SET
                        mpn = excluded.mpn,
                        lcsc_code = excluded.lcsc_code,
                        digikey_code = excluded.digikey_code,
                        approved = excluded.approved
                ''', (comment_code, mpn, lcsc_code, digikey_code, 1 if approved else 0))
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
                
    def get_all_internal_mappings(self) -> list[Dict[str, Any]]:
        """Retrieves all internal mappings."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT comment_code, mpn, lcsc_code, digikey_code, approved FROM internal_mappings")
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
                    "SELECT lcsc_code, stock, price_breaks_raw, package, category, timestamp FROM api_cache WHERE lcsc_code = ?", 
                    (lcsc_code,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

    def upsert_api_cache(self, lcsc_code: str, stock: int, price_breaks_raw: str, package: str, category: str, timestamp: float) -> None:
        """Inserts or updates cached API data for an LCSC code."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO api_cache (lcsc_code, stock, price_breaks_raw, package, category, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lcsc_code) DO UPDATE SET
                        stock = excluded.stock,
                        price_breaks_raw = excluded.price_breaks_raw,
                        package = excluded.package,
                        category = excluded.category,
                        timestamp = excluded.timestamp
                ''', (lcsc_code, stock, price_breaks_raw, package, category, timestamp))
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
