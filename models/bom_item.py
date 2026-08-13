from dataclasses import dataclass, field
from typing import ClassVar, Optional


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

    def is_valid(self) -> bool:
        """Mapping is valid only if MPN and Quantity are mapped."""
        return self.mpn is not None and self.quantity is not None


@dataclass
class BomFile:
    """Represents a loaded Excel file and its detection state."""

    file_path: str
    board_name: str
    sheet_name: str = ""
    headers: list[str] = field(default_factory=list)
    preview_rows: list[list] = field(default_factory=list)
    column_mapping: Optional[ColumnMapping] = None
    row_count: int = 0
    is_valid: bool = True
    error_message: str = ""


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
    quantity: int = 0
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
    required_stock: int = 0  # Computed as (quantity * 10) + 10
    unit_price: Optional[float] = None
    digikey_unit_price: Optional[float] = None
    digikey_stock_qty: Optional[int] = None
    digikey_part_number: str = ""
    
    jlcpcb_price_breaks_raw: str = ""
    digikey_price_breaks: list[tuple[int, float]] = field(default_factory=list)
    
    is_basic: bool = False
    is_preferred: bool = False

    source: str = ""  # "JLCPCB", "DigiKey", etc.
    status: str = ""  # "Found", "Not Found", "Exact MPN Mismatch", "Insufficient Stock", etc.
    notes: str = ""  # Detailed notes/error messages
    skip_reason: str = ""  # "RES-coded component", etc.
    skip_jlcpcb: bool = False  # Set to True for RES/missing MPN to bypass JLCPCB search

    # Output column order for Excel export — matches required spec
    EXPORT_COLUMNS: ClassVar[list[tuple[str, str]]] = [
        ("Board Name", "board_name"),
        ("Description", "description"),
        ("Designator", "designator"),
        ("Quantity (Per Board / Total)", "quantity_formatted"), # We can use a property for this if needed, but for raw it's just 'quantity'
        ("Value", "value"),
        ("Manufacturer", "manufacturer"),
        ("MPN", "mpn"),
        ("Design Item ID", "comment"),
        ("JLCPCB Part Number", "jlcpcb_part_number"),
        ("JLCPCB Stock", "available_stock_qty"),
        ("DigiKey Stock", "digikey_stock_qty"),
        ("JLCPCB Unit Price", "unit_price"),
        ("DigiKey Unit Price", "digikey_unit_price"),
        ("Status", "status"),
    ]

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
