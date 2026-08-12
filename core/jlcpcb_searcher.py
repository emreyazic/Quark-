"""JLCPCB parts searcher using the official JLC Open Platform (JOP) API.
Implements HMAC-SHA256 request signing and JOP authorization headers.
"""

import time
import json
import base64
import hmac
import hashlib
import random
import string
from typing import Optional, Dict, Any
import requests
from models.bom_item import BomItem
from core.mpn_utils import (
    clean_mpn_value,
    is_exact_mpn_match,
    is_res_coded,
    compute_required_stock,
    select_unit_price,
)

# Resmi JLC Open Platform API Base URL (Dokümantasyona göre güncellenebilir)
API_BASE_URL = "https://open.jlcpcb.com"

# İstek zaman aşımı (saniye)
REQUEST_TIMEOUT = 15


class JlcpcbSearchResult:
    """Holds the result of a single official JLCPCB API search."""
    def __init__(self):
        self.found: bool = False
        self.exact_match: bool = False
        self.lcsc_code: str = ""
        self.matched_mpn: str = ""
        self.stock: int = 0
        self.unit_price: Optional[float] = None
        self.price_breaks_raw: str = ""
        self.package: str = ""
        self.category: str = ""
        self.subcategory: str = ""
        self.description: str = ""
        self.is_basic: bool = False
        self.is_preferred: bool = False
        self.match_count: int = 0
        self.error: Optional[str] = None
        self.candidates: list[dict] = []


class JlcpcbSearcher:
    """Searches the JLCPCB parts database via official Open Platform API."""
    def __init__(self, app_id: str, access_key: str, secret_key: str):
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _generate_nonce(self) -> str:
        """Generate a 32-character random alphanumeric string."""
        chars = string.ascii_letters + string.digits
        return "".join(random.choices(chars, k=32))

    def _sign_request(self, method: str, path: str, timestamp: int, nonce: str, body: str) -> str:
        """Constructs the signature string and signs it using HMAC-SHA256 and Base64."""
        string_to_sign = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
        
        # HMAC-SHA256 with secret key
        signature_bytes = hmac.new(
            self.secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256
        ).digest()
        
        return base64.b64encode(signature_bytes).decode("utf-8")

    def _get_auth_header(self, method: str, path: str, body_str: str) -> str:
        """Generates the required JOP Authorization header."""
        nonce = self._generate_nonce()
        timestamp = int(time.time())
        signature = self._sign_request(method, path, timestamp, nonce, body_str)
        
        return (
            f'JOP appid="{self.app_id}",'
            f'accesskey="{self.access_key}",'
            f'nonce="{nonce}",'
            f'timestamp="{timestamp}",'
            f'signature="{signature}"'
        )

    def search_mpn(self, mpn: str, required_stock: int = 0) -> JlcpcbSearchResult:
        """Search for a component by MPN using official JLC OpenAPI endpoints."""
        result = JlcpcbSearchResult()
        if not mpn or mpn.strip() == "":
            result.error = "Missing MPN"
            return result
        
        mpn_clean = clean_mpn_value(mpn)
        if not mpn_clean:
            result.error = "Missing MPN"
            return result

        # Resmi bileşen arama endpoint yolu (Dokümanınızdaki gerçek path ile güncelleyin, örn: /component/v1/search)
        path = "/component/v1/search"
        url = f"{API_BASE_URL}{path}"

        # İstek gövdesi (Payload)
        payload = {
            "keyword": mpn_clean,
            "requiredStock": required_stock
        }
        body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        try:
            auth_header = self._get_auth_header("POST", path, body_str)
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header
            }

            resp = self.session.post(url, data=body_str, headers=headers, timeout=REQUEST_TIMEOUT)
            
            # J-Trace-ID kontrolü için loglanabilir
            trace_id = resp.headers.get("J-Trace-ID", "")
            
            if resp.status_code == 401:
                result.error = "API Unauthorized (401): Signature verification failed."
                return result
            elif resp.status_code == 403:
                result.error = "API Forbidden (403): Access denied."
                return result
            
            resp.raise_for_status()
            data = resp.json()

            # İş seviyesi hata kontrolü
            code = data.get("code", 200)
            if code != 200 and code != 0:
                result.error = f"API Error [{code}]: {data.get('message', 'Unknown error')}"
                return result

            # Yanıt yapısındaki bileşen listesini parse etme
            response_data = data.get("data", {})
            components = response_data.get("components", []) if isinstance(response_data, dict) else response_data
            result.match_count = len(components)

            if not components:
                result.found = False
                return result

            exact_comp = None
            for comp in components:
                candidate_mpn = comp.get("mfr", "") or comp.get("manufacturerPartNumber", "")
                if is_exact_mpn_match(mpn_clean, candidate_mpn):
                    exact_comp = comp
                    break

            for comp in components[:5]:
                result.candidates.append({
                    "mpn": comp.get("mfr", "") or comp.get("manufacturerPartNumber", ""),
                    "lcsc": f"C{comp.get('lcsc', '') or comp.get('lcscCode', '')}",
                    "stock": int(comp.get("stock", 0) or comp.get("availableStock", 0)),
                })

            if exact_comp is None:
                result.found = False
                result.exact_match = False
                return result

            result.found = True
            result.exact_match = True
            result.matched_mpn = exact_comp.get("mfr", "") or exact_comp.get("manufacturerPartNumber", "")
            result.lcsc_code = f"C{exact_comp.get('lcsc', '') or exact_comp.get('lcscCode', '')}"
            result.stock = int(exact_comp.get("stock", 0) or exact_comp.get("availableStock", 0))
            result.package = exact_comp.get("package", "")
            result.category = exact_comp.get("category", "")
            result.price_breaks_raw = json.dumps(exact_comp.get("priceList", []) or exact_comp.get("price", ""))
            
            if result.price_breaks_raw and required_stock > 0:
                result.unit_price = select_unit_price(result.price_breaks_raw, required_stock)

            return result

        except Exception as e:
            result.error = f"API Request Exception: {str(e)}"
            return result

    def close(self):
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

    if item.skip_jlcpcb:
        return

    if search_result.error:
        if search_result.error == "Missing MPN":
            item.status = "Missing MPN"
        else:
            item.status = "JLCPCB API error"
        return

    if not search_result.found and not search_result.exact_match:
        if search_result.match_count > 0:
            item.status = "No exact JLCPCB match"
            item.matched_mpn = ""
        else:
            item.status = "JLCPCB not found"
        return

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
        item.status = "Insufficient JLCPCB stock"
        item.jlcpcb_part_number = ""
        return

    item.jlcpcb_part_number = search_result.lcsc_code
    item.status = ""


