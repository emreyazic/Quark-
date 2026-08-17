from core.digikey_searcher import (
    DigiKeySearcher,
    DigiKeySearchResult,
    enrich_bom_item_digikey,
)
from models.bom_item import BomItem
import requests


def test_digikey_api_error_is_supplier_specific():
    item = BomItem(mpn="MPN1", status="JLCPCB not found")
    result = DigiKeySearchResult()
    result.error = "DigiKey API HTTP error: 503 Service Unavailable"

    enrich_bom_item_digikey(item, result)

    assert "DigiKey API error" in item.status
    assert "503 Service Unavailable" in item.status
    assert "503 Service Unavailable" in item.notes
    assert item.digikey_status == "error"
    assert "503 Service Unavailable" in item.digikey_error


def test_digikey_error_does_not_invalidate_valid_jlcpcb_result():
    item = BomItem(
        mpn="MPN1", jlcpcb_part_number="C123", available_stock_qty=100,
        unit_price=0.25, jlcpcb_status="found",
    )
    result = DigiKeySearchResult()
    result.configured = True
    result.error = "temporary API failure"

    enrich_bom_item_digikey(item, result)

    assert (item.jlcpcb_part_number, item.available_stock_qty, item.unit_price) == ("C123", 100, 0.25)
    assert item.digikey_status == "error"
    assert item.is_available
    assert not item.is_not_found
    assert item.status == ""


def test_not_found_is_distinct_from_supplier_api_error():
    item = BomItem(jlcpcb_status="not_found")
    result = DigiKeySearchResult()
    result.configured = True

    enrich_bom_item_digikey(item, result)
    assert item.is_not_found
    assert item.status == "Not Found"

    result.error = "service unavailable"
    enrich_bom_item_digikey(item, result)
    assert not item.is_not_found
    assert "API error" in item.status


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_digikey_uses_live_selected_variation_stock_and_quantity_price(monkeypatch):
    searcher = DigiKeySearcher()
    monkeypatch.setattr(searcher, "_get_access_token", lambda: "token")

    keyword_product = {
        "ManufacturerProductNumber": "MPN1",
        "Manufacturer": {"Name": "MFG"},
        "QuantityAvailable": 9999,
        "Description": {"ProductDescription": "Part"},
        "ProductVariations": [{
            "DigiKeyProductNumber": "DK-MPN1-CT-ND",
            "QuantityAvailableforPackageType": 999,
            "MinimumOrderQuantity": 1,
            "PackageType": {"Name": "Cut Tape (CT)"},
            "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 1.0}],
        }],
    }
    live_product = {
        **keyword_product,
        "ProductVariations": [{
            "DigiKeyProductNumber": "DK-MPN1-CT-ND",
            "QuantityAvailableforPackageType": 123,
            "MinimumOrderQuantity": 1,
            "PackageType": {"Name": "Cut Tape (CT)"},
            "StandardPricing": [
                {"BreakQuantity": 1, "UnitPrice": 0.50},
                {"BreakQuantity": 50, "UnitPrice": 0.30},
            ],
        }],
    }
    monkeypatch.setattr(
        searcher.session,
        "post",
        lambda *args, **kwargs: _Response({"ExactMatches": [keyword_product]}),
    )
    monkeypatch.setattr(
        searcher.session,
        "get",
        lambda *args, **kwargs: _Response({"Products": [live_product]}),
    )

    result = searcher.search_mpn("MPN1", required_stock=60)
    assert result.digikey_part_number == "DK-MPN1-CT-ND"
    assert result.stock == 123
    assert result.unit_price == 0.50
    assert result.data_source == "DIGIKEY_LIVE"
    searcher.close()


def test_digikey_live_failure_is_visible_search_fallback(monkeypatch):
    searcher = DigiKeySearcher()
    monkeypatch.setattr(searcher, "_get_access_token", lambda: "token")
    keyword_product = {
        "ManufacturerProductNumber": "MPN1",
        "Manufacturer": {"Name": "MFG"},
        "Description": {"ProductDescription": "Part"},
        "ProductVariations": [{
            "DigiKeyProductNumber": "DK-MPN1",
            "QuantityAvailableforPackageType": 12,
            "MinimumOrderQuantity": 1,
            "PackageType": {"Name": "Cut Tape (CT)"},
            "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 1.25}],
        }],
    }
    monkeypatch.setattr(
        searcher.session,
        "post",
        lambda *args, **kwargs: _Response({"ExactMatches": [keyword_product]}),
    )
    monkeypatch.setattr(
        searcher.session,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )

    result = searcher.search_mpn("MPN1")
    item = BomItem(mpn="MPN1")
    enrich_bom_item_digikey(item, result)

    assert result.found
    assert result.data_source == "SEARCH_FALLBACK"
    assert result.warnings and "keyword-search data used" in result.warnings[0]
    assert "keyword-search data used" in item.notes
    assert item.digikey_source == "SEARCH_FALLBACK"


def test_digikey_prefers_cut_tape_before_other_moq_one_variations():
    variations = [
        {"DigiKeyProductNumber": "TRAY", "MinimumOrderQuantity": 1, "PackageType": {"Name": "Tray"}},
        {"DigiKeyProductNumber": "CT", "MinimumOrderQuantity": 1, "PackageType": {"Name": "Cut Tape (CT)"}},
    ]

    assert DigiKeySearcher._select_variation(variations)["DigiKeyProductNumber"] == "CT"


