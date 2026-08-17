"""DigiKey parts searcher.

DigiKey does not have a free public API like JLCPCB.  This module provides
a search interface that attempts to find components on DigiKey:

- If official API credentials are configured via environment variables
  (DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET), uses the official API.
- Otherwise, returns a structured "not configured" result without crashing.

The module is structured so real DigiKey API integration can be added later.
"""

import os
import time
import json
from typing import Optional
from urllib.parse import quote

import requests

from core.mpn_utils import clean_mpn_value, is_exact_mpn_match, normalize_digikey_price_breaks, select_digikey_price

SOURCE_DIGIKEY_LIVE = "DIGIKEY_LIVE"
SOURCE_SEARCH_FALLBACK = "SEARCH_FALLBACK"
SOURCE_CACHE = "CACHE"


class DigiKeySearchResult:
    """Holds the result of a single DigiKey search."""

    def __init__(self):
        self.found: bool = False
        self.exact_match: bool = False
        self.matched_mpn: str = ""
        self.manufacturer: str = ""
        self.digikey_part_number: str = ""
        self.stock: int = 0
        self.unit_price: Optional[float] = None
        self.description: str = ""
        self.match_count: int = 0
        self.error: Optional[str] = None
        self.candidates: list[dict] = []
        self.price_breaks: list[tuple[int, float]] = []
        self.configured: bool = False  # True if DigiKey credentials are available
        self.data_source: str = ""
        self.warnings: list[str] = []


