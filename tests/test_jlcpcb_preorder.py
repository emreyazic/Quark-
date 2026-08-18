import core.jlcpcb_searcher as search_module
from core.base_excel_writer import BaseExcelWriter
from core.database_manager import DatabaseManager
from core.digikey_searcher import DigiKeySearchResult, enrich_bom_item_digikey
from core.jlcpcb_searcher import JlcpcbSearcher, JlcpcbSearchResult, SearchWorker, enrich_bom_item
from core.supplier_availability import AvailabilityState, normalize_availability
from models.bom_item import BomItem
from services.project_pricing import calculate_item_pricing


class _Response:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _product(code, *, preorder=False, stock=100):
    return {
        "productModel": "MPN1",
        "productCode": code,
        "is_pre_sale": preorder,
        "stockNumber": stock,
    }


def test_first_exact_preorder_is_skipped_for_normal_candidate(monkeypatch):
    searcher = JlcpcbSearcher()
    response = _Response({"result": {"exactMatchResult": [_product("C1", preorder=True), _product("C2")]}})
    monkeypatch.setattr(searcher, "_request_with_retry", lambda *args, **kwargs: response)
    assert searcher._resolve_lcsc_from_mpn("MPN1") == "C2"


def test_all_exact_candidates_preorder_returns_preorder_only(monkeypatch):
    searcher = JlcpcbSearcher(max_retries=1)
    official = _Response({"result": {"exactMatchResult": [_product("C1", preorder=True), _product("C2", preorder=True)]}})
    community = _Response({"components": []})
    monkeypatch.setattr(
        searcher,
        "_request_with_retry",
        lambda method, url, **kwargs: official if method == "POST" else community,
    )
    result = searcher.search_mpn("MPN1")
    assert result.availability == AvailabilityState.PREORDER
    assert result.preorder_only
    assert not result.found


def test_official_preorder_cannot_be_overridden_by_page_fallback(monkeypatch):
    searcher = JlcpcbSearcher("app", "access", "secret")
    response = _Response({"code": 200, "data": [{
        "componentCode": "C1", "stockCount": 500, "isPreSale": True,
        "priceRanges": [{"qFrom": 1, "price": 0.01}],
    }]})
    monkeypatch.setattr(searcher, "_request_with_retry", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        searcher,
        "_fetch_jlcpcb_part_page_data",
        lambda *_: (_ for _ in ()).throw(AssertionError("page fallback must not run")),
    )
    result = searcher.search_lcsc("C1", "MPN1", required_stock=10, refresh=True)
    assert result.availability == AvailabilityState.PREORDER
    assert not result.found
    assert result.unit_price is None


def test_description_text_does_not_trigger_preorder():
    payload = {"description": "Controller for pre-order queue", "stockCount": 20}
    assert normalize_availability(payload, 20) == AvailabilityState.IN_STOCK


def test_preorder_boolean_enum_and_text_variants_are_normalized():
    payloads = [
        {"is_pre_sale": True},
        {"isPreSale": "true"},
        {"availabilityStatus": "pre-order"},
        {"stockStatus": "pre order"},
        {"saleStatus": "PREORDER"},
    ]
    assert all(normalize_availability(payload) == AvailabilityState.PREORDER for payload in payloads)


def test_preorder_stock_and_price_are_not_available_or_priced():
    result = JlcpcbSearchResult()
    result.exact_match = True
    result.lcsc_code = "C1"
    result.matched_mpn = "MPN1"
    result.stock = 1000
    result.unit_price = 0.01
    result.price_breaks_raw = '[{"qFrom": 1, "price": 0.01}]'
    result.availability = AvailabilityState.PREORDER
    result.preorder_only = True
    item = BomItem(mpn="MPN1", required_stock=10)
    enrich_bom_item(item, result)
    pricing = calculate_item_pricing(item, 10)
    assert item.jlcpcb_status == "preorder"
    assert not item.is_available
    assert item.get_ui_category() == "manual"
    assert item.unit_price is None
    assert pricing.quote is None


def test_preorder_jlcpcb_falls_back_to_digikey_for_results_and_excel():
    item = BomItem(
        mpn="MPN1", jlcpcb_part_number="C1", available_stock_qty=1000,
        unit_price=0.01, jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 0.01}]',
        jlcpcb_status="preorder", jlcpcb_availability="PREORDER",
        digikey_part_number="DK1", digikey_stock_qty=1000,
        digikey_unit_price=0.05, digikey_price_breaks=[(1, 0.05)], digikey_status="found",
    )
    pricing = calculate_item_pricing(item, 10)
    writer = BaseExcelWriter.__new__(BaseExcelWriter)
    writer.pricing_mode = "project"
    excel = writer._get_pricing_for_component_item(item, 10)
    assert pricing.quote and pricing.quote.supplier == "DigiKey"
    assert excel["selected_source"] == "DigiKey"
    assert item.is_available


