from core.digikey_searcher import DigiKeySearcher


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
    searcher.close()


def test_digikey_prefers_cut_tape_before_other_moq_one_variations():
    variations = [
        {"DigiKeyProductNumber": "TRAY", "MinimumOrderQuantity": 1, "PackageType": {"Name": "Tray"}},
        {"DigiKeyProductNumber": "CT", "MinimumOrderQuantity": 1, "PackageType": {"Name": "Cut Tape (CT)"}},
    ]

    assert DigiKeySearcher._select_variation(variations)["DigiKeyProductNumber"] == "CT"