def test_digikey_variation_selection_is_quantity_aware_and_deterministic():
    variations = [
        {
            "DigiKeyProductNumber": "DK-Z", "MinimumOrderQuantity": 1,
            "QuantityAvailableforPackageType": 20,
            "PackageType": {"Name": "Cut Tape (CT)"},
            "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.10}],
        },
        {
            "DigiKeyProductNumber": "DK-B", "MinimumOrderQuantity": 10,
            "QuantityAvailableforPackageType": 500,
            "PackageType": {"Name": "Cut Tape (CT)"},
            "StandardPricing": [{"BreakQuantity": 100, "UnitPrice": 0.04}],
        },
        {
            "DigiKeyProductNumber": "DK-A", "MinimumOrderQuantity": 10,
            "QuantityAvailableforPackageType": 500,
            "PackageType": {"Name": "Cut Tape (CT)"},
            "StandardPricing": [{"BreakQuantity": 100, "UnitPrice": 0.04}],
        },
    ]

    selected = {
        DigiKeySearcher._select_variation(order, target_quantity=100)["DigiKeyProductNumber"]
        for order in (variations, list(reversed(variations)))
    }

    assert selected == {"DK-A"}


class _TokenResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.exceptions.HTTPError(
                f"{self.status_code} token error", response=response
            )

    def json(self):
        return self.payload


def test_digikey_token_tries_next_credential_after_retryable_failure(monkeypatch):
    searcher = DigiKeySearcher()
    searcher._credentials = [("first-id", "first-secret"), ("second-id", "second-secret")]
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["data"]["client_id"])
        if len(calls) == 1:
            return _TokenResponse(401)
        return _TokenResponse(200, {"access_token": "good-token", "expires_in": 1800})

    monkeypatch.setattr(searcher.session, "post", post)

    assert searcher._get_access_token() == "good-token"
    assert calls == ["first-id", "second-id"]
    assert searcher._active_cred_index == 1


def test_digikey_token_tries_next_credential_after_temporary_network_failure(monkeypatch):
    searcher = DigiKeySearcher()
    searcher._credentials = [("first-id", "first-secret"), ("second-id", "second-secret")]
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["data"]["client_id"])
        if len(calls) == 1:
            raise requests.exceptions.Timeout("temporary timeout")
        return _TokenResponse(200, {"access_token": "good-token"})

    monkeypatch.setattr(searcher.session, "post", post)

    assert searcher._get_access_token() == "good-token"
    assert calls == ["first-id", "second-id"]


def test_digikey_token_permanent_error_does_not_retry_or_expose_secret(monkeypatch):
    searcher = DigiKeySearcher()
    secret = "must-not-appear"
    searcher._credentials = [("first-id", secret), ("second-id", "other-secret")]
    calls = []
    monkeypatch.setattr(
        searcher.session, "post",
        lambda *args, **kwargs: calls.append(kwargs["data"]["client_id"])
        or _TokenResponse(400),
    )

    result = searcher.search_mpn("MPN1")

    assert calls == ["first-id"]
    assert result.error == "DigiKey token request failed with HTTP 400"
    assert secret not in result.error


def test_digikey_token_reports_all_retryable_credentials_exhausted(monkeypatch):
    searcher = DigiKeySearcher()
    searcher._credentials = [("first-id", "secret-1"), ("second-id", "secret-2")]
    monkeypatch.setattr(
        searcher.session, "post", lambda *args, **kwargs: _TokenResponse(503)
    )

    result = searcher.search_mpn("MPN1")

    assert "all configured credentials" in result.error
    assert "503" in result.error
    assert "secret-1" not in result.error


def test_digikey_duplicate_rows_are_deterministic_and_only_exact_mpn_is_eligible(monkeypatch):
    searcher = DigiKeySearcher()
    monkeypatch.setattr(searcher, "_get_access_token", lambda: "token")

    def product(mpn, part_number, stock):
        return {
            "ManufacturerProductNumber": mpn,
            "Manufacturer": {"Name": "MFG"},
            "QuantityAvailable": stock,
            "ProductVariations": [{
                "DigiKeyProductNumber": part_number,
                "QuantityAvailableforPackageType": stock,
                "MinimumOrderQuantity": 1,
                "PackageType": {"Name": "Cut Tape (CT)"},
                "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 1.0}],
            }],
        }

    exact_low = product("MPN1", "DK-LOW", 10)
    exact_high = product("mpn1", "DK-HIGH", 100)
    prefix_with_more_stock = product("MPN1-EXTRA", "DK-WRONG", 10000)

    orders = [
        [prefix_with_more_stock, exact_low, exact_high],
        [exact_high, exact_low, prefix_with_more_stock],
    ]
    selected_codes = []
    for products in orders:
        monkeypatch.setattr(
            searcher.session,
            "post",
            lambda *args, products=products, **kwargs: _Response(
                {"Products": products}
            ),
        )
        result = searcher.search_mpn("MPN1", include_live_data=False)
        selected_codes.append(result.digikey_part_number)
        assert result.exact_match is True
        assert result.matched_mpn.casefold() == "mpn1"

    assert selected_codes == ["DK-HIGH", "DK-HIGH"]
    searcher.close()


def test_failed_digikey_refresh_clears_stale_supplier_code_and_values():
    item = BomItem(
        mpn="MPN1",
        digikey_part_number="OLD-DK",
        digikey_stock_qty=500,
        digikey_unit_price=2.0,
        digikey_price_breaks=[(1, 2.0)],
    )
    result = DigiKeySearchResult()
    result.configured = True
    result.error = "temporary API failure"

    enrich_bom_item_digikey(item, result)

    assert item.digikey_part_number == ""
    assert item.digikey_stock_qty is None
    assert item.digikey_unit_price is None
    assert item.digikey_price_breaks == []
