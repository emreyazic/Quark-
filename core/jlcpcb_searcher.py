"""JLCPCB parts searcher using a hybrid approach:
1. Maps permanent MPN to LCSC code.
2. Fetches real-time stock, package, category, and pricing via official JLC Open Platform (JOP) API.
"""

import time
import json
import copy
import base64
import hmac
import hashlib
import random
import re
import string
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Optional, cast
from urllib.parse import quote
import requests
from models.bom_item import BomItem
from core.mpn_utils import (
    clean_mpn_value,
    is_exact_mpn_match,
    is_res_coded,
    compute_required_stock,
    select_unit_price,
    select_digikey_price,
    normalize_jlcpcb_price_breaks,
)
from core.database_manager import DatabaseManager
from core.logger import get_logger, mask_secret
from core.supplier_availability import AvailabilityState, normalize_availability

logger = get_logger(__name__)

# Resmi JLC Open Platform API Base URL
API_BASE_URL = "https://open.jlcpcb.com"
# MPN -> LCSC eşlemesi için kullanılan hızlı arama servisi
COMMUNITY_SEARCH_URL = "https://jlcsearch.tscircuit.com/components/list.json"
# LCSC's own global search endpoint.  The community index above is fast but
# incomplete for many extended-library parts, so this is an exact-match fallback.
LCSC_GLOBAL_SEARCH_URL = "https://wmsc.lcsc.com/ftps/wm/search/v3/global"
JLCPCB_PART_DETAIL_URL = "https://jlcpcb.com/partdetail/{lcsc_code}"

# İstek zaman aşımı (saniye)
REQUEST_TIMEOUT = 15

SOURCE_JOP = "JOP"
SOURCE_PAGE_FALLBACK = "PAGE_FALLBACK"
SOURCE_CACHE = "CACHE"
SOURCE_LCSC_GLOBAL = "LCSC_GLOBAL"


def select_result_unit_price(price_breaks_raw: str, required_stock: int) -> Optional[float]:
    """Return the unit price for the quantity the BOM actually requires."""
    return select_unit_price(price_breaks_raw, max(required_stock, 1))


def _price_break_signature(price_breaks_raw: str):
    """Return a stable representation for comparing supplier price tiers."""
    if not price_breaks_raw:
        return ()
    try:
        tiers = json.loads(price_breaks_raw)
        if not isinstance(tiers, list):
            return ((price_breaks_raw.strip(),),)
        signature = []
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            q_to = tier.get("qTo")
            price_val = tier.get("price")
            if price_val is None:
                continue
            signature.append((
                int(tier.get("qFrom", 0) or 0),
                None if q_to is None else int(q_to),
                round(float(price_val), 10),
            ))
        return tuple(signature)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ((price_breaks_raw.strip(),),)


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
        self.minimum_order_quantity: Optional[int] = None
        self.order_multiple: int = 1
        self.currency: str = "USD"
        self.package: str = ""
        self.category: str = ""
        self.subcategory: str = ""
        self.description: str = ""
        self.is_basic: bool = False
        self.is_preferred: bool = False
        self.match_count: int = 0
        self.error: Optional[str] = None
        self.candidates: list[dict] = []
        self.data_source: str = ""
        self.warnings: list[str] = []
        self.availability: AvailabilityState = AvailabilityState.UNKNOWN
        self.preorder_only: bool = False

    def is_purchasable(self, required_stock: int) -> bool:
        return bool(
            self.found
            and self.exact_match
            and self.lcsc_code
            and self.availability != AvailabilityState.PREORDER
            and self.unit_price is not None
            and self.stock >= max(int(required_stock or 0), 1)
        )