from PyQt6.QtCore import QThread, pyqtSignal, QMutex
from core.digikey_searcher import DigiKeySearcher, enrich_bom_item_digikey

class SearchWorker(QThread):
    """Background worker that searches JLCPCB for all BOM items."""

    progress = pyqtSignal(int, int, str, str)
    item_result = pyqtSignal(int, object)
    finished_all = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, items: list[BomItem], app_id: str, access_key: str, secret_key: str, parent=None):
        super().__init__(parent)
        self.items = items
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self._cancelled = False
        self._mutex = QMutex()

    def cancel(self):
        self._mutex.lock()
        self._cancelled = True
        self._mutex.unlock()

    def _is_cancelled(self) -> bool:
        self._mutex.lock()
        val = self._cancelled
        self._mutex.unlock()
        return val

    def run(self):
        searcher = JlcpcbSearcher(self.app_id, self.access_key, self.secret_key)
        dk_searcher = DigiKeySearcher()
        total = len(self.items)

        try:
            for idx, item in enumerate(self.items):
                if self._is_cancelled():
                    break

                mpn_display = item.mpn if item.mpn else "(empty)"
                self.progress.emit(idx + 1, total, mpn_display, "Searching JLCPCB...")

                item.required_stock = compute_required_stock(item.quantity)

                if is_res_coded(item.comment):
                    item.status = "RES manual"
                    item.jlcpcb_part_number = ""
                    item.skip_jlcpcb = True
                elif not item.mpn or not item.mpn.strip():
                    item.status = "Missing MPN"
                    item.jlcpcb_part_number = ""
                    item.skip_jlcpcb = True

                if not item.skip_jlcpcb:
                    result = searcher.search_mpn(item.mpn, item.required_stock)
                    enrich_bom_item(item, result)

                if dk_searcher.is_configured:
                    self.progress.emit(idx + 1, total, mpn_display, "Searching DigiKey for pricing...")
                    dk_result = dk_searcher.search_item(item)
                    enrich_bom_item_digikey(item, dk_result)

                self.item_result.emit(idx, item)
                self.progress.emit(idx + 1, total, mpn_display, item.status)

            self.finished_all.emit(self.items)

        except Exception as e:
            self.error.emit(str(e))

        finally:
            searcher.close()
            dk_searcher.close()