def test_existing_approved_preorder_is_preserved_and_pending_invalidated(tmp_path):
    db = DatabaseManager(str(tmp_path / "preorder.sqlite"))
    db.approve_supplier_mapping("IC1", "JLCPCB", "C1", "MPN1")
    db.refresh_mapping_codes("IC1", "C2", None)
    assert db.invalidate_lcsc_preorder_candidate("IC1", "C2")
    mapping = db.get_internal_mapping("IC1")
    assert mapping["lcsc_code"] == "C1"
    assert mapping["lcsc_approved"] == 1
    assert mapping["lcsc_pending_change"] == 0
    assert mapping["last_found_lcsc"] == ""
    history = db.get_mapping_audit_history("IC1", "JLCPCB")
    assert history[0]["action"] == "AUTO_INVALIDATED_PREORDER"


def test_approved_code_live_preorder_needs_review_and_cannot_source(tmp_path):
    db = DatabaseManager(str(tmp_path / "approved-live.sqlite"))
    db.approve_supplier_mapping("IC1", "JLCPCB", "C1", "MPN1")
    result = JlcpcbSearchResult()
    result.exact_match = True
    result.lcsc_code = "C1"
    result.matched_mpn = "MPN1"
    result.stock = 500
    result.unit_price = 0.01
    result.availability = AvailabilityState.PREORDER
    item = BomItem(mpn="MPN1")
    enrich_bom_item(item, result)
    assert db.get_internal_mapping("IC1")["lcsc_code"] == "C1"
    assert item.status == "Pre-order — Needs Review"
    assert calculate_item_pricing(item, 1).quote is None


def test_search_worker_does_not_create_lcsc_pending_for_preorder(monkeypatch):
    class _Database:
        def get_internal_mapping(self, _code):
            return None

        def invalidate_lcsc_preorder_candidate(self, *_args, **_kwargs):
            return False

        def insert_pending_suggestion(self, **kwargs):
            self.pending = kwargs

    class _Jlcpcb:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_mpn(self, *_args, **_kwargs):
            result = JlcpcbSearchResult()
            result.exact_match = True
            result.lcsc_code = "C-PRE"
            result.matched_mpn = "MPN1"
            result.stock = 100
            result.availability = AvailabilityState.PREORDER
            result.preorder_only = True
            return result

        def close(self):
            pass

    class _DigiKey:
        is_configured = False

        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    database = _Database()
    monkeypatch.setattr(search_module, "DatabaseManager", lambda: database)
    monkeypatch.setattr(search_module, "JlcpcbSearcher", _Jlcpcb)
    monkeypatch.setattr(search_module, "DigiKeySearcher", _DigiKey)
    item = BomItem(mpn="MPN1", comment="IC1", quantity=1, pricing_quantity=1)
    worker = SearchWorker([item], "app", "access", "secret")
    worker.run()
    assert database.pending["lcsc_pending_enabled"] is False
    assert database.pending["lcsc_code"] == ""
    assert item.jlcpcb_status == "preorder"


def test_preorder_category_counts_are_mutually_exclusive():
    items = [
        BomItem(jlcpcb_status="preorder", jlcpcb_availability="PREORDER"),
        BomItem(jlcpcb_status="not_found", digikey_status="not_found"),
        BomItem(jlcpcb_status="preorder", jlcpcb_availability="PREORDER", digikey_status="found"),
    ]
    categories = [item.get_ui_category() for item in items]
    assert categories == ["manual", "not_found", "available"]
    assert sum(categories.count(name) for name in set(categories)) == len(items)


def test_digikey_enrichment_is_unchanged_by_jlcpcb_preorder_state():
    item = BomItem(jlcpcb_status="preorder", jlcpcb_availability="PREORDER")
    result = DigiKeySearchResult()
    result.configured = True
    result.found = True
    result.exact_match = True
    result.matched_mpn = "MPN1"
    result.digikey_part_number = "DK1"
    result.stock = 100
    result.unit_price = 0.5
    result.price_breaks = [(1, 0.5)]
    enrich_bom_item_digikey(item, result)
    assert item.digikey_status == "found"
    assert item.digikey_part_number == "DK1"
    assert item.is_available