class DigiKeySearcher:
    """Searches DigiKey for electronic components.

    Checks for API credentials in environment variables on initialization.
    If credentials are not available, all searches return a 'not configured' result.
    """

    def __init__(self, credential_start_index: int = 0):
        # You can add as many DigiKey API credentials as you want to this dictionary.
        # Format: {"CLIENT_ID": "CLIENT_SECRET"}
        api_keys = {
            "0jvVH0jdJXAS3GriH88tr2yH2TVDEKW1L5V4GUEOIx9ee0qD": "rqWPc0c5aiioUIWNeh2wwaPAOYKV90sW3ncFrSbAfurhCnyhiHfKqLM0G0bIXssf",
            "EEwC7DCEbg69k8hbWvcEaY8c08VdcJB4oPcmkQPA8gNiNyOI":"GoaPQIvy8Kvx5H35y5toeZiqSHkSoA4DPiSj8Jx9S1J3HeTg5CLNh9dMurHbKPTk",
            "IhlhvvhqGJ6tVsnKp8gDJU3h1JZ2MAXXyA5kovVL5egXwkgF":"HzyZ2e8VY3RK7K1qeJMGEjeEDxQahTdRu1VyCAN8jA3H4A3VGFsI1lSyiK4Sar7j",
            "A45Zq2AakXtIHa5wItLKxGpAbHWpDj7ovHUCB4xXOAUIDDVV": "rAJG8WSB0JKwCAyPhhw0Xpn9nSqZYjlCTaeUUGD4JqO87OQX8GqAilJ9eoYafJIG",
            # "YOUR_CLIENT_ID_2": "YOUR_CLIENT_SECRET_2",
            # "YOUR_CLIENT_ID_3": "YOUR_CLIENT_SECRET_3",
        }
        
        # Also support environment variables if set
        id_env = os.environ.get("DIGIKEY_CLIENT_ID", "")
        secret_env = os.environ.get("DIGIKEY_CLIENT_SECRET", "")
        if id_env and secret_env:
            ids = [i.strip() for i in id_env.split(",") if i.strip()]
            secrets = [s.strip() for s in secret_env.split(",") if s.strip()]
            for cid, csec in zip(ids, secrets):
                api_keys[cid] = csec
        
        self._credentials = list(api_keys.items())
        if self._credentials:
            offset = credential_start_index % len(self._credentials)
            self._credentials = self._credentials[offset:] + self._credentials[:offset]
        self._active_cred_index = 0
        
        self._configured = len(self._credentials) > 0
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._token_error: Optional[str] = None
        self._last_live_warning: Optional[str] = None

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "BOM-Enrichment-Tool/2.0",
            "Accept": "application/json",
        })

    @property
    def is_configured(self) -> bool:
        """Check if DigiKey API credentials are available."""
        return self._configured

    def _get_access_token(self) -> Optional[str]:
        """Obtain or refresh OAuth2 access token from DigiKey API."""
        if not self._configured or self._active_cred_index >= len(self._credentials):
            return None

        # Check if existing token is still valid
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        failures = []
        while self._active_cred_index < len(self._credentials):
            client_id, client_secret = self._credentials[self._active_cred_index]
            try:
                resp = self.session.post(
                    "https://api.digikey.com/v1/oauth2/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "client_credentials",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                token = data.get("access_token") if isinstance(data, dict) else None
                if not token:
                    self._token_error = "DigiKey token response did not contain an access token"
                    return None
                self._access_token = token
                expires_in = data.get("expires_in", 1800)
                self._token_expires_at = time.time() + expires_in - 60
                self._token_error = None
                return token
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status not in (401, 429) and not (status is not None and status >= 500):
                    self._token_error = f"DigiKey token request failed with HTTP {status or 'error'}"
                    return None
                failures.append(f"HTTP {status}")
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                failures.append(type(exc).__name__)
            except (ValueError, TypeError, KeyError) as exc:
                self._token_error = f"Invalid DigiKey token response: {type(exc).__name__}"
                return None
            except requests.exceptions.RequestException as exc:
                self._token_error = f"DigiKey token request failed: {type(exc).__name__}"
                return None

            self._access_token = None
            self._active_cred_index += 1

        detail = ", ".join(failures) if failures else "no usable credential"
        self._token_error = (
            "DigiKey token request failed for all configured credentials "
            f"({detail})"
        )
        return None

    @staticmethod
    def _select_variation(
        variations: list[dict],
        target_quantity: int = 1,
        preferred_package_type: str = "Cut Tape",
    ) -> Optional[dict]:
        """Select a variation deterministically for the requested quantity."""
        def as_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def rank(variation: dict):
            package = variation.get("PackageType", {})
            package_name = package.get("Name", "") if isinstance(package, dict) else str(package)
            stock_value = variation.get("QuantityAvailableforPackageType")
            if stock_value is None:
                stock_value = variation.get("QuantityAvailable")
            stock = as_int(stock_value)
            moq = as_int(variation.get("MinimumOrderQuantity"), default=2**31 - 1)
            price_breaks = []
            for price in variation.get("StandardPricing", []):
                try:
                    price_breaks.append(
                        (int(price.get("BreakQuantity", 0)), float(price.get("UnitPrice", 0.0)))
                    )
                except (TypeError, ValueError):
                    continue
            relevant_price = select_digikey_price(
                price_breaks, target_quantity, use_quantity_breaks=True
            )
            return (
                0 if preferred_package_type.casefold() in package_name.casefold() else 1,
                0 if stock >= target_quantity else 1,
                0 if moq <= target_quantity else 1,
                moq,
                float("inf") if relevant_price is None else relevant_price,
                str(variation.get("DigiKeyProductNumber", "")).casefold(),
            )

        return min(variations, key=rank) if variations else None

    @classmethod
    def _select_exact_product(cls, products: list[dict], target_quantity: int = 1) -> Optional[dict]:
        """Choose deterministically among duplicate representations of one exact MPN."""
        if not products:
            return None

        def rank(product: dict):
            variation = cls._select_variation(
                product.get("ProductVariations", []), target_quantity
            )
            variation_stock = (
                variation.get("QuantityAvailableforPackageType")
                if variation else None
            )
            if variation_stock is None and variation:
                variation_stock = variation.get("QuantityAvailable")
            try:
                stock = int(
                    variation_stock
                    if variation_stock is not None
                    else product.get("QuantityAvailable", 0) or 0
                )
            except (TypeError, ValueError):
                stock = 0
            product_number = (
                variation.get("DigiKeyProductNumber", "") if variation else ""
            )
            # Prefer a purchasable selected variation, then higher availability;
            # the catalogue number makes equal-stock selection stable.
            return (0 if variation else 1, -stock, product_number.casefold())

        return min(products, key=rank)

    def _load_live_product(self, product_number: str, client_id: str, token: str) -> Optional[dict]:
        """Load real-time availability/pricing for the selected DigiKey code."""
        if not product_number:
            return None
        self._last_live_warning = None
        try:
            response = self.session.get(
                f"https://api.digikey.com/products/v4/search/{quote(product_number, safe='')}/productdetails",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-DIGIKEY-Client-Id": client_id,
                    "X-DIGIKEY-Locale-Site": "US",
                    "X-DIGIKEY-Locale-Language": "en",
                    "X-DIGIKEY-Locale-Currency": "USD",
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            products = data.get("Products", []) if isinstance(data, dict) else []
            if not products and isinstance(data, dict):
                product = data.get("Product")
                products = [product] if isinstance(product, dict) else []
            if products:
                return products[0]
            self._last_live_warning = "DigiKey live detail returned no product; keyword-search data used."
            return None
        except Exception as exc:
            # Keyword data is still a usable fallback if the live endpoint is
            # temporarily unavailable or rate limited.
            self._last_live_warning = (
                f"DigiKey live detail failed ({type(exc).__name__}); keyword-search data used."
            )
            return None

    def search_item(self, item, include_live_data: bool = True) -> DigiKeySearchResult:
        """Search for a component using item fields.

        Args:
            item: BomItem to search for.

        Returns:
            DigiKeySearchResult with match details.
        """
        result = DigiKeySearchResult()
        result.configured = self._configured

        # Build query
        query = ""
        mpn_clean = ""
        if hasattr(item, 'digikey_part_number') and item.digikey_part_number and item.digikey_part_number.strip():
            mpn_clean = clean_mpn_value(item.digikey_part_number)
            query = mpn_clean
        elif item.mpn and item.mpn.strip() and not item.mpn.upper().startswith("RES"):
            mpn_clean = clean_mpn_value(item.mpn)
            query = mpn_clean
        else:
            # Fallback for RES or missing MPN
            parts = [item.value, item.description, item.manufacturer, item.footprint]
            query = " ".join(p.strip() for p in parts if p and p.strip())
            
        if not query:
            result.error = "Missing search criteria"
            return result

        target_quantity = max(int(getattr(item, "required_stock", 1) or 1), 1)

        if not self._configured:
            result.error = (
                "DigiKey API not configured. Set DIGIKEY_CLIENT_ID and "
                "DIGIKEY_CLIENT_SECRET environment variables."
            )
            return result

        # Attempt official API search with retries for multiple credentials
        for attempt in range(len(self._credentials)):
            if self._active_cred_index >= len(self._credentials):
                result.error = "DigiKey API HTTP error: 429 Client Error (All credentials exhausted)"
                return result

            client_id, _ = self._credentials[self._active_cred_index]
            token = self._get_access_token()
            if not token:
                result.error = self._token_error or "Failed to obtain DigiKey API access token"
                return result

            try:
                resp = self.session.post(
                    "https://api.digikey.com/products/v4/search/keyword",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-DIGIKEY-Client-Id": client_id,
                        "X-DIGIKEY-Locale-Site": "US",
                        "X-DIGIKEY-Locale-Language": "en",
                        "X-DIGIKEY-Locale-Currency": "USD",
                        "Content-Type": "application/json",
                    },
                    json={
                        "Keywords": query,
                        "RecordCount": 10,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()

                exact_matches = data.get("ExactMatches", [])
                products = exact_matches if exact_matches else data.get("Products", [])
                result.match_count = len(products)

                if not products:
                    result.found = False
                    return result

                # Only exact MPN matches are eligible. Multiple API rows with
                # that same exact MPN are duplicate representations of one
                # product, not an ambiguity or fuzzy-match opportunity.
                exact_products = [
                    product
                    for product in products
                    if is_exact_mpn_match(
                        mpn_clean, product.get("ManufacturerProductNumber", "")
                    )
                ]
                prod = self._select_exact_product(exact_products, target_quantity)
                if prod:
                    result.found = True
                    result.exact_match = True
                    result.matched_mpn = prod.get("ManufacturerProductNumber", "")

                    mfr_obj = prod.get("Manufacturer", {})
                    result.manufacturer = mfr_obj.get("Name", "") if isinstance(mfr_obj, dict) else str(mfr_obj)

                    variations = prod.get("ProductVariations", [])
                    best_var = self._select_variation(variations, target_quantity)
                    result.digikey_part_number = best_var.get("DigiKeyProductNumber", "") if best_var else ""
                    result.data_source = SOURCE_SEARCH_FALLBACK

                    if include_live_data and result.digikey_part_number:
                        live_product = self._load_live_product(result.digikey_part_number, client_id, token)
                        if live_product:
                            result.data_source = SOURCE_DIGIKEY_LIVE
                            prod = live_product
                            variations = prod.get("ProductVariations", [])
                            best_var = self._select_variation(
                                variations, target_quantity
                            )
                            if best_var:
                                result.digikey_part_number = best_var.get("DigiKeyProductNumber", "") or result.digikey_part_number
                        else:
                            result.warnings.append(
                                self._last_live_warning
                                or "DigiKey live detail unavailable; keyword-search data used."
                            )
                    if (
                        include_live_data
                        and result.digikey_part_number
                        and result.data_source == SOURCE_SEARCH_FALLBACK
                        and not result.warnings
                    ):
                        result.warnings.append(
                            "DigiKey live detail unavailable; keyword-search data used."
                        )

                    variation_stock = best_var.get("QuantityAvailableforPackageType") if best_var else None
                    if variation_stock is None and best_var:
                        variation_stock = best_var.get("QuantityAvailable")
                    result.stock = int(
                        variation_stock
                        if variation_stock is not None
                        else prod.get("QuantityAvailable", 0) or 0
                    )

                    desc_obj = prod.get("Description", {})
                    result.description = desc_obj.get("ProductDescription", "")

                    std_pricing = best_var.get("StandardPricing", []) if best_var else []

                    pbs = []
                    for pb in std_pricing:
                        try:
                            bq = int(pb.get("BreakQuantity", 0))
                            up = float(pb.get("UnitPrice", 0.0))
                            pbs.append((bq, up))
                        except (ValueError, TypeError):
                            pass
                    result.price_breaks, price_warnings = normalize_digikey_price_breaks(pbs)
                    result.warnings.extend(price_warnings)

                    if (
                        include_live_data
                        and result.data_source == SOURCE_SEARCH_FALLBACK
                        and not any("keyword-search data used" in warning for warning in result.warnings)
                    ):
                        result.warnings.append(
                            self._last_live_warning
                            or "DigiKey live detail unavailable; keyword-search data used."
                        )

                    result.unit_price = select_digikey_price(result.price_breaks, target_quantity)

                # Store candidates
                for prod in products[:5]:
                    mfr_obj = prod.get("Manufacturer", {})
                    variations = prod.get("ProductVariations", [])
                    
                    best_cand_var = self._select_variation(variations, target_quantity)
                    
                    result.candidates.append({
                        "mpn": prod.get("ManufacturerProductNumber", ""),
                        "manufacturer": mfr_obj.get("Name", "") if isinstance(mfr_obj, dict) else str(mfr_obj),
                        "digikey_pn": best_cand_var.get("DigiKeyProductNumber", "") if best_cand_var else "",
                        "stock": int(prod.get("QuantityAvailable", 0)),
                    })

                return result

            except requests.exceptions.Timeout:
                result.error = "DigiKey API request timed out"
                return result
            except requests.exceptions.ConnectionError:
                result.error = "DigiKey API connection error"
                return result
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    # Rate limit hit, switch to next credential if available
                    if self._active_cred_index < len(self._credentials) - 1:
                        self._active_cred_index += 1
                        self._access_token = None
                        continue
                    else:
                        result.error = f"DigiKey API HTTP error: {e}"
                        return result
                elif e.response is not None and e.response.status_code == 403:
                    result.error = "DigiKey blocked the request (403 Forbidden). DigiKey actively blocks automated searches without an API key using Cloudflare. Please configure API credentials to search DigiKey."
                    return result
                else:
                    result.error = f"DigiKey API HTTP error: {e}"
                    return result
            except Exception as e:
                result.error = f"DigiKey API error: {e}"
                return result
        
        return result
                
    def search_mpn(self, mpn: str, required_stock: int = 1, include_live_data: bool = True) -> DigiKeySearchResult:
        """Search DigiKey directly by MPN string."""
        class _Item:
            def __init__(self, mpn_val, required_stock_val):
                self.mpn = mpn_val
                self.required_stock = required_stock_val
                self.value = ""
                self.description = ""
                self.manufacturer = ""
                self.footprint = ""

        return self.search_item(_Item(mpn, required_stock), include_live_data=include_live_data)

    def close(self):
        """Close the HTTP session."""
        self.session.close()


def clear_digikey_live_data(item) -> None:
    """Clear only refreshable DigiKey observations, preserving approved codes."""
    item.digikey_stock_qty = None
    item.digikey_unit_price = None
    item.digikey_price_breaks = []
    item.digikey_total_price = None


def enrich_bom_item_digikey(item, search_result: DigiKeySearchResult) -> None:
    """Enrich the BOM item with DigiKey pricing data."""
    clear_digikey_live_data(item)
    item.digikey_source = search_result.data_source or "DigiKey"
    item.digikey_error = ""
    if search_result.error:
        item.digikey_part_number = ""
        item.digikey_status = "error"
        item.digikey_error = search_result.error
        existing_notes = str(getattr(item, "notes", "") or "")
        item.notes = f"{existing_notes}; {search_result.error}".strip("; ")
        item.refresh_status()
        return

    if not search_result.configured:
        item.digikey_part_number = ""
        item.digikey_status = "not_searched"
        item.refresh_status()
        return

    if not search_result.found and not search_result.exact_match:
        item.digikey_part_number = ""
        item.digikey_status = "not_found"
        item.refresh_status()
        return

    # Extract price info if available
    if search_result.found or search_result.exact_match:
        item.digikey_status = "found"
        item.digikey_unit_price = search_result.unit_price
        item.digikey_stock_qty = search_result.stock
        item.digikey_price_breaks = search_result.price_breaks
        if hasattr(item, 'digikey_part_number') and search_result.digikey_part_number:
            item.digikey_part_number = search_result.digikey_part_number
        if search_result.warnings:
            warning_text = "; ".join(search_result.warnings)
            existing_notes = str(getattr(item, "notes", "") or "")
            item.notes = f"{existing_notes}; {warning_text}".strip("; ")
        item.refresh_status()
