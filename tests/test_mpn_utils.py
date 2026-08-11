import pytest
from core.mpn_utils import (
    normalize_mpn,
    is_exact_mpn_match,
    clean_mpn_value,
    is_res_coded,
    compute_required_stock,
    is_mpn_like,
    is_manufacturer_like,
    select_unit_price,
)

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
    assert compute_required_stock(5) == 60
    assert compute_required_stock(1) == 20
    assert compute_required_stock(0) == 10

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
    assert select_unit_price(price_json, 250) == 0.017
    assert select_unit_price(price_json, 10) == 0.022143  # Fallback to first
    assert select_unit_price("", 10) is None
