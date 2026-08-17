import pytest
from core.mpn_utils import (
    normalize_mpn,
    is_exact_mpn_match,
    clean_mpn_value,
    is_res_coded,
    compute_required_stock,
    parse_positive_integer_quantity,
    is_mpn_like,
    is_manufacturer_like,
    select_unit_price,
    select_digikey_price,
)


def test_price_break_normalization_rejects_unsafe_values_and_resolves_duplicates(caplog):
    from core.mpn_utils import normalize_digikey_price_breaks, normalize_jlcpcb_price_breaks

    digikey, warnings = normalize_digikey_price_breaks([
        (10, 2.0), (1, -1), (10, 1.5), (5.5, 1.0), (20, float("nan")), (2, float("inf")),
    ])
    assert digikey == [(10, 1.5)]
    assert warnings
    assert select_digikey_price(digikey, 10, use_quantity_breaks=True) == 1.5

    jlcpcb, warnings = normalize_jlcpcb_price_breaks(
        '[{"qFrom": 10, "price": 2}, {"qFrom": 1, "price": -3}, '
        '{"qFrom": 10, "price": 1.25}, {"qFrom": 20, "price": NaN}]'
    )
    assert jlcpcb == [{"qFrom": 10, "qTo": None, "price": 1.25}]
    assert warnings
    assert "conflicting duplicate quantity 10" in caplog.text


def test_malformed_price_breaks_cannot_produce_negative_cost():
    assert select_unit_price('[{"qFrom": 1, "price": -0.5}]', 10) is None
    assert select_digikey_price([(1, -0.5), (2, float("inf"))], 10) is None

def test_normalize_mpn():
    assert normalize_mpn(" LG R971-KN-1 ") == "lg r971-kn-1"
    assert normalize_mpn("GRM188R72A104KA35D") == "grm188r72a104ka35d"
    assert normalize_mpn("744316100\u00A0") == "744316100"  # Non-breaking space
    assert normalize_mpn("  A   B  ") == "a b"

def test_is_exact_mpn_match():
    assert is_exact_mpn_match("LG R971-KN-1", "LG R971-KN-1") is True
    assert is_exact_mpn_match("LG R971-KN-1", "lg r971-kn-1") is True
    # Test that prefix match is REJECTED
    assert is_exact_mpn_match("LG R971-KN-1", "LG R971-KN-1-0-20-R18") is False

def test_clean_mpn_value():
    assert clean_mpn_value("123\n456") == "123 456"
    assert clean_mpn_value("ABC\u00A0DEF") == "ABC DEF"

def test_is_res_coded():
    assert is_res_coded("RES010251") is True
    assert is_res_coded("RES050003") is True
    assert is_res_coded("R1") is False
    assert is_res_coded("RESISTOR") is False
    assert is_res_coded("res010") is True

def test_compute_required_stock():
    assert compute_required_stock(5) == 5
    assert compute_required_stock(1) == 1
    assert compute_required_stock(0) == 0
    assert compute_required_stock(None) == 0
    assert compute_required_stock(1.5) == 0
    assert compute_required_stock(float("nan")) == 0
    assert compute_required_stock(float("inf")) == 0


@pytest.mark.parametrize("value", [0, -1, 1.5, "2.25", float("nan"), float("inf"), None, True])
def test_component_quantity_rejects_non_positive_or_non_integer_values(value):
    with pytest.raises(ValueError):
        parse_positive_integer_quantity(value)


@pytest.mark.parametrize(("value", "expected"), [(1, 1), (2.0, 2), ("3", 3), ("4.0", 4)])
def test_component_quantity_accepts_positive_integer_values(value, expected):
    assert parse_positive_integer_quantity(value) == expected

def test_is_mpn_like():
    assert is_mpn_like("GRM188R72A104KA35D") is True
    assert is_mpn_like("RC0603FR-0710KL") is True
    assert is_mpn_like("Panasonic Electronic Components") is False

def test_is_manufacturer_like():
    assert is_manufacturer_like("Würth Elektronik") is True
    assert is_manufacturer_like("Murata Electronics") is True
    assert is_manufacturer_like("Texas Instruments") is True
    assert is_manufacturer_like("GRM188") is False

def test_select_unit_price():
    price_json = '[{"qFrom": 20, "qTo": 180, "price": 0.022142857}, {"qFrom": 200, "qTo": null, "price": 0.017}]'
    assert select_unit_price(price_json, 50) == 0.022143
    assert select_unit_price(price_json, 250) == 0.022143
    assert select_unit_price(price_json, 10) == 0.022143  # Fallback to first
    assert select_unit_price("", 10) is None


def test_supplier_unit_prices_do_not_change_with_quantity():
    jlcpcb_prices = '[{"qFrom": 1, "qTo": 9, "price": 2.0}, {"qFrom": 10, "qTo": null, "price": 1.0}]'
    digikey_prices = [(1, 2.0), (10, 1.0)]

    assert select_unit_price(jlcpcb_prices, 1000) == 2.0
    assert select_digikey_price(digikey_prices, 1000) == 2.0


def test_supplier_project_pricing_uses_applicable_quantity_tier():
    jlcpcb_prices = '[{"qFrom": 1, "qTo": 9, "price": 5.0}, {"qFrom": 10, "qTo": null, "price": 3.5}]'
    digikey_prices = [(1, 5.0), (10, 3.5)]

    jlcpcb_unit = select_unit_price(jlcpcb_prices, 15, use_quantity_breaks=True)
    digikey_unit = select_digikey_price(digikey_prices, 15, use_quantity_breaks=True)

    assert jlcpcb_unit == 3.5
    assert digikey_unit == 3.5
    assert 15 * jlcpcb_unit == 52.5
    assert 15 * digikey_unit == 52.5


def test_jlcpcb_project_pricing_respects_qto_and_does_not_cross_gaps():
    prices = (
        '[{"qFrom": 1, "qTo": 9, "price": 5.0}, '
        '{"qFrom": 10, "qTo": 19, "price": 4.0}, '
        '{"qFrom": 25, "qTo": null, "price": 3.0}]'
    )

    assert select_unit_price(prices, 9, use_quantity_breaks=True) == 5.0
    assert select_unit_price(prices, 10, use_quantity_breaks=True) == 4.0
    assert select_unit_price(prices, 19, use_quantity_breaks=True) == 4.0
    assert select_unit_price(prices, 20, use_quantity_breaks=True) is None
    assert select_unit_price(prices, 24, use_quantity_breaks=True) is None
    assert select_unit_price(prices, 25, use_quantity_breaks=True) == 3.0


def test_jlcpcb_project_pricing_falls_back_when_quantity_below_first_tier():
    prices = (
        '[{"qFrom": 20, "qTo": 99, "price": 0.05}, '
        '{"qFrom": 100, "qTo": null, "price": 0.03}]'
    )
    # Quantity below first tier qFrom (20) should fall back to first tier price (0.05)
    assert select_unit_price(prices, 5, use_quantity_breaks=True) == 0.05
    assert select_unit_price(prices, 19, use_quantity_breaks=True) == 0.05
    assert select_unit_price(prices, 20, use_quantity_breaks=True) == 0.05
    assert select_unit_price(prices, 150, use_quantity_breaks=True) == 0.03
