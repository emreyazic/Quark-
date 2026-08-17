import json
import time

import core.jlcpcb_searcher as jlcpcb_searcher_module
from core.digikey_searcher import DigiKeySearchResult
from core.jlcpcb_searcher import JlcpcbSearcher, JlcpcbSearchResult, enrich_bom_item
from models.bom_item import BomItem


def test_jlcpcb_error_does_not_invalidate_valid_digikey_result():
    item = BomItem(
        mpn="MPN1", digikey_part_number="DK-1", digikey_stock_qty=50,
        digikey_unit_price=1.5, digikey_status="found",
    )
    result = JlcpcbSearchResult()
    result.error = "service unavailable"

    enrich_bom_item(item, result)

    assert (item.digikey_part_number, item.digikey_stock_qty, item.digikey_unit_price) == ("DK-1", 50, 1.5)
    assert item.jlcpcb_status == "error"
    assert item.is_available
    assert item.status == ""


def test_mpn_lookup_service_failure_is_not_reported_as_not_found(monkeypatch):
    class _FailingSession:
        def post(self, *args, **kwargs):
            raise TimeoutError("official timeout")

        def get(self, *args, **kwargs):
            raise ConnectionError("community offline")

        def close(self):
            pass

    searcher = JlcpcbSearcher("app", "access", "secret")
    searcher.session = _FailingSession()
    monkeypatch.setattr(jlcpcb_searcher_module.time, "sleep", lambda *_: None)

    result = searcher.search_mpn("MPN-FAIL")
    item = BomItem(mpn="MPN-FAIL")
    enrich_bom_item(item, result)

    assert result.error is not None
    assert "official timeout" in result.error
    assert "community offline" in result.error
    assert item.status.startswith("JLCPCB API error:")
    assert item.status != "JLCPCB not found"


def test_main_bom_search_runs_items_in_parallel_and_preserves_order(monkeypatch):
    class _JlcpcbSearcher:
        def __init__(self, *args, **kwargs):
            pass

        def search_mpn(self, mpn, *args, **kwargs):
            time.sleep(0.04)
            result = JlcpcbSearchResult()
            result.found = True
            result.exact_match = True
            result.matched_mpn = mpn
            result.lcsc_code = f"C-{mpn}"
            result.stock = 100
            result.unit_price = 1.0
            return result

        def close(self):
            pass

    class _DigiKeySearcher:
        is_configured = False

        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(jlcpcb_searcher_module, "DatabaseManager", lambda: object())
    monkeypatch.setattr(jlcpcb_searcher_module, "JlcpcbSearcher", _JlcpcbSearcher)
    monkeypatch.setattr(jlcpcb_searcher_module, "DigiKeySearcher", _DigiKeySearcher)

    items = [
        BomItem(mpn=f"MPN{index}", quantity=1, pricing_quantity=1)
        for index in range(16)
    ]
    worker = jlcpcb_searcher_module.SearchWorker(items, "app", "access", "secret")
    completed = []
    item_results = []
    worker.finished_all.connect(completed.append)
    worker._item_callback = (
        lambda index, item: item_results.append((index, item.mpn))
    )

    started = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started

    assert elapsed < 0.45
    assert completed == [items]
    assert sorted(item_results) == [
        (index, f"MPN{index}") for index in range(16)
    ]
    assert [item.mpn for item in items] == [f"MPN{index}" for index in range(16)]
    assert [item.jlcpcb_part_number for item in items] == [
        f"C-MPN{index}" for index in range(16)
    ]