class JlcpcbSearcher:
    """Searches and enriches JLCPCB parts using MPN resolution + Official JOP API."""
    def __init__(
        self,
        app_id: str = "",
        access_key: str = "",
        secret_key: str = "",
        db_manager: Optional[DatabaseManager] = None,
        _sleep_fn: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ):
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.db_manager = db_manager
        self._sleep_fn = _sleep_fn
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.last_resolution_failed = False
        self.last_resolution_error: Optional[str] = None
        self.last_resolution_preorder_only = False
        self._part_page_cache: Dict[str, tuple[Optional[int], str, Optional[str]]] = {}
        self._part_page_availability: Dict[str, AvailabilityState] = {}
        self._logged_preorder_candidates: set[tuple[str, str]] = set()

    def _log_preorder_candidate(self, lcsc_code: str, mpn: str) -> None:
        key = (lcsc_code.upper(), mpn.casefold())
        if key in self._logged_preorder_candidates:
            return
        self._logged_preorder_candidates.add(key)
        logger.info(
            "JLCPCB_CANDIDATE_SKIPPED reason=preorder lcsc=%s mpn=%s",
            lcsc_code,
            mpn,
        )

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Perform HTTP request with exponential backoff + jitter on timeout, 429, and 5xx."""
        attempt = 0
        method_upper = method.upper()
        while True:
            try:
                if method_upper == "GET":
                    resp = self.session.get(url, **kwargs)
                elif method_upper == "POST":
                    resp = self.session.post(url, **kwargs)
                else:
                    resp = self.session.request(method, url, **kwargs)

                if resp is not None and getattr(resp, "status_code", None) in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    attempt += 1
                    retry_after_hdr = getattr(resp, "headers", {}).get("Retry-After") if hasattr(resp, "headers") and resp.headers else None
                    if retry_after_hdr and str(retry_after_hdr).isdigit():
                        delay = min(int(retry_after_hdr), 30)
                    else:
                        delay = min(10.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5))
                    logger.warning("HTTP %d on %s, retrying attempt %d/%d after %.2fs", resp.status_code, url, attempt, self.max_retries, delay)
                    self._sleep_fn(delay)
                    continue
                return resp
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    delay = min(10.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5))
                    logger.warning("Network error (%s) on %s, retrying attempt %d/%d after %.2fs", type(exc).__name__, url, attempt, self.max_retries, delay)
                    self._sleep_fn(delay)
                    continue
                raise

    @staticmethod
    def _normalize_jop_price_ranges(price_ranges: list[dict]) -> str:
        """Convert JOP's price fields to the common qFrom/qTo/price shape."""
        normalized = []
        for tier in price_ranges or []:
            if not isinstance(tier, dict):
                continue
            quantity_from = tier.get("qFrom")
            if quantity_from is None:
                quantity_from = tier.get("startQuantity")
            quantity_to = tier.get("qTo")
            if quantity_to is None:
                quantity_to = tier.get("endQuantity")
            price = tier.get("price")
            if price is None:
                price = tier.get("unitPrice")
            if price is None:
                price = tier.get("productPrice")
            if quantity_from is None or price is None:
                continue
            quantity_to_value = int(quantity_to) if quantity_to is not None else None
            normalized.append({
                "qFrom": int(quantity_from),
                "qTo": None if quantity_to_value == -1 else quantity_to_value,
                "price": float(price),
            })
        return json.dumps(normalized) if normalized else ""

    @staticmethod
    def _extract_json_from_script_tags(html: str) -> list[Any]:
        """Extract structured JSON objects from <script> tags in HTML."""
        json_objects = []
        script_pattern = re.compile(
            r'<script[^>]*?(?:type=["\']application/json["\']|id=["\']__(?:NEXT|NUXT)_DATA__[\'"])[^>]*?>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        )
        for match in script_pattern.finditer(html):
            content = match.group(1).strip()
            if content:
                try:
                    json_objects.append(json.loads(content))
                except Exception:
                    pass

        state_pattern = re.compile(
            r'(?:window\.__INITIAL_STATE__|window\.__DATA__|window\.__NUXT__)\s*=\s*(\{.*?\});',
            re.DOTALL,
        )
        for match in state_pattern.finditer(html):
            content = match.group(1).strip()
            if content:
                try:
                    json_objects.append(json.loads(content))
                except Exception:
                    pass

        return json_objects

    @classmethod
    def _find_component_in_json(cls, data: Any, lcsc_code: str) -> Optional[dict]:
        """Search recursively for a component dict in nested JSON data."""
        target = lcsc_code.upper()
        if isinstance(data, dict):
            code_val = (
                data.get("componentCode")
                or data.get("component_code")
                or data.get("productCode")
                or data.get("lcscCode")
            )
            if code_val and str(code_val).strip().upper() == target:
                return data
            for val in data.values():
                found = cls._find_component_in_json(val, target)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = cls._find_component_in_json(item, target)
                if found is not None:
                    return found
        return None

    @classmethod
    def _extract_stock_and_prices_from_dict(cls, comp_dict: dict) -> tuple[Optional[int], str]:
        stock = None
        for k in ("overseasStockCount", "stockCount", "stock", "availableStock", "overseasStock"):
            if k in comp_dict and comp_dict[k] is not None:
                try:
                    stock = int(comp_dict[k])
                    break
                except (ValueError, TypeError):
                    pass

        raw_tiers = (
            comp_dict.get("priceRanges")
            or comp_dict.get("priceList")
            or comp_dict.get("pricesList")
            or comp_dict.get("prices")
        )
        price_breaks = []
        if isinstance(raw_tiers, list):
            for tier in raw_tiers:
                if not isinstance(tier, dict):
                    continue
                q_from = tier.get("startNumber") if "startNumber" in tier else (tier.get("qFrom") or tier.get("startQuantity"))
                q_to = tier.get("endNumber") if "endNumber" in tier else (tier.get("qTo") or tier.get("endQuantity"))
                price = tier.get("productPrice") if "productPrice" in tier else (tier.get("unitPrice") or tier.get("price"))
                if q_from is not None and price is not None:
                    try:
                        q_from_int = int(q_from)
                        q_to_int = None if q_to in (None, -1, "-1", "") else int(q_to)
                        price_float = float(price)
                        price_breaks.append({
                            "qFrom": q_from_int,
                            "qTo": q_to_int,
                            "price": price_float,
                        })
                    except (ValueError, TypeError):
                        pass
        price_breaks.sort(key=lambda tier: tier["qFrom"])
        return stock, json.dumps(price_breaks) if price_breaks else ""

    @classmethod
    def _extract_via_regex(cls, html: str, lcsc_code: str) -> tuple[Optional[int], str]:
        escaped_code = re.escape(lcsc_code.upper())
        stock_pattern = re.compile(
            rf'\\?"componentCode\\?":\\?"{escaped_code}\\?"'
            rf'.{{0,1200}}?\\?"overseasStockCount\\?":(\d+)',
            re.DOTALL,
        )
        stock_matches = stock_pattern.findall(html)
        stock = int(stock_matches[0]) if stock_matches else None

        tier_pattern = re.compile(
            rf'\\?"componentCode\\?":\\?"{escaped_code}\\?"'
            rf'.{{0,600}}?\\?"startNumber\\?":(\d+),'
            rf'\\?"endNumber\\?":(-?\d+),'
            rf'\\?"productPrice\\?":([0-9.eE+-]+)',
            re.DOTALL,
        )
        price_breaks = []
        seen_tiers = set()
        for quantity_from, quantity_to, price in tier_pattern.findall(html):
            tier_key = (int(quantity_from), int(quantity_to), float(price))
            if tier_key in seen_tiers:
                continue
            seen_tiers.add(tier_key)
            price_breaks.append({
                "qFrom": tier_key[0],
                "qTo": None if tier_key[1] == -1 else tier_key[1],
                "price": tier_key[2],
            })
        price_breaks.sort(key=lambda tier: tier["qFrom"])
        return stock, json.dumps(price_breaks) if price_breaks else ""

    def _fetch_jlcpcb_part_page_data(self, lcsc_code: str) -> tuple[Optional[int], str, Optional[str]]:
        """Read JLCPCB's displayed overseas stock and price tiers.

        Attempts structured JSON/script parsing first, falling back to regex.
        Returns:
            tuple (stock, price_breaks_json, error_message_if_failed)
        """
        cache_key = lcsc_code.upper()
        if cache_key in self._part_page_cache:
            return self._part_page_cache[cache_key]
        self._part_page_availability[cache_key] = AvailabilityState.UNKNOWN

        try:
            response = self._request_with_retry(
                "GET",
                JLCPCB_PART_DETAIL_URL.format(lcsc_code=quote(lcsc_code)),
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BOM-Enrichment-Tool/2.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            response.raise_for_status()
            html = response.text
        except requests.exceptions.RequestException as exc:
            err = f"HTTP/Network error: {exc}"
            result = (None, "", err)
            self._part_page_cache[cache_key] = result
            return result
        except Exception as exc:
            err = f"Page request failed: {exc}"
            result = (None, "", err)
            self._part_page_cache[cache_key] = result
            return result

        stock = None
        price_breaks = ""
        parse_error = None

        # 1. Structured JSON parsing
        try:
            json_objects = self._extract_json_from_script_tags(html)
            for obj in json_objects:
                comp = self._find_component_in_json(obj, lcsc_code)
                if comp:
                    stock, price_breaks = self._extract_stock_and_prices_from_dict(comp)
                    self._part_page_availability[cache_key] = normalize_availability(comp, stock)
                    if (
                        self._part_page_availability[cache_key] == AvailabilityState.PREORDER
                        or stock is not None
                        or price_breaks
                    ):
                        break
        except Exception as exc:
            parse_error = f"JSON parse error: {exc}"

        # 2. Regex fallback
        if stock is None or not price_breaks:
            try:
                reg_stock, reg_price_breaks = self._extract_via_regex(html, lcsc_code)
                if stock is None:
                    stock = reg_stock
                if not price_breaks:
                    price_breaks = reg_price_breaks
            except Exception as exc:
                if not parse_error:
                    parse_error = f"Regex parse error: {exc}"

        error_message = None
        if stock is None and not price_breaks:
            error_message = parse_error or "Component data not found in page HTML"

        result = (stock, price_breaks, error_message)
        self._part_page_cache[cache_key] = result
        return result

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

    def _resolve_lcsc_from_mpn(
        self,
        mpn_clean: str,
        excluded_codes: Optional[set[str]] = None,
        skip_local: bool = False,
    ) -> Optional[str]:
        """Resolves permanent MPN to LCSC part code (C...) using:
        1. Local JLC library DB (fast, offline)
        2. LCSC global search with exact MPN matching
        3. Community search API (fallback)
        Returns None silently if not found — caller handles pending flow.
        """
        self.last_resolution_failed = False
        self.last_resolution_error = None
        self.last_resolution_preorder_only = False
        if not mpn_clean:
            return None

        excluded = {code.upper() for code in (excluded_codes or set())}
        preorder_matches = 0

        completed_remote_lookup = False
        resolution_errors = []

        # 1. Önce yerel kütüphane veritabanını sorgula
        if self.db_manager and not skip_local:
            lcsc = self.db_manager.lookup_lcsc_by_mpn(mpn_clean)
            if lcsc:
                lcsc_code = str(lcsc).strip()
                if lcsc_code.upper() not in excluded:
                    logger.debug("Found LCSC: %s for MPN: %s", lcsc_code, mpn_clean)
                    return lcsc_code

        # Community fallback helpers
        def get_components(response_data: Any) -> list[dict]:
            """Accept both documented object responses and legacy list responses."""
            if isinstance(response_data, dict):
                components = response_data.get("components", []) or response_data.get("data", [])
            else:
                components = response_data
            return components if isinstance(components, list) else []

        def find_exact_lcsc(components: list[dict]) -> Optional[str]:
            nonlocal preorder_matches
            eligible: dict[str, tuple[int, int, str]] = {}
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
                    if lcsc_code.upper() in excluded:
                        continue
                    stock_value = next(
                        (comp.get(key) for key in ("stockNumber", "stockCount", "stock", "availableStock") if comp.get(key) is not None),
                        None,
                    )
                    availability = normalize_availability(comp, stock_value)
                    if availability == AvailabilityState.PREORDER:
                        preorder_matches += 1
                        self._log_preorder_candidate(lcsc_code, mpn_clean)
                        continue
                    try:
                        stock = int(stock_value or 0)
                    except (TypeError, ValueError):
                        stock = 0
                    priority = 0 if availability == AvailabilityState.IN_STOCK else 1
                    eligible[lcsc_code.upper()] = (priority, -stock, lcsc_code)

            if eligible:
                lcsc_code = min(eligible.values())[2]
                logger.debug("Found LCSC: %s for MPN: %s", lcsc_code, mpn_clean)
                return lcsc_code
            return None

        try:
            official_resp = self._request_with_retry(
                "POST",
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
                completed_remote_lookup = True
                official_data = official_resp.json()
                official_result = official_data.get("result") if isinstance(official_data, dict) else None
                if isinstance(official_result, dict):
                    exact_products = official_result.get("exactMatchResult") or []
                    search_result = official_result.get("productSearchResultVO") or {}
                    search_products = search_result.get("productList") or [] if isinstance(search_result, dict) else []
                    lcsc_code = find_exact_lcsc(exact_products + search_products)
                    if lcsc_code:
                        return lcsc_code
            else:
                resolution_errors.append(
                    f"LCSC search HTTP {official_resp.status_code}"
                )
        except Exception as exc:
            resolution_errors.append(f"LCSC search request failed: {exc}")

        for attempt in range(self.max_retries):
            try:
                resp = self._request_with_retry(
                    "GET",
                    COMMUNITY_SEARCH_URL,
                    params={"search": mpn_clean},
                    timeout=8,
                    headers={
                        "User-Agent": "BOM-Enrichment-Tool/2.0",
                        "Accept": "application/json",
                    },
                )
                if resp.status_code == 200:
                    completed_remote_lookup = True
                    lcsc_code = find_exact_lcsc(get_components(resp.json()))
                    if lcsc_code:
                        return lcsc_code
                    resp2 = self._request_with_retry(
                        "GET",
                        COMMUNITY_SEARCH_URL,
                        params={"manufacturer_part_number": mpn_clean, "limit": 5},
                        timeout=8,
                        headers={
                            "User-Agent": "BOM-Enrichment-Tool/2.0",
                            "Accept": "application/json",
                        },
                    )
                    if resp2.status_code == 200:
                        lcsc_code2 = find_exact_lcsc(get_components(resp2.json()))
                        if lcsc_code2:
                            return lcsc_code2
                    # Service responded 200 without match
                    break
                else:
                    resolution_errors.append(
                        f"Community search HTTP {resp.status_code}"
                    )
            except Exception as exc:
                resolution_errors.append(f"Community search request failed: {exc}")
                break

        self.last_resolution_failed = not completed_remote_lookup
        self.last_resolution_preorder_only = bool(preorder_matches and completed_remote_lookup)
        if self.last_resolution_failed:
            self.last_resolution_error = "; ".join(resolution_errors) or "MPN lookup services did not respond"
        return None


    def search_mpn(
        self,
        mpn: str,
        required_stock: int = 0,
        refresh: bool = False,
        excluded_lcsc_codes: Optional[set[str]] = None,
    ) -> JlcpcbSearchResult:
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
        lcsc_code = self._resolve_lcsc_from_mpn(
            mpn_clean,
            excluded_codes=excluded_lcsc_codes,
            skip_local=bool(excluded_lcsc_codes),
        )
        if not lcsc_code:
            # Harici servis yanıt vermedi / parçayı bulamadı.
            # Hata verip durmak yerine boş LCSC ile dön;
            # çağıran kod bunu "pending" olarak veritabanına kaydedecek
            # ve kullanıcı ApprovalDialog üzerinden manuel girebilecek.
            result.found = False
            result.lcsc_code = ""
            result.matched_mpn = mpn_clean
            if self.last_resolution_preorder_only:
                result.availability = AvailabilityState.PREORDER
                result.preorder_only = True
            if self.last_resolution_failed:
                result.error = self.last_resolution_error or "MPN lookup failed"
            return result

        # Use the same direct-code path for MPN and approved-code searches.
        # It includes the LCSC global-search fallback, which provides live
        # stock/pricing when JOP returns no component detail.
        resolved = self.search_lcsc(
            lcsc_code,
            mpn_clean,
            required_stock=required_stock,
            refresh=refresh,
        )
        if resolved.availability != AvailabilityState.PREORDER:
            return resolved

        alternative_code = self._resolve_lcsc_from_mpn(
            mpn_clean,
            excluded_codes={lcsc_code, *(excluded_lcsc_codes or set())},
            skip_local=True,
        )
        if alternative_code:
            alternative = self.search_lcsc(
                alternative_code,
                mpn_clean,
                required_stock=required_stock,
                refresh=refresh,
            )
            if alternative.availability != AvailabilityState.PREORDER:
                return alternative
        resolved.preorder_only = True
        return resolved

    def search_lcsc(self, lcsc_code: str, mpn: str, required_stock: int = 0, refresh: bool = False) -> JlcpcbSearchResult:
        """Search component directly by LCSC code using API cache or JOP OpenAPI."""
        result = JlcpcbSearchResult()

        def fallback_to_lcsc_global_search() -> JlcpcbSearchResult:
            """Get stock and price when JOP does not expose an extended part."""
            fallback = JlcpcbSearchResult()
            try:
                response = self._request_with_retry(
                    "POST",
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

                fallback.availability = normalize_availability(
                    product, product.get("stockNumber")
                )
                if fallback.availability == AvailabilityState.PREORDER:
                    fallback.exact_match = True
                    fallback.matched_mpn = mpn
                    fallback.lcsc_code = lcsc_code
                    fallback.preorder_only = True
                    self._log_preorder_candidate(lcsc_code, mpn)
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
                if fallback.availability == AvailabilityState.UNKNOWN:
                    fallback.availability = normalize_availability(None, fallback.stock)
                fallback.data_source = SOURCE_LCSC_GLOBAL
                fallback.package = product.get("encapStandard", "") or ""
                fallback.category = product.get("catalogName", "") or ""
                fallback.price_breaks_raw = json.dumps(price_breaks) if price_breaks else ""
                if fallback.price_breaks_raw:
                    fallback.unit_price = select_result_unit_price(fallback.price_breaks_raw, required_stock)
                logger.debug("Found LCSC fallback data: %s for MPN: %s", lcsc_code, mpn)
            except Exception:
                pass
            return fallback
        
        if self.db_manager and not refresh:
            cached = self.db_manager.get_api_cache(lcsc_code)
            cache_source_is_current = bool(
                cached
                and (
                    cached.get("source") in (SOURCE_JOP, SOURCE_PAGE_FALLBACK, SOURCE_LCSC_GLOBAL)
                    or cached.get("source") == "JLCPCB_PAGE_V1"
                    or str(cached.get("source", "")).startswith("JLCPCB_OPENAPI_V2")
                )
            )
            cache_has_supplier_data = bool(
                cached
                and (
                    int(cached.get("stock", 0) or 0) > 0
                    or bool(cached.get("price_breaks_raw", ""))
                )
            )
            if cached and cache_source_is_current and cache_has_supplier_data and time.time() - cached["timestamp"] < 86400:
                result.found = True
                result.exact_match = True
                result.matched_mpn = mpn
                result.lcsc_code = lcsc_code
                result.stock = cached["stock"]
                result.package = cached["package"]
                result.category = cached["category"]
                result.price_breaks_raw = cached["price_breaks_raw"]
                result.data_source = SOURCE_CACHE
                try:
                    result.availability = AvailabilityState(cached.get("availability") or "UNKNOWN")
                except ValueError:
                    result.availability = normalize_availability(None, result.stock)
                if result.availability == AvailabilityState.PREORDER:
                    result.found = False
                    result.preorder_only = True
                    self._log_preorder_candidate(lcsc_code, mpn)
                    return result
                if result.price_breaks_raw:
                    result.unit_price = select_result_unit_price(result.price_breaks_raw, required_stock)
                return result
        
        path = "/overseas/openapi/component/getComponentDetailByCode"
        url = f"{API_BASE_URL}{path}"
        # JOP requires the complete catalogue number (for example C127833).
        # Removing the C prefix returns HTTP 200 with an empty data array.
        normalized_lcsc_code = lcsc_code if lcsc_code.upper().startswith("C") else f"C{lcsc_code}"
        payload = {"componentCodes": [normalized_lcsc_code.upper()]}
        body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        try:
            auth_header = self._get_auth_header("POST", path, body_str)
            headers = {"Content-Type": "application/json", "Authorization": auth_header}
            resp = self._request_with_retry("POST", url, data=body_str, headers=headers, timeout=REQUEST_TIMEOUT)
            
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
                if fallback_result.found or fallback_result.availability == AvailabilityState.PREORDER:
                    if self.db_manager:
                        self.db_manager.upsert_api_cache(
                            lcsc_code=fallback_result.lcsc_code,
                            stock=fallback_result.stock,
                            price_breaks_raw=fallback_result.price_breaks_raw,
                            package=fallback_result.package,
                            category=fallback_result.category,
                            timestamp=time.time(),
                            source=fallback_result.data_source,
                            availability=fallback_result.availability.value,
                        )
                    return fallback_result
                result.found = False
                return result

            exact_comp = components[0]
            result.exact_match = True
            result.matched_mpn = mpn
            result.lcsc_code = lcsc_code
            stock_keys = ("stockCount", "stock", "availableStock")
            official_stock_available = any(
                key in exact_comp and exact_comp.get(key) is not None
                for key in stock_keys
            )
            official_stock = next(
                (
                    exact_comp[key]
                    for key in stock_keys
                    if key in exact_comp and exact_comp[key] is not None
                ),
                0,
            )
            result.stock = int(official_stock)
            result.availability = normalize_availability(
                exact_comp, result.stock if official_stock_available else None
            )
            result.data_source = SOURCE_JOP
            result.package = exact_comp.get("componentSpecification", "") or exact_comp.get("package", "")
            result.category = exact_comp.get("firstTypeName", "") or exact_comp.get("category", "")

            if result.availability == AvailabilityState.PREORDER:
                result.found = False
                result.preorder_only = True
                self._log_preorder_candidate(lcsc_code, mpn)
                if self.db_manager:
                    self.db_manager.upsert_api_cache(
                        lcsc_code=lcsc_code,
                        stock=result.stock,
                        price_breaks_raw="",
                        package=result.package,
                        category=result.category,
                        timestamp=time.time(),
                        source=result.data_source,
                        availability=result.availability.value,
                    )
                return result

            result.found = True
            
            price_ranges = exact_comp.get("priceRanges", []) or exact_comp.get("priceList", [])
            result.price_breaks_raw = self._normalize_jop_price_ranges(price_ranges)
            currency = exact_comp.get("priceCurrency") or exact_comp.get("currency")
            if isinstance(currency, str) and currency.strip():
                result.currency = currency.strip().upper()
            try:
                moq = int(
                    exact_comp.get("minimumOrderQuantity")
                    or exact_comp.get("minOrderQuantity")
                    or exact_comp.get("moq")
                    or 0
                )
                result.minimum_order_quantity = moq if moq > 0 else None
            except (TypeError, ValueError):
                result.minimum_order_quantity = None
            try:
                multiple = int(
                    exact_comp.get("orderMultiple")
                    or exact_comp.get("orderIncrement")
                    or 1
                )
                result.order_multiple = multiple if multiple > 0 else 1
            except (TypeError, ValueError):
                result.order_multiple = 1

            needs_page_stock = not official_stock_available
            needs_page_prices = not result.price_breaks_raw
            page_stock = None
            page_price_breaks = ""
            page_error = None
            if needs_page_stock or needs_page_prices:
                page_stock, page_price_breaks, page_error = self._fetch_jlcpcb_part_page_data(lcsc_code)
                page_availability = self._part_page_availability.get(
                    lcsc_code.upper(), AvailabilityState.UNKNOWN
                )
                if page_availability == AvailabilityState.PREORDER:
                    result.found = False
                    result.availability = AvailabilityState.PREORDER
                    result.preorder_only = True
                    result.price_breaks_raw = ""
                    result.unit_price = None
                    self._log_preorder_candidate(lcsc_code, mpn)
                    return result
                if result.availability == AvailabilityState.UNKNOWN and page_stock is not None:
                    result.availability = normalize_availability(None, page_stock)
            used_page_fallback = False
            if not official_stock_available:
                if page_stock is not None:
                    result.stock = page_stock
                    used_page_fallback = True
                elif page_error:
                    result.warnings.append(
                        f"Official stock missing and page fallback failed: {page_error}"
                    )

            if not result.price_breaks_raw:
                if page_price_breaks:
                    result.price_breaks_raw = page_price_breaks
                    used_page_fallback = True
                elif page_error:
                    result.warnings.append(
                        f"Official price tiers missing and page fallback failed: {page_error}"
                    )
            if used_page_fallback:
                result.data_source = SOURCE_PAGE_FALLBACK

            normalized_breaks, normalization_warnings = normalize_jlcpcb_price_breaks(
                result.price_breaks_raw
            )
            result.price_breaks_raw = (
                json.dumps(normalized_breaks) if normalized_breaks else ""
            )
            result.warnings.extend(normalization_warnings)

            if result.price_breaks_raw:
                result.unit_price = select_result_unit_price(result.price_breaks_raw, required_stock)

            if self.db_manager:
                self.db_manager.upsert_api_cache(
                    lcsc_code=lcsc_code,
                    stock=result.stock,
                    price_breaks_raw=result.price_breaks_raw,
                    package=result.package,
                    category=result.category,
                    timestamp=time.time(),
                    source=result.data_source,
                    availability=result.availability.value,
                )

            return result

        except Exception as e:
            result.error = f"API Request Exception: {str(e)}"
            return result

    def close(self):
        self.session.close()


def clear_jlcpcb_live_data(item: BomItem) -> None:
    """Clear only refreshable JLCPCB observations, preserving approved codes."""
    item.available_stock_qty = None
    item.unit_price = None
    item.jlcpcb_price_breaks_raw = ""
    item.jlcpcb_min_order_quantity = None
    item.jlcpcb_order_multiple = 1
    item.jlcpcb_currency = "USD"
    item.jlcpcb_total_price = None
    item.jlcpcb_status = "not_searched"
    item.jlcpcb_availability = AvailabilityState.UNKNOWN.value
    item.jlcpcb_error = ""

    item.jlcpcb_source = ""


def enrich_bom_item(item: BomItem, search_result: JlcpcbSearchResult) -> None:
    """Apply JLCPCB search results to a BomItem in-place."""
    clear_jlcpcb_live_data(item)
    item.source = search_result.data_source or "JLCPCB"
    item.jlcpcb_source = item.source
    item.jlcpcb_error = ""

    if search_result.availability == AvailabilityState.PREORDER:
        item.jlcpcb_part_number = search_result.lcsc_code
        item.matched_mpn = search_result.matched_mpn
        item.exact_match = search_result.exact_match
        item.available_stock_qty = search_result.stock
        item.jlcpcb_category = search_result.category
        item.jlcpcb_package = search_result.package
        item.jlcpcb_availability = AvailabilityState.PREORDER.value
        item.jlcpcb_status = "preorder"
        item.notes = "JLCPCB supplier result is pre-order and is not purchasable."
        item.refresh_status()
        if search_result.preorder_only and item.digikey_status != "found":
            item.status = "Pre-order only"
        return

    if search_result.error:
        item.jlcpcb_part_number = ""
        item.jlcpcb_status = "error"
        item.jlcpcb_error = search_result.error
        if search_result.error == "Missing MPN":
            item.status = "Missing MPN"
        else:
            item.status = f"JLCPCB API error: {search_result.error}"
            item.notes = search_result.error
        item.refresh_status()
        return

    if not search_result.found and not search_result.exact_match:
        item.jlcpcb_part_number = ""
        if search_result.match_count > 0:
            item.jlcpcb_status = "mismatch"
            item.status = "No exact JLCPCB match"
            item.matched_mpn = ""
        else:
            item.jlcpcb_status = "not_found"
            item.status = "JLCPCB not found"
        item.refresh_status()
        return

    item.matched_mpn = search_result.matched_mpn
    item.exact_match = True
    item.available_stock_qty = search_result.stock
    availability = search_result.availability
    if availability == AvailabilityState.UNKNOWN:
        availability = normalize_availability(None, search_result.stock)
    item.jlcpcb_availability = availability.value
    item.jlcpcb_category = search_result.category
    item.jlcpcb_package = search_result.package
    item.is_basic = search_result.is_basic
    item.is_preferred = search_result.is_preferred
    item.unit_price = search_result.unit_price
    normalized_breaks, price_warnings = normalize_jlcpcb_price_breaks(search_result.price_breaks_raw)
    item.jlcpcb_price_breaks_raw = json.dumps(normalized_breaks) if normalized_breaks else ""
    item.jlcpcb_min_order_quantity = search_result.minimum_order_quantity
    item.jlcpcb_order_multiple = search_result.order_multiple
    item.jlcpcb_currency = search_result.currency
    if price_warnings:
        search_result.warnings.extend(price_warnings)

    item.jlcpcb_part_number = search_result.lcsc_code
    if search_result.warnings:
        item.jlcpcb_status = "warning"
        warning_text = "; ".join(search_result.warnings)
        item.notes = warning_text
        item.status = f"Warning [{item.source}]: {warning_text}"
    else:
        item.jlcpcb_status = "found"
    item.refresh_status()


from PyQt6.QtCore import QThread, pyqtSignal, QMutex
from core.digikey_searcher import (
    DigiKeySearcher,
    clear_digikey_live_data,
    enrich_bom_item_digikey,
)

class SearchWorker(QThread):
    """Background worker that searches JLCPCB for all BOM items."""

    MAX_PARALLEL_WORKERS = 8

    progress = pyqtSignal(int, int, str, str)
    item_result = pyqtSignal(int, object)
    finished_all = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, items: list[BomItem], app_id: str, access_key: str, secret_key: str, parent=None, force_refresh: bool = False, pricing_mode: str = "unit", _allow_parallel: bool = True, _copy_items: bool = True, _credential_start_index: int = 0, _observation_run_id: Optional[str] = None, _record_history: bool = True):
        super().__init__(parent)
        # Refresh runs against a staging snapshot. The UI commits it only from
        # ``finished_all``; an exception therefore leaves the last successful
        # item list untouched. Initial processing keeps its existing behavior.
        self.items = copy.deepcopy(items) if force_refresh and _copy_items else items
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.force_refresh = force_refresh
        self.pricing_mode = pricing_mode
        self._cancelled = False
        self._mutex = QMutex()
        self._allow_parallel = _allow_parallel
        self._credential_start_index = _credential_start_index
        self._observation_run_id = _observation_run_id or uuid.uuid4().hex
        self._record_history = _record_history
        self._child_workers = []
        self._item_callback = None
        self._error_callback = None
        self.db_manager = DatabaseManager()

    def cancel(self):
        self._mutex.lock()
        self._cancelled = True
        children = list(self._child_workers)
        self._mutex.unlock()
        for child in children:
            child.cancel()

    def _is_cancelled(self) -> bool:
        self._mutex.lock()
        val = self._cancelled
        self._mutex.unlock()
        return val

    def run(self):
        if self._allow_parallel and len(self.items) > 1:
            self._run_parallel()
        else:
            self._run_sequential()

    def _run_parallel(self):
        """Process independent BOM components concurrently, preserving order."""
        total = len(self.items)
        worker_count = min(self.MAX_PARALLEL_WORKERS, total)
        indexed_chunks = [[] for _ in range(worker_count)]
        for index, item in enumerate(self.items):
            indexed_chunks[index % worker_count].append((index, item))

        progress_lock = threading.Lock()
        completed_count = 0
        errors = []

        def build_child(worker_index, indexed_items):
            child = SearchWorker(
                [item for _, item in indexed_items],
                self.app_id,
                self.access_key,
                self.secret_key,
                force_refresh=self.force_refresh,
                pricing_mode=self.pricing_mode,
                _allow_parallel=False,
                _copy_items=False,
                _credential_start_index=worker_index,
                _observation_run_id=self._observation_run_id,
                _record_history=False,
            )

            def forward_item(local_index, item):
                nonlocal completed_count
                global_index = indexed_items[local_index][0]
                with progress_lock:
                    completed_count += 1
                    current = completed_count
                self._publish_item(global_index, item)
                self.progress.emit(
                    current,
                    total,
                    item.mpn or "(empty)",
                    item.status,
                )

            # Child QThread objects are executed by the Python pool rather than
            # started with QThread.start(), so queued Qt child signals have no
            # event loop. Use direct internal callbacks, then emit only from
            # this parent worker.
            child._item_callback = forward_item
            child._error_callback = errors.append
            return child

        children = [
            build_child(worker_index, chunk)
            for worker_index, chunk in enumerate(indexed_chunks)
            if chunk
        ]
        self._mutex.lock()
        self._child_workers = children
        already_cancelled = self._cancelled
        self._mutex.unlock()
        if already_cancelled:
            for child in children:
                child.cancel()

        try:
            with ThreadPoolExecutor(
                max_workers=len(children),
                thread_name_prefix="bom-search",
            ) as executor:
                futures = [executor.submit(child.run) for child in children]
                for future in as_completed(futures):
                    future.result()

            if errors:
                self.error.emit("; ".join(dict.fromkeys(errors)))
                return

            self._record_supplier_history_batch(self.items)

            # During the test phase cancellation intentionally retains a
            # partial export, matching the sequential worker behavior.
            self.finished_all.emit(self.items)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self._mutex.lock()
            self._child_workers = []
            self._mutex.unlock()

    def _publish_item(self, index, item):
        if self._item_callback is not None:
            self._item_callback(index, item)
        else:
            self.item_result.emit(index, item)

    def _publish_error(self, message):
        if self._error_callback is not None:
            self._error_callback(message)
        else:
            self.error.emit(message)

    @staticmethod
    def _observation_changed(previous, part_number, stock, unit_price, observation_type="FOUND") -> bool:
        if previous.get("observation_type", "FOUND") != observation_type:
            return True
        if (previous.get("part_number") or "") != (part_number or ""):
            return True
        if previous.get("stock") != stock:
            return True
        previous_price = previous.get("unit_price")
        if previous_price is None or unit_price is None:
            return previous_price != unit_price
        return abs(float(previous_price) - float(unit_price)) > 1e-12

    @staticmethod
    def _supplier_observations(item: BomItem):
        observations = []
        for supplier, status, error, part_number, stock, price, source in (
            ("JLCPCB", item.jlcpcb_status, item.jlcpcb_error, item.jlcpcb_part_number,
             item.available_stock_qty, item.unit_price, item.jlcpcb_source or item.source or "JLCPCB"),
            ("DIGIKEY", item.digikey_status, item.digikey_error, item.digikey_part_number,
             item.digikey_stock_qty, item.digikey_unit_price, item.digikey_source or "DIGIKEY_API"),
        ):
            if status == "not_searched":
                if part_number or stock is not None or price is not None:
                    status = "found"
                else:
                    continue
            observation_type = {
                "found": "FOUND", "warning": "FOUND", "not_found": "NOT_FOUND",
                "error": "API_ERROR", "preorder": "PREORDER",
            }.get(status)
            if observation_type:
                observations.append((supplier, part_number, stock, price, source, observation_type, error))
        return observations

    def _record_supplier_history(self, item: BomItem) -> None:
        recorder = cast(
            Optional[Callable[..., Optional[Dict[str, Any]]]],
            getattr(self.db_manager, "record_supplier_observation", None),
        )
        if not callable(recorder):
            return
        observations = self._supplier_observations(item)
        for supplier, part_number, stock, unit_price, data_source, observation_type, error in observations:
            previous = recorder(
                self._observation_run_id,
                item.mpn,
                supplier,
                part_number,
                stock,
                unit_price,
                data_source,
                observation_type,
                error,
            )
            if (
                self.force_refresh
                and previous is not None
                and self._observation_changed(
                    previous, part_number, stock, unit_price, observation_type
                )
            ):
                item.supplier_changes.append(
                    {
                        "mpn": item.mpn,
                        "supplier": supplier,
                        "previous_part_number": previous.get("part_number", ""),
                        "current_part_number": part_number,
                        "previous_observation_type": previous.get("observation_type", "FOUND"),
                        "current_observation_type": observation_type,
                        "previous_stock": previous.get("stock"),
                        "current_stock": stock,
                        "stock_change": (
                            stock - previous["stock"]
                            if stock is not None and previous.get("stock") is not None
                            else None
                        ),
                        "previous_unit_price": previous.get("unit_price"),
                        "current_unit_price": unit_price,
                        "unit_price_change": (
                            unit_price - previous["unit_price"]
                            if unit_price is not None
                            and previous.get("unit_price") is not None
                            else None
                        ),
                        "previous_observed_at": previous.get("observed_at"),
                        "current_observed_at": time.time(),
                    }
                )

    def _record_supplier_history_batch(self, items: list[BomItem]) -> None:
        recorder = cast(
            Optional[
                Callable[[list], list[Optional[Dict[str, Any]]]]
            ],
            getattr(self.db_manager, "record_supplier_observations", None),
        )
        if not callable(recorder):
            for item in items:
                self._record_supplier_history(item)
            return

        pending = []
        records = []
        for item in items:
            observations = self._supplier_observations(item)
            for supplier, part_number, stock, unit_price, data_source, observation_type, error in observations:
                pending.append((item, supplier, part_number, stock, unit_price, observation_type))
                records.append((
                    self._observation_run_id,
                    item.mpn,
                    supplier,
                    part_number,
                    stock,
                    unit_price,
                    data_source,
                    observation_type,
                    error,
                ))

        previous_values = recorder(records)
        if not self.force_refresh:
            return
        for current, previous in zip(pending, previous_values):
            if previous is None:
                continue
            item, supplier, part_number, stock, unit_price, observation_type = current
            if not self._observation_changed(previous, part_number, stock, unit_price, observation_type):
                continue
            item.supplier_changes.append({
                "mpn": item.mpn,
                "supplier": supplier,
                "previous_part_number": previous.get("part_number", ""),
                "current_part_number": part_number,
                "previous_observation_type": previous.get("observation_type", "FOUND"),
                "current_observation_type": observation_type,
                "previous_stock": previous.get("stock"),
                "current_stock": stock,
                "stock_change": (
                    stock - previous["stock"]
                    if stock is not None and previous.get("stock") is not None
                    else None
                ),
                "previous_unit_price": previous.get("unit_price"),
                "current_unit_price": unit_price,
                "unit_price_change": (
                    unit_price - previous["unit_price"]
                    if unit_price is not None and previous.get("unit_price") is not None
                    else None
                ),
                "previous_observed_at": previous.get("observed_at"),
                "current_observed_at": time.time(),
            })

    def _run_sequential(self):
        searcher = JlcpcbSearcher(self.app_id, self.access_key, self.secret_key, self.db_manager)
        dk_searcher = (
            DigiKeySearcher()
            if self._credential_start_index == 0
            else DigiKeySearcher(
                credential_start_index=self._credential_start_index
            )
        )
        total = len(self.items)

        try:
            for idx, item in enumerate(self.items):
                if self._is_cancelled():
                    break

                mpn_display = item.mpn if item.mpn else "(empty)"
                self.progress.emit(idx + 1, total, mpn_display, "Searching JLCPCB...")

                # Refresh live supplier observations from a clean state. Part
                # numbers approved by the user are restored after the lookup,
                # but stale stock/prices must never survive a failed refresh.
                clear_jlcpcb_live_data(item)
                clear_digikey_live_data(item)
                item.supplier_changes = []
                item.skip_jlcpcb = False

                # ``pricing_quantity`` already includes the aggregated BOM
                # requirement and the Process BOM production multiplier.
                item.required_stock = compute_required_stock(item.pricing_quantity)
                if item.required_stock == 0:
                    item.status = "Invalid quantity"
                    self._publish_item(idx, item)
                    self.progress.emit(idx + 1, total, mpn_display, item.status)
                    continue

                lcsc_pending = False
                digikey_pending = False
                internal_code = item.comment.strip() if item.comment else ""
                mapping = None
                
                # Default suggestion values
                suggested_mpn = item.mpn or ""
                suggested_lcsc = ""
                suggested_digikey = ""
                approved_lcsc_code = ""
                approved_digikey_code = ""
                lcsc_is_approved = False
                digikey_is_approved = False
                supplier_errors = []
                
                if internal_code:
                    mapping = self.db_manager.get_internal_mapping(internal_code)
                    if mapping:
                        lcsc_is_approved = bool(
                            mapping.get("lcsc_approved", mapping.get("approved", 0))
                        )
                        digikey_is_approved = bool(
                            mapping.get("digikey_approved", mapping.get("approved", 0))
                        )
                        lcsc_pending = bool(mapping.get("lcsc_pending_change", 0))
                        digikey_pending = bool(mapping.get("digikey_pending_change", 0))

                        if lcsc_is_approved:
                            approved_lcsc_code = (mapping.get("lcsc_code", "") or "").strip()
                            # An approved empty value is intentional. Do not
                            # rediscover and silently replace it during Process BOM.
                            item.skip_jlcpcb = True
                            item.jlcpcb_part_number = approved_lcsc_code
                            if approved_lcsc_code:
                                result = searcher.search_lcsc(
                                    approved_lcsc_code,
                                    item.mpn,
                                    item.required_stock,
                                    refresh=self.force_refresh,
                                )
                                enrich_bom_item(item, result)
                                item.jlcpcb_part_number = approved_lcsc_code
                                if result.availability == AvailabilityState.PREORDER:
                                    if lcsc_pending:
                                        self.db_manager.invalidate_lcsc_preorder_candidate(
                                            internal_code,
                                            mapping.get("last_found_lcsc", "") or approved_lcsc_code,
                                        )
                                        lcsc_pending = False
                                    alternative = searcher.search_mpn(
                                        item.mpn,
                                        item.required_stock,
                                        refresh=self.force_refresh,
                                        excluded_lcsc_codes={approved_lcsc_code},
                                    )
                                    if alternative.is_purchasable(item.required_stock):
                                        suggested_lcsc = alternative.lcsc_code
                                        lcsc_pending = suggested_lcsc != approved_lcsc_code
                        elif not lcsc_pending:
                            lcsc_pending = True

                        if digikey_is_approved:
                            approved_digikey_code = (
                                mapping.get("digikey_code", "") or ""
                            ).strip()
                            item.digikey_part_number = approved_digikey_code
                        elif not digikey_pending:
                            digikey_pending = True
                    else:
                        lcsc_pending = True
                        digikey_pending = True
                
                # NOTE: If there is no internal_code, we just proceed normally and do NOT skip JLCPCB.

                if not item.skip_jlcpcb:
                    result = searcher.search_mpn(item.mpn, item.required_stock, refresh=self.force_refresh)
                    if result:
                        suggested_mpn = result.matched_mpn or item.mpn
                        if result.is_purchasable(item.required_stock):
                            suggested_lcsc = result.lcsc_code or ""
                        elif lcsc_pending and not lcsc_is_approved:
                            if result.availability == AvailabilityState.PREORDER and internal_code:
                                self.db_manager.invalidate_lcsc_preorder_candidate(
                                    internal_code,
                                    result.lcsc_code or (mapping.get("last_found_lcsc", "") if mapping else ""),
                                )
                            lcsc_pending = False
                        if result.error:
                            supplier_errors.append(f"JLCPCB: {result.error}")
                    # A pending supplier collects a suggestion without replacing
                    # that supplier's approved value. The other supplier remains usable.
                    if not lcsc_pending or lcsc_is_approved:
                        enrich_bom_item(item, result)

                digikey_approved_empty = bool(
                    mapping
                    and mapping.get("digikey_approved", mapping.get("approved", 0))
                    and not approved_digikey_code
                )
                if (
                    dk_searcher.is_configured
                    and (not digikey_pending or digikey_is_approved)
                    and not digikey_approved_empty
                ):
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
                    if mapping and bool(mapping.get("digikey_approved", mapping.get("approved", 0))):
                        item.digikey_part_number = approved_digikey_code
                elif dk_searcher.is_configured and digikey_pending:
                    # Pending için DigiKey önerisini sadece DB'de hiç kayıt yokken topla
                    if mapping is None or not mapping.get("last_found_digikey"):
                        self.progress.emit(idx + 1, total, mpn_display, "Searching DigiKey suggestion...")
                        try:
                            dk_result = dk_searcher.search_item(item)
                            if dk_result:
                                suggested_digikey = dk_result.digikey_part_number or ""
                                if dk_result.error:
                                    supplier_errors.append(
                                        f"DigiKey: {dk_result.error}"
                                    )
                        except Exception as exc:
                            supplier_errors.append(f"DigiKey: {exc}")

                if lcsc_pending or digikey_pending:
                    # Kullanıcının ApprovalDialog'da manuel girdiği verileri koruyarak
                    # yalnızca pending tedarikçinin otomatik geçmişini güncelle.
                    if mapping:
                        if not lcsc_pending:
                            suggested_lcsc = mapping.get("last_found_lcsc", "") or ""
                        if not digikey_pending:
                            suggested_digikey = mapping.get("last_found_digikey", "") or ""
                    self.db_manager.insert_pending_suggestion(
                        comment_code=internal_code,
                        mpn=suggested_mpn,
                        lcsc_code=suggested_lcsc,
                        digikey_code=suggested_digikey,
                        lcsc_pending_enabled=lcsc_pending,
                        digikey_pending_enabled=digikey_pending,
                    )
                    item.status = "Pending Approval"
                    if supplier_errors:
                        error_text = "; ".join(supplier_errors)
                        item.status += f" — Supplier API error: {error_text}"
                        item.notes = error_text
                    if lcsc_pending and not lcsc_is_approved:
                        item.jlcpcb_part_number = ""
                    if digikey_pending and not digikey_is_approved:
                        item.digikey_part_number = ""

                use_quantity_breaks = self.pricing_mode == "project"
                pricing_quantity = item.required_stock
                if item.jlcpcb_price_breaks_raw:
                    item.unit_price = select_unit_price(
                        item.jlcpcb_price_breaks_raw,
                        pricing_quantity,
                        use_quantity_breaks=use_quantity_breaks,
                    )
                if item.digikey_price_breaks:
                    item.digikey_unit_price = select_digikey_price(
                        item.digikey_price_breaks,
                        pricing_quantity,
                        use_quantity_breaks=use_quantity_breaks,
                    )
                item.jlcpcb_total_price = (
                    pricing_quantity * item.unit_price
                    if item.unit_price is not None else None
                )
                item.digikey_total_price = (
                    pricing_quantity * item.digikey_unit_price
                    if item.digikey_unit_price is not None else None
                )

                if self._record_history:
                    self._record_supplier_history(item)

                self._publish_item(idx, item)
                self.progress.emit(idx + 1, total, mpn_display, item.status)

            # During the test phase, keep exporting the partial results after
            # cancellation so they can be inspected.  Before the final release
            # this should be changed back to suppress successful completion.
            self.finished_all.emit(self.items)

        except Exception as e:
            self._publish_error(str(e))
        finally:
            searcher.close()
            dk_searcher.close()


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
