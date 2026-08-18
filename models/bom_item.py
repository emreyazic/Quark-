from dataclasses import dataclass, field
from typing import ClassVar, Optional, Union


@dataclass
class ColumnMapping:
    """Stores indices mapping standard headers to actual spreadsheet columns."""

    board_identifier: Optional[int] = None
    mpn: Optional[int] = None
    quantity: Optional[int] = None
    manufacturer: Optional[int] = None
    description: Optional[int] = None
    designator: Optional[int] = None
    comment: Optional[int] = None
    footprint: Optional[int] = None
    value: Optional[int] = None

    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def get_mapped_fields(self) -> dict[str, int]:
        """Return a mapping of field names to column indices for non-None, non-negative mappings."""
        fields = [
            "board_identifier",
            "mpn",
            "quantity",
            "manufacturer",
            "description",
            "designator",
            "comment",
            "footprint",
            "value",
        ]
        return {
            field: getattr(self, field)
            for field in fields
            if getattr(self, field) is not None and getattr(self, field) >= 0
        }

    def has_duplicate_mappings(self) -> bool:
        """Check if any spreadsheet column index is mapped to multiple fields."""
        mapped = list(self.get_mapped_fields().values())
        return len(mapped) != len(set(mapped))

    def get_duplicate_fields(self) -> dict[int, list[str]]:
        """Return column indices that are mapped to multiple fields."""
        col_to_fields: dict[int, list[str]] = {}
        for field, col in self.get_mapped_fields().items():
            col_to_fields.setdefault(col, []).append(field)
        return {col: fields for col, fields in col_to_fields.items() if len(fields) > 1}

    def is_valid(self) -> bool:
        """Mapping is valid only if MPN and Quantity are mapped and there are no duplicate mappings."""
        if self.mpn is None or self.quantity is None:
            return False
        if self.mpn < 0 or self.quantity < 0:
            return False
        return not self.has_duplicate_mappings()


