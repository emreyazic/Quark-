"""JLCPCB parts searcher using the jlcsearch community API.

Key fixes vs. original:
- Exact MPN matching for ALL results (single or multiple) — never accepts partial matches.
- Stock threshold check: (Quantity × 10) + 10
- Price break parsing from API response
- RES-coded component skip before API call
- Clean MPN values before search
- Stores candidate info in notes when no exact match found
"""

import time
import json
from typing import Optional

import requests
from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from models.bom_item import BomItem
from core.mpn_utils import (
    clean_mpn_value,
    is_exact_mpn_match,
    is_res_coded,
    compute_required_stock,
    select_unit_price,
)
from core.digikey_searcher import DigiKeySearcher, enrich_bom_item_digikey


# jlcsearch community API endpoint
API_BASE_URL = "https://jlcsearch.tscircuit.com/components/list.json"

# Rate limiting: delay between API requests (seconds)
REQUEST_DELAY = 0.2

# HTTP timeout per request (seconds)
REQUEST_TIMEOUT = 15

# Maximum retry attempts on failure
MAX_RETRIES = 3

# Exponential backoff base (seconds)
RETRY_BACKOFF_BASE = 1.0


class JlcpcbSearchResult:
    """Holds the result of a single JLCPCB search."""

    def __init__(self):
        self.found: bool = False
        self.exact_match: bool = False
        self.lcsc_code: str = ""
        self.matched_mpn: str = ""  # The actual MPN from the API (mfr field)
        self.stock: int = 0
        self.unit_price: Optional[float] = None
        self.price_breaks_raw: str = ""  # Raw JSON string
        self.package: str = ""
        self.category: str = ""
        self.subcategory: str = ""
        self.description: str = ""
        self.is_basic: bool = False
        self.is_preferred: bool = False
        self.match_count: int = 0
        self.error: Optional[str] = None
        self.candidates: list[dict] = []  # Summary of all candidates for notes


