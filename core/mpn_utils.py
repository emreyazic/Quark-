"""MPN utility functions: normalization, matching, cleaning, heuristics.

This module provides pure functions used throughout the BOM enrichment pipeline
to ensure consistent MPN comparison, detect RES-coded components, compute
required stock, and distinguish manufacturer names from part numbers.
"""

import re
import unicodedata
import math
import json
import logging
from typing import Optional


def _positive_int(value):
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError
    return int(number)


def _valid_price(value):
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError
    return number


def normalize_price_breaks(price_breaks, supplier="supplier", logger=None):
    """Normalize JLCPCB dict tiers or DigiKey tuple tiers into sorted tuples."""
    warnings = []
    grouped = {}
    for raw in price_breaks or []:
        try:
            if isinstance(raw, dict):
                quantity = _positive_int(raw.get("qFrom"))
                price = _valid_price(raw.get("price"))
                raw_to = raw.get("qTo")
                quantity_to = None if raw_to in (None, -1, "-1", "") else _positive_int(raw_to)
                if quantity_to is not None and quantity_to < quantity:
                    raise ValueError
            else:
                quantity = _positive_int(raw[0])
                price = _valid_price(raw[1])
                quantity_to = None
        except (TypeError, ValueError, IndexError, OverflowError):
            warnings.append(f"{supplier}: invalid price break rejected: {raw!r}")
            continue
        candidate = (quantity, quantity_to, price)
        previous = grouped.get(quantity)
        if previous is not None and previous != candidate:
            warnings.append(f"{supplier}: conflicting duplicate quantity {quantity}")
        # Resolve duplicates independently of input order: lowest valid price,
        # then the narrowest finite range wins.
        grouped[quantity] = min(
            filter(None, (previous, candidate)),
            key=lambda tier: (tier[2], float("inf") if tier[1] is None else tier[1]),
        )
    if warnings:
        (logger or logging.getLogger(__name__)).warning("; ".join(warnings))
    return [grouped[key] for key in sorted(grouped)], warnings


def normalize_jlcpcb_price_breaks(price_json_str: str, logger=None):
    try:
        raw = json.loads(price_json_str) if price_json_str else []
    except (json.JSONDecodeError, TypeError):
        raw = []
    if not isinstance(raw, list):
        raw = []
    normalized, warnings = normalize_price_breaks(raw, "JLCPCB", logger)
    return [
        {"qFrom": quantity, "qTo": quantity_to, "price": price}
        for quantity, quantity_to, price in normalized
    ], warnings


def normalize_digikey_price_breaks(price_breaks, logger=None):
    normalized, warnings = normalize_price_breaks(price_breaks, "DigiKey", logger)
    return [(quantity, price) for quantity, _, price in normalized], warnings


# ─── Known manufacturer name fragments (case-insensitive) ───────────────────
# Used by is_manufacturer_like() heuristic — intentionally broad but not
# exhaustive; the heuristic combines this with structural checks.
_KNOWN_MFR_FRAGMENTS = {
    "electronics", "electronic", "corporation", "corp", "inc", "ltd",
    "limited", "co.", "gmbh", "ag", "sa", "sas", "nv", "bv",
    "semiconductor", "semiconductors", "components", "devices",
    "technologies", "technology", "systems", "micro", "microelectronics",
    # Common manufacturer names
    "murata", "kemet", "tdk", "samsung", "yageo", "onsemi", "osram",
    "vishay", "panasonic", "texas instruments", "analog devices",
    "infineon", "stmicroelectronics", "nxp", "renesas", "rohm",
    "bourns", "littelfuse", "taiyo yuden", "susumu", "semitec",
    "wurth", "würth", "würth elektronik", "diodes", "maxim",
    "microchip", "silicon labs", "skyworks", "qorvo", "broadcom",
    "amphenol", "te connectivity", "molex", "hirose", "jst",
}


def normalize_mpn(mpn: str) -> str:
    """Normalize an MPN for comparison.

    - Strip leading/trailing whitespace
    - Replace non-breaking spaces and other Unicode spaces with regular space
    - Collapse repeated internal whitespace to single space
    - Case-fold to lowercase

    Does NOT remove hyphens, slashes, dots, or any meaningful characters.
    Does NOT remove suffixes or truncate.
    """
    if not mpn:
        return ""
    # Replace all Unicode whitespace variants with regular space
    s = re.sub(r'[\s\u00a0\u2000-\u200b\u202f\u205f\u3000]+', ' ', mpn)
    s = s.strip()
    s = s.casefold()
    return s


def is_exact_mpn_match(requested_mpn: str, candidate_mpn: str) -> bool:
    """Check if two MPNs are an exact match after normalization.

    This is the ONLY function that should decide whether a JLCPCB result
    corresponds to the requested MPN.  It does NOT accept:
    - prefix matches
    - contains matches
    - suffix-stripped matches
    - "close enough" matches
    """
    return normalize_mpn(requested_mpn) == normalize_mpn(candidate_mpn)


def clean_mpn_value(raw: str) -> str:
    """Clean a raw MPN value from an Excel cell.

    Removes invisible characters, non-breaking spaces, line breaks, and
    control characters that may come from Excel formatting.  Preserves
    hyphens, slashes, dots, and all meaningful punctuation.
    """
    if not raw:
        return ""
    
    # Replace line breaks and tabs with space BEFORE removing control characters
    s = raw.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')

    # Remove control characters (except space)
    s = "".join(
        ch for ch in s
        if ch == ' ' or not unicodedata.category(ch).startswith('C')
    )
    # Replace non-breaking spaces and other Unicode spaces with regular space
    s = re.sub(r'[\u00a0\u2000-\u200b\u202f\u205f\u3000]', ' ', s)
    
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def is_res_coded(comment: str) -> bool:
    """Check if a component's comment/internal code indicates a RES-coded part.

    RES-coded components have internal codes like RES010251, RES050003, etc.
    These start with 'RES' followed by digits.

    Important: This does NOT match designators like R1, R2 — only internal
    codes that start with 'RES' (case-insensitive).
    """
    if not comment:
        return False
    comment_stripped = comment.strip()
    # Match RES followed by digits (e.g. RES010251, RES050003)
    return bool(re.match(r'^RES\d', comment_stripped, re.IGNORECASE))