@dataclass
class BomFile:
    """Represents a loaded Excel file and its detection state."""

    file_path: str
    board_name: str
    sheet_name: str = ""
    headers: list[str] = field(default_factory=list)
    header_row_index: int = 1
    preview_rows: list[list] = field(default_factory=list)
    column_mapping: Optional[ColumnMapping] = None
    row_count: int = 0
    is_valid: bool = True
    error_message: str = ""
    duplicate_of: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class BomItem:
    """Represents a single component from a BOM file, enriched with JLCPCB data."""

    # Source BOM fields
    source_file_name: str = ""
    board_name: str = ""
    comment: str = ""
    description: str = ""
    designator: str = ""
    footprint: str = ""
    quantity: Union[int, float, str] = 0
    value: str = ""
    manufacturer: str = ""
    mpn: str = ""  # Manufacturer Part Number

    # Enrichment result fields
    jlcpcb_part_number: str = ""  # e.g. "C77058" — blank unless exact match + sufficient stock
    jlcpcb_category: Optional[str] = ""
    jlcpcb_package: Optional[str] = ""
    matched_mpn: str = ""  # The MPN exactly matched by JLCPCB
    exact_match: bool = False  # True only if requested MPN exactly matches source MPN
    available_stock_qty: Optional[int] = None
    required_stock: int = 0  # Actual quantity required by the production run
    unit_price: Optional[float] = None
    digikey_unit_price: Optional[float] = None
    digikey_stock_qty: Optional[int] = None
    digikey_part_number: str = ""
    production_quantity: Optional[Union[int, float]] = None
    safety_surplus: int = 0
    purchase_quantity: Optional[Union[int, float]] = None
    pricing_quantity: Union[int, float] = 1
    jlcpcb_total_price: Optional[float] = None
    digikey_total_price: Optional[float] = None
    
    jlcpcb_price_breaks_raw: str = ""
    digikey_price_breaks: list[tuple[int, float]] = field(default_factory=list)
    jlcpcb_min_order_quantity: Optional[int] = None
    jlcpcb_order_multiple: int = 1
    jlcpcb_currency: str = "USD"
    digikey_min_order_quantity: Optional[int] = None
    digikey_order_multiple: int = 1
    digikey_currency: str = "USD"
    
    is_basic: bool = False
    is_preferred: bool = False

    source: str = ""  # "JLCPCB", "DigiKey", etc.
    status: str = ""  # "Found", "Not Found", "Exact MPN Mismatch", "Insufficient Stock", etc.
    notes: str = ""  # Detailed notes/error messages
    jlcpcb_status: str = "not_searched"  # found, not_found, error, warning, not_searched
    jlcpcb_availability: str = "UNKNOWN"  # IN_STOCK, OUT_OF_STOCK, PREORDER, UNKNOWN
    jlcpcb_error: str = ""
    jlcpcb_source: str = ""
    digikey_status: str = "not_searched"
    digikey_error: str = ""
    digikey_source: str = ""
    skip_reason: str = ""  # "RES-coded component", etc.
    skip_jlcpcb: bool = False  # Set to True for RES/missing MPN to bypass JLCPCB search
    _generated_component_key: Optional[str] = field(
        default=None, repr=False, compare=False
    )
    supplier_changes: list[dict] = field(default_factory=list, repr=False)

    # Output column order for Excel export — matches required spec
    EXPORT_COLUMNS: ClassVar[list[tuple[str, str]]] = [
        ("Board Name", "board_name"),
        ("Description", "description"),
        ("Designator", "designator"),
        ("Quantity (Per Board / Total)", "quantity_formatted"),
        ("Value", "value"),
        ("Manufacturer", "manufacturer"),
        ("MPN", "mpn"),
        ("Design Item ID", "comment"),
        ("JLCPCB Part Number", "jlcpcb_part_number"),
        ("DigiKey Part Number", "digikey_part_number"),
        ("JLCPCB Stock", "available_stock_qty"),
        ("DigiKey Stock", "digikey_stock_qty"),
        ("JLCPCB Unit Price", "unit_price"),
        ("DigiKey Unit Price", "digikey_unit_price"),
        ("Production Required Quantity", "production_quantity_val"),
        ("Safety Surplus", "safety_surplus"),
        ("Purchase Quantity", "purchase_quantity_val"),
        ("JLCPCB Total Price", "jlcpcb_total_price"),
        ("DigiKey Total Price", "digikey_total_price"),
        ("Status", "status"),
    ]

    @property
    def production_quantity_val(self) -> int:
        if self.production_quantity is not None:
            try:
                from core.mpn_utils import parse_positive_integer_quantity
                return parse_positive_integer_quantity(self.production_quantity)
            except (ValueError, TypeError):
                pass
        try:
            from core.mpn_utils import parse_positive_integer_quantity
            return parse_positive_integer_quantity(self.quantity)
        except (ValueError, TypeError):
            return 1

    @property
    def purchase_quantity_val(self) -> int:
        if self.purchase_quantity is not None:
            try:
                from core.mpn_utils import parse_positive_integer_quantity
                return parse_positive_integer_quantity(self.purchase_quantity)
            except (ValueError, TypeError):
                pass
        return self.production_quantity_val + self.safety_surplus

    @property
    def quantity_formatted(self) -> str:
        # For raw row export, we just show the base quantity or a simple calculation if board_quantity was tracked.
        # Since BomItem doesn't inherently know board_quantity unless injected, we'll just format as 'qty / qty' to be consistent if possible, 
        # but actually for raw export, it's just the raw quantity string, so we'll just return str(self.quantity).
        return str(self.quantity)

    @property
    def combined_unit_price(self) -> str:
        j_price_str = f"{self.unit_price:.5f}" if self.unit_price is not None else "-"
        d_price_str = f"{self.digikey_unit_price:.5f}" if self.digikey_unit_price is not None else "-"
        return f"JLCPCB: {j_price_str} / DigiKey: {d_price_str}"

    @property
    def is_sufficient_stock(self) -> bool:
        if self.available_stock_qty is None:
            return False
        return self.available_stock_qty >= self.required_stock

    @property
    def is_available(self) -> bool:
        """True when at least one supplier returned a usable result."""
        return self.is_jlcpcb_usable or self.digikey_status == "found"

    @property
    def is_jlcpcb_preorder(self) -> bool:
        return self.jlcpcb_availability == "PREORDER" or self.jlcpcb_status == "preorder"

    @property
    def is_jlcpcb_usable(self) -> bool:
        return not self.is_jlcpcb_preorder and self.jlcpcb_status in ("found", "warning")

    @property
    def is_not_found(self) -> bool:
        """True only when every queried supplier returned not_found."""
        statuses = (self.jlcpcb_status, self.digikey_status)
        queried = [status for status in statuses if status != "not_searched"]
        return bool(queried) and all(status == "not_found" for status in queried)

    def refresh_status(self) -> str:
        """Derive the presentation status from independent supplier states."""
        if self.is_available:
            if self.jlcpcb_status == "warning":
                self.status = f"Warning [{self.jlcpcb_source or 'JLCPCB'}]: {self.notes}"
            else:
                self.status = ""
        else:
            errors = [
                f"JLCPCB API error: {self.jlcpcb_error}" if self.jlcpcb_status == "error" else "",
                f"DigiKey API error: {self.digikey_error}" if self.digikey_status == "error" else "",
            ]
            errors = [error for error in errors if error]
            if self.jlcpcb_status == "mismatch":
                if errors:
                    self.status = f"No exact JLCPCB match; {'; '.join(errors)}"
                else:
                    self.status = "No exact JLCPCB match"
            elif errors:
                self.status = "; ".join(errors)
            elif self.is_jlcpcb_preorder:
                self.status = "Pre-order — Needs Review"
            elif self.is_not_found:
                self.status = "Not Found"
            elif "Insufficient JLCPCB stock" in (self.status or ""):
                pass
            elif not self.status:
                self.status = "Not Found"
        return self.status

    def get_ui_category(self) -> str:
        """Return a single mutually exclusive category: 'available', 'not_found', 'mismatch', 'low_stock', 'error', 'manual'."""
        if self.is_available:
            return "available"
        if "Insufficient JLCPCB stock" in (self.status or ""):
            return "low_stock"
        if self.jlcpcb_status == "mismatch" or "No exact JLCPCB match" in (self.status or ""):
            return "mismatch"
        if self.is_not_found or self.status == "Not Found":
            return "not_found"
        if self.jlcpcb_status == "error" or self.digikey_status == "error" or "error" in (self.status or "").lower():
            return "error"
        return "manual"

    @classmethod
    def get_headers(cls) -> list[str]:
        return [h[0] for h in cls.EXPORT_COLUMNS]

    def to_row(self) -> list:
        row = []
        for header, attr in self.EXPORT_COLUMNS:
            if header == "Quantity (Per Board / Total)":
                row.append(self.quantity)
            else:
                val = getattr(self, attr, "")
                row.append(val if val is not None else "")
        return row
