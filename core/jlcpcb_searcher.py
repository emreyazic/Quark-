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
    is_exact_mpn_match,
    is_res_coded,
    compute_required_stock,
    select_unit_price,
)
from core.database_manager import DatabaseManager

# Resmi JLC Open Platform API Base URL
API_BASE_URL = "https://open.jlcpcb.com"
# MPN -> LCSC eşlemesi için kullanılan hızlı arama servisi
COMMUNITY_SEARCH_URL = "https://jlcsearch.tscircuit.com/components/list.json"
# LCSC's own global search endpoint.  The community index above is fast but
# incomplete for many extended-library parts, so this is an exact-match fallback.
LCSC_GLOBAL_SEARCH_URL = "https://wmsc.lcsc.com/ftps/wm/search/v3/global"

# İstek zaman aşımı (saniye)
REQUEST_TIMEOUT = 15


def select_result_unit_price(price_breaks_raw: str, required_stock: int) -> Optional[float]:
    """Return the unit price for the quantity the BOM actually requires."""
    return select_unit_price(price_breaks_raw, max(required_stock, 1))


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
        """Resolves permanent MPN to LCSC part code (C...) using:
        1. Local JLC library DB (fast, offline)
        2. LCSC global search with exact MPN matching
        3. Community search API (fallback)
        Returns None silently if not found — caller handles pending flow.
        """
        # 1. Önce yerel kütüphane veritabanını sorgula
        if self.db_manager:
            lcsc = self.db_manager.lookup_lcsc_by_mpn(mpn_clean)
            if lcsc:
                lcsc_code = str(lcsc).strip()
                print(f"DEBUG: Found LCSC: {lcsc_code} for MPN: {mpn_clean}")
                return lcsc_code

        # Community fallback helpers
        #    Endpoint: GET /components/list.json?search=<MPN>
        #    mfr alanı MPN'i, lcsc alanı LCSC numarasını içerir
        def get_components(response_data: Any) -> list[dict]:
            """Accept both documented object responses and legacy list responses."""
            if isinstance(response_data, dict):
                components = response_data.get("components", []) or response_data.get("data", [])
            else:
                components = response_data
            return components if isinstance(components, list) else []

        def find_exact_lcsc(components: list[dict]) -> Optional[str]:
            """Return an LCSC code only when the MPN match is unambiguous.

            A search response can contain a similarly named part or multiple
            LCSC entries for the same MPN.  Selecting the first entry in either
            case can silently map a BOM to the wrong component, so those cases
            must remain pending for user review.
            """
            exact_codes: set[str] = set()
            for comp in components:
                if not isinstance(comp, dict):
                    continue
                candidate_mpns = (
                    comp.get("mfr", ""),
                    comp.get("manufacturer_part_number", ""),
                    comp.get("manufacturerPartNumber", ""),
                    comp.get("manufacturerPartNum", ""),
                    comp.get("componentModel", ""),
                    comp.get("productModel", ""),
                    comp.get("mpn", ""),
                )
                if not any(is_exact_mpn_match(mpn_clean, candidate_mpn) for candidate_mpn in candidate_mpns if candidate_mpn):
                    continue
                lcsc_val = (
                    comp.get("lcsc", "")
                    or comp.get("lcscPart", "")
                    or comp.get("lcsc_part_number", "")
                    or comp.get("lcscPartNumber", "")
                    or comp.get("componentCode", "")
                    or comp.get("productCode", "")
                )
                if lcsc_val:
                    lcsc_str = str(lcsc_val).strip()
                    lcsc_code = lcsc_str if lcsc_str.startswith("C") else f"C{lcsc_str}"
                    exact_codes.add(lcsc_code)

            if len(exact_codes) == 1:
                lcsc_code = exact_codes.pop()
                print(f"DEBUG: Found LCSC: {lcsc_code} for MPN: {mpn_clean}")
                return lcsc_code
            if len(exact_codes) > 1:
                print(f"DEBUG: Ambiguous exact LCSC matches for MPN: {mpn_clean}: {', '.join(sorted(exact_codes))}")
            return None

        # LCSC's own endpoint is normally faster and more complete than the
        # community index. Query it first, then retain the community service as
        # a fallback. Exact/ambiguous-match safeguards remain identical.
        try:
            official_resp = self.session.post(
                LCSC_GLOBAL_SEARCH_URL,
                json={"keyword": mpn_clean},
                timeout=8,
                headers={
                    "User-Agent": "BOM-Enrichment-Tool/2.0",
                    "Accept": "application/json",
                    "Origin": "https://www.lcsc.com",
                    "Referer": "https://www.lcsc.com/",
                },
            )
            if official_resp.status_code == 200:
                official_data = official_resp.json()
                official_result = official_data.get("result") if isinstance(official_data, dict) else None
                if isinstance(official_result, dict):
                    exact_products = official_result.get("exactMatchResult") or []
                    search_result = official_result.get("productSearchResultVO") or {}
                    search_products = search_result.get("productList") or [] if isinstance(search_result, dict) else []
                    lcsc_code = find_exact_lcsc(exact_products + search_products)
                    if lcsc_code:
                        return lcsc_code
        except Exception:
            pass

        max_retries = 2
        backoff = 0.5

        for attempt in range(max_retries):
            try:
                resp = self.session.get(
                    COMMUNITY_SEARCH_URL,
                    params={"search": mpn_clean},
                    timeout=8,
                    headers={
                        "User-Agent": "BOM-Enrichment-Tool/2.0",
                        "Accept": "application/json",
                    },
                )
                if resp.status_code == 200:
                    lcsc_code = find_exact_lcsc(get_components(resp.json()))
                    if lcsc_code:
                        return lcsc_code
                    # Tam eşleşme yoksa manufacturer_part_number parametresiyle ikinci bir deneme
                    resp2 = self.session.get(
                        COMMUNITY_SEARCH_URL,
                        params={"manufacturer_part_number": mpn_clean, "limit": 5},
                        timeout=8,
                        headers={
                            "User-Agent": "BOM-Enrichment-Tool/2.0",
                            "Accept": "application/json",
                        },
                    )
                    if resp2.status_code == 200:
                        lcsc_code = find_exact_lcsc(get_components(resp2.json()))
                        if lcsc_code:
                            return lcsc_code

                    # Bulunamadı ama hata yok — sessizce None dön
                    return None
                elif resp.status_code in (429, 503):
                    # Rate limit — tekrar dene
                    if attempt < max_retries - 1:
                        time.sleep(backoff * (2 ** attempt))
                        continue
                # Diğer HTTP hataları — sessizce None dön
                return None
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                return None

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

        # 1. Adım: MPN'i LCSC koduna çözümle (topluluk arama servisi üzerinden)
        lcsc_code = self._resolve_lcsc_from_mpn(mpn_clean)
        if not lcsc_code:
            # Harici servis yanıt vermedi / parçayı bulamadı.
            # Hata verip durmak yerine boş LCSC ile dön;
            # çağıran kod bunu "pending" olarak veritabanına kaydedecek
            # ve kullanıcı ApprovalDialog üzerinden manuel girebilecek.
            result.found = False
            result.lcsc_code = ""
            result.matched_mpn = mpn_clean
            # error değil, sessizce dön — is_unapproved akışına bırak
            return result

        # Use the same direct-code path for MPN and approved-code searches.
        # It includes the LCSC global-search fallback, which provides live
        # stock/pricing when JOP returns no component detail.
        return self.search_lcsc(
            lcsc_code,
            mpn_clean,
            required_stock=required_stock,
            refresh=refresh,
        )

    def search_lcsc(self, lcsc_code: str, mpn: str, required_stock: int = 0, refresh: bool = False) -> JlcpcbSearchResult:
        """Search component directly by LCSC code using API cache or JOP OpenAPI."""
        result = JlcpcbSearchResult()

        def fallback_to_lcsc_global_search() -> JlcpcbSearchResult:
            """Get stock and price when JOP does not expose an extended part."""
            fallback = JlcpcbSearchResult()
            try:
                response = self.session.post(
                    LCSC_GLOBAL_SEARCH_URL,
                    json={"keyword": mpn},
                    timeout=12,
                    headers={
                        "User-Agent": "BOM-Enrichment-Tool/2.0",
                        "Accept": "application/json",
                        "Origin": "https://www.lcsc.com",
                        "Referer": "https://www.lcsc.com/",
                    },
                )
                if response.status_code != 200:
                    return fallback
                payload = response.json()
                data = payload.get("result") if isinstance(payload, dict) else None
                products = data.get("exactMatchResult") or [] if isinstance(data, dict) else []
                target_code = lcsc_code.upper()
                product = next(
                    (
                        candidate for candidate in products
                        if is_exact_mpn_match(mpn, candidate.get("productModel", ""))
                        and str(candidate.get("productCode", "")).upper() == target_code
                    ),
                    None,
                )
                if not product:
                    return fallback

                price_list = product.get("productPriceList") or []
                price_breaks = []
                for index, tier in enumerate(price_list):
                    price = tier.get("usdPrice")
                    if price is None:
                        price = tier.get("productPrice")
                    if price is None:
                        continue
                    quantity = int(tier.get("ladder", 1) or 1)
                    next_quantity = price_list[index + 1].get("ladder") if index + 1 < len(price_list) else None
                    price_breaks.append({
                        "qFrom": quantity,
                        "qTo": int(next_quantity) - 1 if next_quantity else None,
                        "price": float(price),
                    })

                fallback.found = True
                fallback.exact_match = True
                fallback.matched_mpn = mpn
                fallback.lcsc_code = lcsc_code
                fallback.stock = int(product.get("stockNumber", 0) or 0)
                fallback.package = product.get("encapStandard", "") or ""
                fallback.category = product.get("catalogName", "") or ""
                fallback.price_breaks_raw = json.dumps(price_breaks) if price_breaks else ""
                if fallback.price_breaks_raw:
                    fallback.unit_price = select_result_unit_price(fallback.price_breaks_raw, required_stock)
                print(f"DEBUG: Found LCSC fallback data: {lcsc_code} for MPN: {mpn}")
            except Exception:
                pass
            return fallback
        
        if self.db_manager and not refresh:
            cached = self.db_manager.get_api_cache(lcsc_code)
            cache_has_supplier_data = bool(
                cached
                and (
                    int(cached.get("stock", 0) or 0) > 0
                    or bool(cached.get("price_breaks_raw", ""))
                )
            )
            if cached and cache_has_supplier_data and time.time() - cached["timestamp"] < 86400:
                result.found = True
                result.exact_match = True
                result.matched_mpn = mpn
                result.lcsc_code = lcsc_code
                result.stock = cached["stock"]
                result.package = cached["package"]
                result.category = cached["category"]
                result.price_breaks_raw = cached["price_breaks_raw"]
                if result.price_breaks_raw:
                    result.unit_price = select_result_unit_price(result.price_breaks_raw, required_stock)
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
                fallback_result = fallback_to_lcsc_global_search()
                if fallback_result.found:
                    if self.db_manager:
                        self.db_manager.upsert_api_cache(
                            lcsc_code=fallback_result.lcsc_code,
                            stock=fallback_result.stock,
                            price_breaks_raw=fallback_result.price_breaks_raw,
                            package=fallback_result.package,
                            category=fallback_result.category,
                            timestamp=time.time(),
                        )
                    return fallback_result
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
            
            if result.price_breaks_raw:
                result.unit_price = select_result_unit_price(result.price_breaks_raw, required_stock)

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
                        # An approval may intentionally contain only an MPN or
                        # DigiKey code. Do not call the LCSC detail API with an
                        # empty code: it always returns no components and turns
                        # a valid approved mapping into "JLCPCB not found".
                        # Leave such entries on the normal MPN resolver path.
                        if lcsc_code.strip():
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
                    # is_unapproved ise sadece öneri topluyoruz, item durumunu bozmuyoruz
                    if not is_unapproved:
                        enrich_bom_item(item, result)

                if dk_searcher.is_configured and not is_unapproved:
                    # Pending parçalar için DigiKey araması yapmıyoruz — çok yavaş olur
                    # DigiKey önerisi ancak mevcut DB'de boşsa aranır
                    self.progress.emit(idx + 1, total, mpn_display, "Searching DigiKey for pricing...")
                    # For approved mappings the item already has a DigiKey
                    # catalogue number. ``search_item`` treats that number as
                    # an MPN and rejects the returned manufacturer's MPN as a
                    # non-exact match. Search by the real MPN for pricing.
                    dk_result = (
                        dk_searcher.search_mpn(item.mpn, item.required_stock)
                        if item.digikey_part_number
                        else dk_searcher.search_item(item)
                    )
                    if dk_result:
                        suggested_digikey = dk_result.digikey_part_number or ""
                    enrich_bom_item_digikey(item, dk_result)
                elif dk_searcher.is_configured and is_unapproved:
                    # Pending için DigiKey önerisini sadece DB'de hiç kayıt yokken topla
                    existing = self.db_manager.get_internal_mapping(internal_code)
                    if existing is None or not existing.get("digikey_code"):
                        self.progress.emit(idx + 1, total, mpn_display, "Searching DigiKey suggestion...")
                        try:
                            dk_result = dk_searcher.search_item(item)
                            if dk_result:
                                suggested_digikey = dk_result.digikey_part_number or ""
                        except Exception:
                            pass

                if is_unapproved:
                    # Kullanıcının ApprovalDialog'da manuel girdiği verileri koruyarak
                    # sadece boş alanları doldur
                    self.db_manager.insert_pending_suggestion(
                        comment_code=internal_code,
                        mpn=suggested_mpn,
                        lcsc_code=suggested_lcsc,
                        digikey_code=suggested_digikey,
                    )
                    item.status = "Pending Approval"
                    item.jlcpcb_part_number = ""

                self.item_result.emit(idx, item)
                self.progress.emit(idx + 1, total, mpn_display, item.status)

            self.finished_all.emit(self.items)

        except Exception as e:
            self.error.emit(str(e))