def parse_positive_integer_quantity(quantity) -> int:
    """Validate and return a component quantity as a positive integer.

    Electronic component quantities cannot be zero, fractional, NaN, or
    infinite. Invalid values raise ``ValueError`` instead of being rounded or
    silently promoted to one.
    """
    if quantity is None or isinstance(quantity, bool):
        raise ValueError(f"Invalid component quantity: {quantity!r}")
    if isinstance(quantity, str) and not quantity.strip():
        raise ValueError("Component quantity is empty")
    try:
        numeric = float(quantity)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid component quantity: {quantity!r}") from exc
    if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
        raise ValueError(f"Component quantity must be a positive integer: {quantity!r}")
    return int(numeric)


def compute_required_stock(quantity) -> int:
    """Return the actual component quantity required by the production run."""
    try:
        return parse_positive_integer_quantity(quantity)
    except ValueError:
        return 0


def is_mpn_like(value: str) -> bool:
    """Heuristic: does this value look like a Manufacturer Part Number?

    MPN-like values tend to:
    - Contain dense alphanumeric codes with hyphens, slashes, dots
    - Have a high ratio of digits and uppercase letters
    - Contain few or no natural-language words
    - Be relatively short (no long phrases)

    Returns True if the value looks more like a part number than a name.
    """
    if not value or len(value.strip()) < 3:
        return False

    v = value.strip()
    # Count character categories
    digits = sum(1 for c in v if c.isdigit())
    alphas = sum(1 for c in v if c.isalpha())
    total = len(v)

    if total == 0:
        return False

    digit_ratio = digits / total
    # MPNs typically have significant digit content
    if digit_ratio > 0.25:
        return True

    # MPNs often contain mixed alphanumeric with hyphens/slashes
    if re.search(r'[A-Z0-9]{3,}[-/][A-Z0-9]', v, re.IGNORECASE):
        return True

    # Short alphanumeric codes without spaces
    if ' ' not in v and len(v) >= 5 and digits >= 2:
        return True

    return False


def is_manufacturer_like(value: str) -> bool:
    """Heuristic: does this value look like a manufacturer name?

    Manufacturer names tend to:
    - Contain spaces and natural-language words
    - Include known company suffixes (Inc, Corp, Ltd, Electronics, etc.)
    - Have mostly alphabetic characters
    - Be longer phrases

    Returns True if the value looks more like a company name than a part number.
    """
    if not value or len(value.strip()) < 2:
        return False

    v = value.strip().lower()

    # Check against known manufacturer fragments
    for fragment in _KNOWN_MFR_FRAGMENTS:
        if fragment in v:
            return True

    # Mostly alphabetic with spaces suggests a name
    alphas = sum(1 for c in v if c.isalpha())
    total = len(v)
    if total > 0 and alphas / total > 0.85 and ' ' in v:
        return True

    return False


def select_unit_price(
    price_json_str: str,
    quantity: int,
    use_quantity_breaks: bool = False,
) -> Optional[float]:
    """Return the base or quantity-tier JLCPCB unit price.

    Args:
        price_json_str: JSON string of price breaks from JLCPCB API,
            e.g. '[{"qFrom": 20, "qTo": 180, "price": 0.022}, ...]'
        quantity: Quantity used when ``use_quantity_breaks`` is enabled.
        use_quantity_breaks: Select the best applicable quantity tier when true.

    Returns:
        The first advertised unit price, or None if unavailable.
    """
    if not price_json_str:
        return None

    import json
    try:
        breaks = json.loads(price_json_str)
        if not isinstance(breaks, list) or not breaks:
            return None

        valid_breaks, _ = normalize_price_breaks(breaks, "JLCPCB")

        if not valid_breaks:
            return None

        valid_breaks.sort(key=lambda tier: tier[0])
        if not use_quantity_breaks:
            return round(valid_breaks[0][2], 6)

        if quantity < valid_breaks[0][0]:
            return round(valid_breaks[0][2], 6)

        selected = None
        for minimum_quantity, maximum_quantity, price in valid_breaks:
            if quantity < minimum_quantity:
                continue
            if maximum_quantity is not None and quantity > maximum_quantity:
                continue
            # If malformed data contains overlapping ranges, prefer the most
            # specific/latest range (the one with the highest qFrom).
            selected = price
        return round(selected, 6) if selected is not None else None

    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        pass

    return None

def select_digikey_price(
    price_breaks: list[tuple[int, float]],
    quantity: int,
    use_quantity_breaks: bool = False,
) -> Optional[float]:
    """Return the base or quantity-tier DigiKey unit price.

    Args:
        price_breaks: List of (BreakQuantity, UnitPrice) tuples, sorted by BreakQuantity ascending.
        quantity: Quantity used when ``use_quantity_breaks`` is enabled.
        use_quantity_breaks: Select the best applicable quantity tier when true.

    Returns:
        The unit price, or None if unavailable.
    """
    if not price_breaks:
        return None

    ordered, _ = normalize_digikey_price_breaks(price_breaks)
    if not ordered:
        return None
    if not use_quantity_breaks:
        return ordered[0][1]

    selected = ordered[0][1]
    for minimum_quantity, price in ordered:
        if quantity >= minimum_quantity:
            selected = price
        else:
            break
    return selected