class _Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_complete_jop_data_does_not_download_product_page(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    posted_bodies = []

    def fake_post(*args, **kwargs):
        posted_bodies.append(kwargs["data"])
        return _Response({
            "success": True,
            "code": 200,
            "data": [{
                "componentCode": "C127833",
                "componentModel": "C0603C104K5RACTU",
                "stockCount": 245702,
                "priceRanges": [
                    {"startQuantity": 1, "endQuantity": 499, "unitPrice": 0.0266},
                    {"startQuantity": 500, "endQuantity": -1, "unitPrice": 0.0235},
                ],
            }],
        })

    page_calls = []
    monkeypatch.setattr(searcher.session, "post", fake_post)
    monkeypatch.setattr(searcher.session, "get", lambda *args, **kwargs: page_calls.append(args))

    result = searcher.search_lcsc(
        "C127833",
        "C0603C104K5RACTU",
        required_stock=1,
        refresh=True,
    )

    assert json.loads(posted_bodies[0]) == {"componentCodes": ["C127833"]}
    assert result.stock == 245702
    assert result.unit_price == 0.0266
    assert json.loads(result.price_breaks_raw) == [
        {"qFrom": 1, "qTo": 499, "price": 0.0266},
        {"qFrom": 500, "qTo": None, "price": 0.0235},
    ]
    assert result.data_source == "JOP"
    assert result.warnings == []
    assert page_calls == []


def test_page_scraping_only_fills_fields_missing_from_official_api(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    monkeypatch.setattr(
        searcher.session,
        "post",
        lambda *args, **kwargs: _Response({
            "code": 200,
            "data": [{"componentCode": "C1"}],
        }),
    )
    page_html = r'''\"componentCode\":\"C1\",\"startNumber\":1,\"endNumber\":-1,\"productPrice\":0.5
    \"overseasStockCount\":25'''
    monkeypatch.setattr(
        searcher.session,
        "get",
        lambda *args, **kwargs: _Response(text=page_html),
    )

    result = searcher.search_lcsc("C1", "MPN1", required_stock=1, refresh=True)

    assert result.stock == 25
    assert result.unit_price == 0.5
    assert result.data_source == "PAGE_FALLBACK"
    assert result.warnings == []


def test_jop_price_ranges_are_normalized_when_page_is_unavailable(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    monkeypatch.setattr(
        searcher.session,
        "post",
        lambda *args, **kwargs: _Response({
            "code": 200,
            "data": [{
                "stockCount": 100,
                "priceRanges": [
                    {"startQuantity": 1, "endQuantity": 49, "unitPrice": 1.0},
                    {"startQuantity": 50, "endQuantity": -1, "unitPrice": 0.75},
                ],
            }],
        }),
    )
    monkeypatch.setattr(
        searcher.session,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    result = searcher.search_lcsc("C1", "MPN1", required_stock=60, refresh=True)

    assert result.stock == 100
    assert result.unit_price == 1.0
    assert result.data_source == "JOP"


def test_jop_preserves_zero_from_first_present_stock_field(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    monkeypatch.setattr(
        searcher.session,
        "post",
        lambda *args, **kwargs: _Response({
            "code": 200,
            "data": [{"stockCount": 0, "stock": 100, "availableStock": 200}],
        }),
    )
    monkeypatch.setattr(
        searcher, "_fetch_jlcpcb_part_page_data", lambda code: (0, "", None)
    )

    result = searcher.search_lcsc("C1", "MPN1", required_stock=1, refresh=True)

    assert result.stock == 0


def test_enrichment_updates_an_approved_item_even_when_skip_flag_is_set():
    item = BomItem(
        mpn="MPN1",
        jlcpcb_part_number="C1",
        available_stock_qty=10,
        unit_price=9.0,
        skip_jlcpcb=True,
    )
    result = JlcpcbSearchResult()
    result.found = True
    result.exact_match = True
    result.lcsc_code = "C1"
    result.matched_mpn = "MPN1"
    result.stock = 250
    result.unit_price = 1.25
    result.price_breaks_raw = '[{"qFrom": 1, "price": 1.25}]'

    enrich_bom_item(item, result)

    assert item.available_stock_qty == 250
    assert item.unit_price == 1.25
    assert item.jlcpcb_part_number == "C1"


def test_low_stock_is_informational_and_keeps_valid_lcsc_code():
    item = BomItem(mpn="MPN1", required_stock=1000)
    result = JlcpcbSearchResult()
    result.found = True
    result.exact_match = True
    result.lcsc_code = "C1"
    result.matched_mpn = "MPN1"
    result.stock = 5
    result.unit_price = 1.25

    enrich_bom_item(item, result)

    assert item.required_stock == 1000
    assert item.available_stock_qty == 5
    assert item.jlcpcb_part_number == "C1"
    assert item.unit_price == 1.25
    assert item.status == ""


def test_failed_refresh_does_not_leave_old_jlcpcb_values_visible():
    item = BomItem(
        mpn="MPN1",
        jlcpcb_part_number="C-OLD",
        available_stock_qty=999,
        unit_price=3.0,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 3.0}]',
    )
    result = JlcpcbSearchResult()
    result.error = "temporary API failure"

    enrich_bom_item(item, result)

    assert item.jlcpcb_part_number == ""
    assert item.available_stock_qty is None
    assert item.unit_price is None
    assert item.jlcpcb_price_breaks_raw == ""


def test_force_refresh_error_keeps_last_successful_item_snapshot(monkeypatch):
    class _JlcpcbSearcher:
        def __init__(self, *args, **kwargs):
            pass

        def search_mpn(self, *args, **kwargs):
            raise RuntimeError("unexpected refresh failure")

        def close(self):
            pass

    class _DigiKeySearcher:
        is_configured = False

        def close(self):
            pass

    monkeypatch.setattr(jlcpcb_searcher_module, "DatabaseManager", lambda: object())
    monkeypatch.setattr(jlcpcb_searcher_module, "JlcpcbSearcher", _JlcpcbSearcher)
    monkeypatch.setattr(jlcpcb_searcher_module, "DigiKeySearcher", _DigiKeySearcher)

    original = BomItem(
        mpn="MPN1",
        quantity=1,
        pricing_quantity=1,
        jlcpcb_part_number="C1",
        available_stock_qty=100,
        unit_price=1.5,
        digikey_part_number="DK1",
        digikey_stock_qty=200,
        digikey_unit_price=2.0,
    )
    worker = jlcpcb_searcher_module.SearchWorker(
        [original], "app", "access", "secret", force_refresh=True
    )
    completed = []
    errors = []
    worker.finished_all.connect(completed.append)
    worker.error.connect(errors.append)

    worker.run()

    assert completed == []
    assert errors == ["unexpected refresh failure"]
    assert original.jlcpcb_part_number == "C1"
    assert original.available_stock_qty == 100
    assert original.unit_price == 1.5
    assert original.digikey_part_number == "DK1"
    assert original.digikey_stock_qty == 200
    assert original.digikey_unit_price == 2.0


def test_pending_lcsc_does_not_block_approved_digikey_pricing(monkeypatch):
    class _Database:
        def get_internal_mapping(self, comment_code):
            return {
                "comment_code": comment_code,
                "mpn": "MAPPED-MPN",
                "lcsc_code": "C-OLD",
                "digikey_code": "DK-APPROVED",
                "approved": 0,
                "lcsc_approved": 0,
                "digikey_approved": 1,
                "last_found_lcsc": "C-OLD",
                "last_found_digikey": "DK-APPROVED",
            }

        def insert_pending_suggestion(self, **kwargs):
            self.pending = kwargs

    class _JlcpcbSearcher:
        def __init__(self, *args, **kwargs):
            pass

        def search_mpn(self, *args, **kwargs):
            result = JlcpcbSearchResult()
            result.found = True
            result.exact_match = True
            result.matched_mpn = "MPN1"
            result.lcsc_code = "C-NEW"
            return result

        def close(self):
            pass

    class _DigiKeySearcher:
        is_configured = True

        def search_mpn(self, *args, **kwargs):
            result = DigiKeySearchResult()
            result.configured = True
            result.found = True
            result.exact_match = True
            result.matched_mpn = "MPN1"
            result.digikey_part_number = "DK-LIVE"
            result.stock = 50
            result.unit_price = 2.0
            result.price_breaks = [(1, 2.0)]
            return result

        def close(self):
            pass

    database = _Database()
    monkeypatch.setattr(jlcpcb_searcher_module, "DatabaseManager", lambda: database)
    monkeypatch.setattr(jlcpcb_searcher_module, "JlcpcbSearcher", _JlcpcbSearcher)
    monkeypatch.setattr(jlcpcb_searcher_module, "DigiKeySearcher", _DigiKeySearcher)

    item = BomItem(mpn="MPN1", comment="INTERNAL1", quantity=1, pricing_quantity=1)
    worker = jlcpcb_searcher_module.SearchWorker([item], "app", "access", "secret")
    worker.run()

    assert item.mpn == "MPN1"
    assert item.status == "Pending Approval"
    assert item.jlcpcb_part_number == ""
    assert item.digikey_part_number == "DK-APPROVED"
    assert item.digikey_stock_qty == 50
    assert item.digikey_unit_price == 2.0


def test_pending_change_keeps_approved_supplier_codes_in_processed_bom(monkeypatch):
    class _Database:
        def get_internal_mapping(self, comment_code):
            return {
                "mpn": "MPN1",
                "lcsc_code": "C-APPROVED",
                "digikey_code": "DK-APPROVED",
                "approved": 1,
                "lcsc_approved": 1,
                "digikey_approved": 1,
                "lcsc_pending_change": 1,
                "digikey_pending_change": 1,
                "last_found_lcsc": "C-CANDIDATE",
                "last_found_digikey": "DK-CANDIDATE",
            }

        def insert_pending_suggestion(self, **kwargs):
            self.pending = kwargs

    class _JlcpcbSearcher:
        def __init__(self, *args, **kwargs):
            pass

        def search_lcsc(self, *args, **kwargs):
            result = JlcpcbSearchResult()
            result.found = True
            result.exact_match = True
            result.lcsc_code = "C-APPROVED"
            return result

        def close(self):
            pass

    class _DigiKeySearcher:
        is_configured = False

        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(jlcpcb_searcher_module, "DatabaseManager", _Database)
    monkeypatch.setattr(jlcpcb_searcher_module, "JlcpcbSearcher", _JlcpcbSearcher)
    monkeypatch.setattr(jlcpcb_searcher_module, "DigiKeySearcher", _DigiKeySearcher)

    item = BomItem(mpn="MPN1", comment="R1", quantity=1, pricing_quantity=1)
    worker = jlcpcb_searcher_module.SearchWorker([item], "app", "access", "secret")
    worker.run()

    assert item.status == "Pending Approval"
    assert item.jlcpcb_part_number == "C-APPROVED"
    assert item.digikey_part_number == "DK-APPROVED"


def test_fetch_jlcpcb_part_page_data_structured_json(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    json_data = {
        "props": {
            "pageProps": {
                "component": {
                    "componentCode": "C9999",
                    "overseasStockCount": 1500,
                    "priceRanges": [
                        {"startNumber": 1, "endNumber": 99, "productPrice": 0.12},
                        {"startNumber": 100, "endNumber": -1, "productPrice": 0.08},
                    ],
                }
            }
        }
    }
    html = f'''<html><head><script id="__NEXT_DATA__" type="application/json">{json.dumps(json_data)}</script></head><body></body></html>'''
    monkeypatch.setattr(searcher.session, "get", lambda *args, **kwargs: _Response(text=html))

    stock, price_breaks_raw, error = searcher._fetch_jlcpcb_part_page_data("C9999")
    assert stock == 1500
    assert error is None
    parsed_breaks = json.loads(price_breaks_raw)
    assert parsed_breaks[0] == {"qFrom": 1, "qTo": 99, "price": 0.12}
    assert parsed_breaks[1] == {"qFrom": 100, "qTo": None, "price": 0.08}


def test_search_lcsc_warns_when_official_missing_and_page_fallback_fails(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    monkeypatch.setattr(
        searcher.session,
        "post",
        lambda *args, **kwargs: _Response({
            "code": 200,
            "data": [{"componentCode": "C1"}],
        }),
    )
    # Page returns 404
    class _ErrorResponse:
        status_code = 404
        text = "Not Found"
        def raise_for_status(self):
            import requests
            raise requests.exceptions.HTTPError("404 Client Error: Not Found")

    monkeypatch.setattr(searcher.session, "get", lambda *args, **kwargs: _ErrorResponse())

    result = searcher.search_lcsc("C1", "MPN1", required_stock=1, refresh=True)
    assert any("Official stock missing and page fallback failed" in w for w in result.warnings)
    assert any("Official price tiers missing and page fallback failed" in w for w in result.warnings)
