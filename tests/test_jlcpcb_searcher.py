import json

from core.jlcpcb_searcher import JlcpcbSearcher, JlcpcbSearchResult, enrich_bom_item
from models.bom_item import BomItem


class _Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_jop_keeps_c_prefix_and_prefers_jlcpcb_page_stock_and_prices(monkeypatch):
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

    page_html = r'''\"componentCode\":\"C127833\",\"startNumber\":1,\"endNumber\":499,\"productPrice\":0.0264
    \"componentCode\":\"C127833\",\"startNumber\":500,\"endNumber\":-1,\"productPrice\":0.0233
    \"overseasStockCount\":245702'''
    monkeypatch.setattr(searcher.session, "post", fake_post)
    monkeypatch.setattr(searcher.session, "get", lambda *args, **kwargs: _Response(text=page_html))

    result = searcher.search_lcsc(
        "C127833",
        "C0603C104K5RACTU",
        required_stock=1,
        refresh=True,
    )

    assert json.loads(posted_bodies[0]) == {"componentCodes": ["C127833"]}
    assert result.stock == 245702
    assert result.unit_price == 0.0264
    assert json.loads(result.price_breaks_raw) == [
        {"qFrom": 1, "qTo": 499, "price": 0.0264},
        {"qFrom": 500, "qTo": None, "price": 0.0233},
    ]
    assert result.data_source == "JLCPCB_PAGE_V1"


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
    assert result.data_source == "JLCPCB_OPENAPI_V2"


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


def test_failed_refresh_does_not_leave_old_jlcpcb_values_visible():
    item = BomItem(
        mpn="MPN1",
        jlcpcb_part_number="C-OLD",
        available_stock_qty=999,
        unit_price=3.0,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 3.0}]',
    )
    item.available_stock_qty = None
    item.unit_price = None
    item.jlcpcb_price_breaks_raw = ""
    result = JlcpcbSearchResult()
    result.error = "temporary API failure"

    enrich_bom_item(item, result)

    assert item.jlcpcb_part_number == ""
    assert item.available_stock_qty is None
    assert item.unit_price is None
    assert item.jlcpcb_price_breaks_raw == ""