class JlcpcbSearcher:
    """Searches the JLCPCB parts database via the jlcsearch community API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "BOM-Enrichment-Tool/2.0",
            "Accept": "application/json",
        })

    def search_mpn(self, mpn: str, required_stock: int = 0) -> JlcpcbSearchResult:
        """Search for a component by MPN with exact matching.

        Args:
            mpn: Manufacturer Part Number to search for.
            required_stock: Minimum stock required (from compute_required_stock).

        Returns:
            JlcpcbSearchResult with match details.
        """
        result = JlcpcbSearchResult()

        if not mpn or mpn.strip() == "":
            result.error = "Missing MPN"
            return result

        mpn_clean = clean_mpn_value(mpn)
        if not mpn_clean:
            result.error = "Missing MPN"
            return result

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(
                    API_BASE_URL,
                    params={"search": mpn_clean},
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()

                data = resp.json()
                components = data.get("components", [])

                result.match_count = len(components)

                if len(components) == 0:
                    result.found = False
                    return result

                # Search ALL results for an exact MPN match
                # The API 'mfr' field contains the MPN (not manufacturer name)
                exact_comp = None
                for comp in components:
                    candidate_mpn = comp.get("mfr", "")
                    if is_exact_mpn_match(mpn_clean, candidate_mpn):
                        exact_comp = comp
                        break

                # Store candidate info for notes regardless of match
                for comp in components[:5]:  # Limit to first 5 candidates
                    result.candidates.append({
                        "mpn": comp.get("mfr", ""),
                        "lcsc": f"C{comp.get('lcsc', '')}",
                        "stock": int(comp.get("stock", 0)),
                        "category": comp.get("category", ""),
                    })

                if exact_comp is None:
                    # No exact match among results
                    result.found = False
                    result.exact_match = False
                    return result

                # Exact match found — populate result
                result.found = True
                result.exact_match = True
                result.matched_mpn = exact_comp.get("mfr", "")
                result.lcsc_code = f"C{exact_comp.get('lcsc', '')}"
                result.stock = int(exact_comp.get("stock", 0))
                result.package = exact_comp.get("package", "")
                result.category = exact_comp.get("category", "")
                result.subcategory = exact_comp.get("subcategory", "")
                result.description = exact_comp.get("description", "")
                result.is_basic = bool(exact_comp.get("is_basic", False))
                result.is_preferred = bool(exact_comp.get("is_preferred", False))
                result.price_breaks_raw = exact_comp.get("price", "")

                # Parse unit price for the required quantity
                if result.price_breaks_raw and required_stock > 0:
                    result.unit_price = select_unit_price(
                        result.price_breaks_raw, required_stock
                    )

                return result

            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                result.error = f"Request timed out after {MAX_RETRIES} attempts"
                return result

            except requests.exceptions.ConnectionError:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                result.error = "Connection error - check internet connection"
                return result

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code >= 500:
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                        continue
                result.error = f"HTTP error: {e}"
                return result

            except (json.JSONDecodeError, KeyError, ValueError) as e:
                result.error = f"Invalid API response: {e}"
                return result

            except Exception as e:
                result.error = f"Unexpected error: {e}"
                return result

        return result

    def close(self):
        """Close the HTTP session."""
        self.session.close()


def enrich_bom_item(item: BomItem, search_result: JlcpcbSearchResult) -> None:
    """Apply JLCPCB search results to a BomItem in-place.

    CRITICAL RULE: jlcpcb_part_number is ONLY set when:
      1. The MPN match is EXACT
      2. Stock is sufficient: available >= (Quantity × 10) + 10

    All other cases leave jlcpcb_part_number BLANK.

    Args:
        item: The BomItem to enrich.
        search_result: The search result from JLCPCB.
    """
    item.source = "JLCPCB"
    required = item.required_stock

    # ── Skip: PRE-CHECKED conditions (e.g. RES or Missing MPN) ──
    if item.skip_jlcpcb:
        # Don't overwrite item.status here! It might be "RES manual" or "Missing MPN"
        return

    # ── Error cases ──────────────────────────────────────────────
    if search_result.error:
        if search_result.error == "Missing MPN":
            item.status = "Missing MPN"
        else:
            item.status = "JLCPCB API error"
        return

    # ── Not found at all ─────────────────────────────────────────
    if not search_result.found and not search_result.exact_match:
        if search_result.match_count > 0:
            # Results exist but none are an exact MPN match
            item.status = "No exact JLCPCB match"
            item.matched_mpn = ""
        else:
            item.status = "JLCPCB not found"
        return

    # ── Exact match found — check stock ──────────────────────────
    item.matched_mpn = search_result.matched_mpn
    item.exact_match = True
    item.available_stock_qty = search_result.stock
    item.jlcpcb_category = search_result.category
    item.jlcpcb_package = search_result.package
    item.is_basic = search_result.is_basic
    item.is_preferred = search_result.is_preferred
    item.unit_price = search_result.unit_price
    item.jlcpcb_price_breaks_raw = search_result.price_breaks_raw

    if search_result.stock < required:
        # Exact match but insufficient stock — do NOT fill JLC code
        item.status = "Insufficient JLCPCB stock"
        item.jlcpcb_part_number = ""  # Explicitly blank
        return

    # ── Success: exact match + sufficient stock ──────────────────
    item.jlcpcb_part_number = search_result.lcsc_code
    item.status = ""


class SearchWorker(QThread):
    """Background worker that searches JLCPCB for all BOM items.

    Signals:
        progress(int, int, str, str): (current, total, mpn, status) — progress update
        item_result(int, BomItem): (index, enriched_item) — single item result
        finished_all(list): list of all enriched BomItems
        error(str): fatal error message
    """

    progress = pyqtSignal(int, int, str, str)  # current, total, mpn, status
    item_result = pyqtSignal(int, object)  # index, BomItem
    finished_all = pyqtSignal(list)  # all items
    error = pyqtSignal(str)  # error message

    def __init__(self, items: list[BomItem], parent=None):
        super().__init__(parent)
        self.items = items
        self._cancelled = False
        self._mutex = QMutex()

    def cancel(self):
        """Request cancellation of the search."""
        self._mutex.lock()
        self._cancelled = True
        self._mutex.unlock()

    def _is_cancelled(self) -> bool:
        self._mutex.lock()
        val = self._cancelled
        self._mutex.unlock()
        return val

    def run(self):
        """Execute the search for all items."""
        searcher = JlcpcbSearcher()
        dk_searcher = DigiKeySearcher()
        total = len(self.items)

        try:
            for idx, item in enumerate(self.items):
                if self._is_cancelled():
                    break

                mpn_display = item.mpn if item.mpn else "(empty)"
                self.progress.emit(idx + 1, total, mpn_display, "Searching JLCPCB...")

                # Compute required stock for this item
                item.required_stock = compute_required_stock(item.quantity)

                # ── RES skip: check comment column ───────────────────
                if is_res_coded(item.comment):
                    item.status = "RES manual"
                    item.jlcpcb_part_number = ""
                    item.skip_jlcpcb = True
                # ── Missing MPN: don't search JLCPCB ───────────────────
                elif not item.mpn or not item.mpn.strip():
                    item.status = "Missing MPN"
                    item.jlcpcb_part_number = ""
                    item.skip_jlcpcb = True

                # ── Search JLCPCB ────────────────────────────────────
                if not item.skip_jlcpcb:
                    result = searcher.search_mpn(item.mpn, item.required_stock)
                    enrich_bom_item(item, result)

                # ── Always Search DigiKey for Price Reference ─────────────
                if dk_searcher.is_configured:
                    self.progress.emit(idx + 1, total, mpn_display, "Searching DigiKey for pricing...")
                    dk_result = dk_searcher.search_item(item)
                    enrich_bom_item_digikey(item, dk_result)

                self.item_result.emit(idx, item)
                self.progress.emit(
                    idx + 1, total, mpn_display, item.status
                )

                # Rate limiting delay (only for JLCPCB since DigiKey has different limits)
                if idx < total - 1 and not self._is_cancelled():
                    time.sleep(REQUEST_DELAY)

            self.finished_all.emit(self.items)

        except Exception as e:
            self.error.emit(str(e))

        finally:
            searcher.close()
            dk_searcher.close()
