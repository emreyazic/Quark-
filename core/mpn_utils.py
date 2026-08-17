"""MPN utility functions: normalization, matching, cleaning, heuristics.

This module provides pure functions used throughout the BOM enrichment pipeline
to ensure consistent MPN comparison, detect RES-coded components, compute
required stock, and distinguish manufacturer names from part numbers.
"""

import re
import unicodedata
from typing import Optional


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


def compute_required_stock(quantity: int) -> int:
    """Compute the required stock threshold for a BOM component.

    Formula: (Quantity × 10) + 10
    """
    return (quantity * 10) + 10


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

        valid_breaks = []
        for pb in breaks:
            price = pb.get("price")
            if price is not None:
                valid_breaks.append((int(pb.get("qFrom") or 0), float(price)))

        if not valid_breaks:
            return None

        valid_breaks.sort(key=lambda tier: tier[0])
        if not use_quantity_breaks:
            return round(valid_breaks[0][1], 6)

        selected = valid_breaks[0][1]
        for minimum_quantity, price in valid_breaks:
            if quantity >= minimum_quantity:
                selected = price
            else:
                break
        return round(selected, 6)

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

    ordered = sorted(price_breaks, key=lambda tier: tier[0])
    if not use_quantity_breaks:
        return ordered[0][1]

    selected = ordered[0][1]
    for minimum_quantity, price in ordered:
        if quantity >= minimum_quantity:
            selected = price
        else:
            break
    return selected
