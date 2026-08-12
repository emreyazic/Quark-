"""JLCPCB parts searcher using a hybrid approach:
1. Maps permanent MPN to LCSC code.
2. Fetches real-time stock, package, category, and pricing via official JLC Open Platform (JOP) API.
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
    is_res_coded,
    compute_required_stock,
    select_unit_price,
)
from core.database_manager import DatabaseManager

# Resmi JLC Open Platform API Base URL
API_BASE_URL = "https://open.jlcpcb.com"
# MPN -> LCSC eşlemesi için kullanılan hızlı arama servisi
COMMUNITY_SEARCH_URL = "https://jlcsearch.tscircuit.com/components/list.json"

# İstek zaman aşımı (saniye)
REQUEST_TIMEOUT = 15


class JlcpcbSearchResult:
    """Holds the result of a hybrid JLCPCB search."""
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
    """Searches and enriches JLCPCB parts using MPN resolution + Official JOP API."""
    def __init__(self, app_id: str, access_key: str, secret_key: str, db_manager: Optional[DatabaseManager] = None):
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.db_manager = db_manager
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _generate_nonce(self) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choices(chars, k=32))

    def _sign_request(self, method: str, path: str, timestamp: int, nonce: str, body: str) -> str:
        string_to_sign = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
        signature_bytes = hmac.new(
            self.secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256
        ).digest()
        return base64.b64encode(signature_bytes).decode("utf-8")

    def _get_auth_header(self, method: str, path: str, body_str: str) -> str:
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

    def _resolve_lcsc_from_mpn(self, mpn_clean: str) -> Optional[str]:
        """Resolves permanent MPN to LCSC part code (C...) using search mapping."""
        try:
            resp = self.session.get(
                COMMUNITY_SEARCH_URL,
                params={"manufacturer_part_number": mpn_clean, "limit": 1},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                components = data.get("components", []) if isinstance(data, dict) else data
                if components and len(components) > 0:
                    comp = components[0]
                    lcsc_val = comp.get("lcscPart", "") or comp.get("lcsc", "") or comp.get("componentCode", "")
                    if lcsc_val:
                        lcsc_str = str(lcsc_val).strip()
                        return lcsc_str if lcsc_str.startswith("C") else f"C{lcsc_str}"
        except Exception:
            pass
        return None

    def search_mpn(self, mpn: str, required_stock: int = 0, refresh: bool = False) -> JlcpcbSearchResult:
        """Search component by MPN: resolves LCSC code first, then queries official JOP OpenAPI."""
        result = JlcpcbSearchResult()
        if not mpn or mpn.strip() == "":
            result.error = "Missing MPN"
            return result
        
        mpn_clean = clean_mpn_value(mpn)
        if not mpn_clean:
            result.error = "Missing MPN"
            return result

        # 1. Adım: MPN'i LCSC koduna çözümle
        lcsc_code = self._resolve_lcsc_from_mpn(mpn_clean)
        if not lcsc_code:
            result.found = False
            result.error = "JLCPCB not found"
            return result

        # 2. Adım: Resmi SDK path'i ile resmi JOP API'sinden canlı veri çek
        path = "/overseas/openapi/component/getComponentDetailByCode"
        url = f"{API_BASE_URL}{path}"

        # LCSC kodunu sayısal formata çevir (C harfini çıkararak veya olduğu gibi, JOP API genellikle C'siz sayı veya C ile kabul eder)
        lcsc_numeric = lcsc_code[1:] if lcsc_code.startswith("C") else lcsc_code

        payload = {
            "componentCodes": [lcsc_numeric]
        }
        body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        try:
            auth_header = self._get_auth_header("POST", path, body_str)
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header
            }

            resp = self.session.post(url, data=body_str, headers=headers, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code == 401:
                result.error = "API Unauthorized (401): Signature verification failed."
                return result
            elif resp.status_code == 403:
                result.error = "API Forbidden (403): Access denied."
                return result
            
            resp.raise_for_status()
            data = resp.json()

            code = data.get("code", 200)
            if code != 200 and code != 0:
                result.error = f"API Error [{code}]: {data.get('message', 'Unknown error')}"
                return result

            response_data = data.get("data", [])
            if isinstance(response_data, dict):
                components = response_data.get("components", []) or response_data.get("componentList", []) or response_data.get("data", [])
            elif isinstance(response_data, list):
                components = response_data
            else:
                components = []

            result.match_count = len(components)

            if not components:
                result.found = False
                return result

            exact_comp = components[0]

            result.found = True
            result.exact_match = True
            result.matched_mpn = exact_comp.get("componentModel", "") or exact_comp.get("mfr", "") or exact_comp.get("manufacturerPartNumber", "") or mpn_clean
            
            result.lcsc_code = lcsc_code
            result.stock = int(exact_comp.get("stockCount", 0) or exact_comp.get("stock", 0) or exact_comp.get("availableStock", 0))
            result.package = exact_comp.get("componentSpecification", "") or exact_comp.get("package", "")
            result.category = exact_comp.get("firstTypeName", "") or exact_comp.get("category", "")
            
            price_ranges = exact_comp.get("priceRanges", []) or exact_comp.get("priceList", [])
            result.price_breaks_raw = json.dumps(price_ranges) if price_ranges else ""
            
            if result.price_breaks_raw and required_stock > 0:
                result.unit_price = select_unit_price(result.price_breaks_raw, required_stock)

            if self.db_manager and result.lcsc_code:
                self.db_manager.upsert_api_cache(
                    lcsc_code=result.lcsc_code,
                    stock=result.stock,
                    price_breaks_raw=result.price_breaks_raw,
                    package=result.package,
                    category=result.category,
                    timestamp=time.time()
                )

            return result

        except Exception as e:
            result.error = f"API Request Exception: {str(e)}"
            return result

    def search_lcsc(self, lcsc_code: str, mpn: str, required_stock: int = 0, refresh: bool = False) -> JlcpcbSearchResult:
        """Search component directly by LCSC code using API cache or JOP OpenAPI."""
        result = JlcpcbSearchResult()
        
        if self.db_manager and not refresh:
            cached = self.db_manager.get_api_cache(lcsc_code)
            if cached and time.time() - cached["timestamp"] < 86400:
                result.found = True
                result.exact_match = True
                result.matched_mpn = mpn
                result.lcsc_code = lcsc_code
                result.stock = cached["stock"]
                result.package = cached["package"]
                result.category = cached["category"]
                result.price_breaks_raw = cached["price_breaks_raw"]
                if result.price_breaks_raw and required_stock > 0:
                    result.unit_price = select_unit_price(result.price_breaks_raw, required_stock)
                return result
        
        path = "/overseas/openapi/component/getComponentDetailByCode"
        url = f"{API_BASE_URL}{path}"
        lcsc_numeric = lcsc_code[1:] if lcsc_code.startswith("C") else lcsc_code
        payload = {"componentCodes": [lcsc_numeric]}
        body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        try:
            auth_header = self._get_auth_header("POST", path, body_str)
            headers = {"Content-Type": "application/json", "Authorization": auth_header}
            resp = self.session.post(url, data=body_str, headers=headers, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code in [401, 403]:
                result.error = f"API Auth Error ({resp.status_code})"
                return result
                
            resp.raise_for_status()
            data = resp.json()

            code = data.get("code", 200)
            if code != 200 and code != 0:
                result.error = f"API Error [{code}]: {data.get('message', 'Unknown error')}"
                return result

            response_data = data.get("data", [])
            if isinstance(response_data, dict):
                components = response_data.get("components", []) or response_data.get("componentList", []) or response_data.get("data", [])
            elif isinstance(response_data, list):
                components = response_data
            else:
                components = []

            result.match_count = len(components)
            if not components:
                result.found = False
                return result

            exact_comp = components[0]
            result.found = True
            result.exact_match = True
            result.matched_mpn = mpn
            result.lcsc_code = lcsc_code
            result.stock = int(exact_comp.get("stockCount", 0) or exact_comp.get("stock", 0) or exact_comp.get("availableStock", 0))
            result.package = exact_comp.get("componentSpecification", "") or exact_comp.get("package", "")
            result.category = exact_comp.get("firstTypeName", "") or exact_comp.get("category", "")
            
            price_ranges = exact_comp.get("priceRanges", []) or exact_comp.get("priceList", [])
            result.price_breaks_raw = json.dumps(price_ranges) if price_ranges else ""
            
            if result.price_breaks_raw and required_stock > 0:
                result.unit_price = select_unit_price(result.price_breaks_raw, required_stock)

            if self.db_manager:
                self.db_manager.upsert_api_cache(
                    lcsc_code=lcsc_code,
                    stock=result.stock,
                    price_breaks_raw=result.price_breaks_raw,
                    package=result.package,
                    category=result.category,
                    timestamp=time.time()
                )

            return result

        except Exception as e:
            result.error = f"API Request Exception: {str(e)}"
            return result

    def close(self):
        self.session.close()


def enrich_bom_item(item: BomItem, search_result: JlcpcbSearchResult) -> None:
    """Apply JLCPCB search results to a BomItem in-place."""
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

    def __init__(self, items: list[BomItem], app_id: str, access_key: str, secret_key: str, parent=None, force_refresh: bool = False):
        super().__init__(parent)
        self.items = items
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.force_refresh = force_refresh
        self._cancelled = False
        self._mutex = QMutex()
        self.db_manager = DatabaseManager()

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
        searcher = JlcpcbSearcher(self.app_id, self.access_key, self.secret_key, self.db_manager)
        dk_searcher = DigiKeySearcher()
        total = len(self.items)

        try:
            for idx, item in enumerate(self.items):
                if self._is_cancelled():
                    break

                mpn_display = item.mpn if item.mpn else "(empty)"
                self.progress.emit(idx + 1, total, mpn_display, "Searching JLCPCB...")

                item.required_stock = compute_required_stock(item.quantity)

                is_unapproved = False
                internal_code = item.comment.strip() if item.comment else ""
                
                # Default suggestion values
                suggested_mpn = item.mpn or ""
                suggested_lcsc = ""
                suggested_digikey = ""
                
                if is_res_coded(item.comment):
                    item.status = "RES manual"
                    item.jlcpcb_part_number = ""
                    item.skip_jlcpcb = True
                elif internal_code:
                    mapping = self.db_manager.get_internal_mapping(internal_code)
                    if mapping and mapping.get("approved") == 1:
                        item.mpn = mapping.get("mpn", "")
                        lcsc_code = mapping.get("lcsc_code", "")
                        item.digikey_part_number = mapping.get("digikey_code", "")
                        result = searcher.search_lcsc(lcsc_code, item.mpn, item.required_stock, refresh=self.force_refresh)
                        enrich_bom_item(item, result)
                        item.skip_jlcpcb = True
                    else:
                        is_unapproved = True
                
                # NOTE: If there is no internal_code, we just proceed normally and do NOT skip JLCPCB.

                if not item.skip_jlcpcb:
                    result = searcher.search_mpn(item.mpn, item.required_stock, refresh=self.force_refresh)
                    if result:
                        suggested_mpn = result.matched_mpn or item.mpn
                        suggested_lcsc = result.lcsc_code or ""
                    enrich_bom_item(item, result)

                if dk_searcher.is_configured:
                    self.progress.emit(idx + 1, total, mpn_display, "Searching DigiKey for pricing...")
                    dk_result = dk_searcher.search_item(item)
                    if dk_result:
                        suggested_digikey = dk_result.digikey_part_number or ""
                    enrich_bom_item_digikey(item, dk_result)

                if is_unapproved:
                    self.db_manager.upsert_internal_mapping(
                        comment_code=internal_code,
                        mpn=suggested_mpn,
                        lcsc_code=suggested_lcsc,
                        approved=False,
                        digikey_code=suggested_digikey
                    )
                    item.status = "Pending Approval"
                    item.jlcpcb_part_number = ""
                    
                self.item_result.emit(idx, item)
                self.progress.emit(idx + 1, total, mpn_display, item.status)

            self.finished_all.emit(self.items)

        except Exception as e:
            self.error.emit(str(e))

        finally:
            searcher.close()
            dk_searcher.close()