# ═══════════════════════════════════════════════════════════════════
#  Library Sync Worker
# ═══════════════════════════════════════════════════════════════════

class LibrarySyncWorker(QThread):
    """Background worker that syncs the JOP component library to the local SQLite DB."""

    progress = pyqtSignal(int, int, str)   # fetched, total, message
    finished = pyqtSignal(int)             # total records written
    error = pyqtSignal(str)

    # JOP API kütüphane listesi endpoint'i
    LIBRARY_PATH = "/overseas/openapi/component/getComponentLibraryList"
    PAGE_SIZE = 100
    MAX_PAGES = 2000  # güvenlik sınırı (~200k parça)

    def __init__(self, app_id: str, access_key: str, secret_key: str, db_manager: DatabaseManager, parent=None):
        super().__init__(parent)
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.db_manager = db_manager
        self._cancelled = False
        self._mutex = QMutex()
        self._searcher = JlcpcbSearcher(app_id, access_key, secret_key, db_manager)

    def cancel(self):
        self._mutex.lock()
        try:
            self._cancelled = True
        finally:
            self._mutex.unlock()

    def _is_cancelled(self) -> bool:
        self._mutex.lock()
        try:
            return self._cancelled
        finally:
            self._mutex.unlock()

    def run(self):
        total_written = 0
        page = 1
        url = f"{API_BASE_URL}{self.LIBRARY_PATH}"

        try:
            while page <= self.MAX_PAGES:
                if self._is_cancelled():
                    break

                payload = {"pageIndex": page, "pageSize": self.PAGE_SIZE}
                body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

                try:
                    auth_header = self._searcher._get_auth_header("POST", self.LIBRARY_PATH, body_str)
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": auth_header,
                        # Gerçekçi tarayıcı başlıkları — WAF / Cloudflare engelini aşmak için
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://jlcpcb.com/",
                        "Origin": "https://jlcpcb.com",
                    }
                    resp = self._searcher.session.post(url, data=body_str, headers=headers, timeout=30)

                    # 403 / WAF engeli — çökme yerine kullanıcıya bilgi ver ve dur
                    if resp.status_code == 403:
                        self.error.emit(
                            "403 Forbidden: JLCPCB API access denied (WAF/Cloudflare block).\n\n"
                            "The bulk library download is not available via the API at this time.\n"
                            "You can still manually enter LCSC codes in the Approval Dialog."
                        )
                        return

                    resp.raise_for_status()
                    data = resp.json()

                except Exception as e:
                    err_str = str(e)
                    # HTTP 403 requests exception olarak da gelebilir
                    if "403" in err_str:
                        self.error.emit(
                            "403 Forbidden: JLCPCB API access denied (WAF/Cloudflare block).\n\n"
                            "The bulk library download is not available via the API at this time.\n"
                            "You can still manually enter LCSC codes in the Approval Dialog."
                        )
                    else:
                        self.error.emit(f"API request failed on page {page}: {err_str}")
                    return

                code = data.get("code", 200)
                if code not in (200, 0):
                    self.error.emit(f"API Error [{code}]: {data.get('message', 'Unknown error')}")
                    return

                response_data = data.get("data", {})
                if isinstance(response_data, dict):
                    components = (
                        response_data.get("componentList", [])
                        or response_data.get("components", [])
                        or response_data.get("data", [])
                    )
                    total = response_data.get("totalCount", 0) or response_data.get("total", 0)
                elif isinstance(response_data, list):
                    components = response_data
                    total = 0
                else:
                    components = []
                    total = 0

                if not components:
                    # Son sayfa — daha fazla veri yok
                    break

                records = []
                for comp in components:
                    # JOP API alan isimleri farklı versiyonlarda değişiyor, hepsini dene
                    lcsc_raw = (
                        comp.get("componentCode", "")
                        or comp.get("lcscCode", "")
                        or comp.get("lcsc", "")
                    )
                    if not lcsc_raw:
                        continue
                    lcsc_str = str(lcsc_raw).strip()
                    if not lcsc_str.startswith("C"):
                        lcsc_str = f"C{lcsc_str}"

                    mpn = (
                        comp.get("componentModel", "")
                        or comp.get("mfr", "")
                        or comp.get("manufacturerPartNumber", "")
                        or comp.get("mpn", "")
                    ).strip()
                    if not mpn:
                        continue

                    records.append({
                        "lcsc_code": lcsc_str,
                        "mpn": mpn,
                        "manufacturer": comp.get("brandName", "") or comp.get("manufacturer", ""),
                        "description": comp.get("describe", "") or comp.get("description", ""),
                        "package": comp.get("componentSpecification", "") or comp.get("package", ""),
                        "category": comp.get("firstTypeName", "") or comp.get("category", ""),
                        "subcategory": comp.get("secondTypeName", "") or comp.get("subcategory", ""),
                    })

                written = self.db_manager.bulk_upsert_library(records)
                total_written += written

                self.progress.emit(
                    total_written,
                    total if total > 0 else total_written + self.PAGE_SIZE,
                    f"Page {page}: +{written} records ({total_written} total)"
                )

                if len(components) < self.PAGE_SIZE:
                    # Son sayfa
                    break

                page += 1

            self.finished.emit(total_written)

        except Exception as e:
            self.error.emit(str(e))
