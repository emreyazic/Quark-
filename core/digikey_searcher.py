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

import requests

from core.mpn_utils import clean_mpn_value, is_exact_mpn_match, select_unit_price


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


class DigiKeySearcher:
    """Searches DigiKey for electronic components.

    Checks for API credentials in environment variables on initialization.
    If credentials are not available, all searches return a 'not configured' result.
    """

    def __init__(self):
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
        self._active_cred_index = 0
        
        self._configured = len(self._credentials) > 0
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

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
            self._access_token = data.get("access_token")
            # Token typically lasts ~1800 seconds; refresh 60s before expiry
            expires_in = data.get("expires_in", 1800)
            self._token_expires_at = time.time() + expires_in - 60
            return self._access_token
        except Exception as e:
            self._access_token = None
            return None

    def search_item(self, item) -> DigiKeySearchResult:
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
                result.error = "Failed to obtain DigiKey API access token"
                return result

            try:
                resp = self.session.post(
                    "https://api.digikey.com/products/v4/search/keyword",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-DIGIKEY-Client-Id": client_id,
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

                # Search for exact MPN match
                for prod in products:
                    candidate_mpn = prod.get("ManufacturerProductNumber", "")
                    if is_exact_mpn_match(mpn_clean, candidate_mpn):
                        result.found = True
                        result.exact_match = True
                        result.matched_mpn = candidate_mpn
                        
                        mfr_obj = prod.get("Manufacturer", {})
                        result.manufacturer = mfr_obj.get("Name", "") if isinstance(mfr_obj, dict) else str(mfr_obj)
                        
                        variations = prod.get("ProductVariations", [])
                        best_var = None
                        if variations:
                            for var in variations:
                                package = var.get("PackageType", {})
                                package_name = package.get("Name", "") if isinstance(package, dict) else str(package)
                                try:
                                    moq = int(var.get("MinimumOrderQuantity", 0))
                                except:
                                    moq = 0
                                if "Cut Tape" in package_name or "CT" in package_name or moq == 1:
                                    best_var = var
                                    break
                            if not best_var:
                                best_var = variations[0]
                                
                        result.digikey_part_number = best_var.get("DigiKeyProductNumber", "") if best_var else ""
                        
                        result.stock = int(prod.get("QuantityAvailable", 0))
                        
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
                        result.price_breaks = sorted(pbs, key=lambda x: x[0])

                        # Get unit price from first price break
                        if result.price_breaks:
                            result.unit_price = result.price_breaks[0][1]
                        break

                # Store candidates
                for prod in products[:5]:
                    mfr_obj = prod.get("Manufacturer", {})
                    variations = prod.get("ProductVariations", [])
                    
                    best_cand_var = None
                    if variations:
                        for var in variations:
                            package = var.get("PackageType", {})
                            package_name = package.get("Name", "") if isinstance(package, dict) else str(package)
                            try:
                                moq = int(var.get("MinimumOrderQuantity", 0))
                            except:
                                moq = 0
                            if "Cut Tape" in package_name or "CT" in package_name or moq == 1:
                                best_cand_var = var
                                break
                        if not best_cand_var:
                            best_cand_var = variations[0]
                    
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
                
    def search_mpn(self, mpn: str) -> DigiKeySearchResult:
        """Search DigiKey directly by MPN string."""
        class _Item:
            def __init__(self, mpn_val):
                self.mpn = mpn_val
                self.value = ""
                self.description = ""
                self.manufacturer = ""
                self.footprint = ""

        return self.search_item(_Item(mpn))

    def close(self):
        """Close the HTTP session."""
        self.session.close()


def enrich_bom_item_digikey(item, search_result: DigiKeySearchResult) -> None:
    """Enrich the BOM item with DigiKey pricing data."""
    if search_result.error:
        if "429" in search_result.error:
            if item.status:
                item.status += " (DigiKey Rate Limited)"
            else:
                item.status = "DigiKey Rate Limited"
        return

    if not search_result.configured:
        return

    if not search_result.found and not search_result.exact_match:
        return

    # Extract price info if available
    if search_result.found or search_result.exact_match:
        item.digikey_unit_price = search_result.unit_price
        item.digikey_stock_qty = search_result.stock
        item.digikey_price_breaks = search_result.price_breaks
        if hasattr(item, 'digikey_part_number') and search_result.digikey_part_number:
            item.digikey_part_number = search_result.digikey_part_number
