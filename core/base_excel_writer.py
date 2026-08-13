import re
import openpyxl
from typing import Dict, Any, Union
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

from models.bom_item import BomItem
from core.mpn_utils import select_unit_price, select_digikey_price

class BaseExcelWriter:
    """Base class providing shared logic for Excel writers (Project and Workspace)."""
    
    def __init__(self):
        self.wb = openpyxl.Workbook()
        if self.wb.sheetnames:
            self.wb.remove(self.wb.active)

        self.header_font = Font(bold=True)
        self.header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        self.fill_green = PatternFill(start_color="c8e6c9", end_color="c8e6c9", fill_type="solid")
        self.fill_yellow = PatternFill(start_color="fff9c4", end_color="fff9c4", fill_type="solid")
        self.fill_red = PatternFill(start_color="ffcdd2", end_color="ffcdd2", fill_type="solid")
        self.wrap_alignment = Alignment(vertical="center", wrap_text=True)

    def _fmt_qty(self, val: Union[int, float, str]) -> str:
        try:
            f = float(val)
            if f.is_integer():
                return str(int(f))
            return str(f)
        except (ValueError, TypeError):
            return str(val)

    def _fmt_qty_pair(self, per_board_qty: Union[int, float, str], total_qty: Union[int, float, str]) -> str:
        return f"{self._fmt_qty(per_board_qty)} / {self._fmt_qty(total_qty)}"

    def _format_usage_quantities(self, usages) -> str:
        return "\n".join(
            self._fmt_qty_pair(usage.bom_line_quantity, usage.total_quantity)
            for usage in usages
        )

    def _apply_narrow_columns(self, ws, header_row: int = 1):
        headers = [str(cell.value) for cell in ws[header_row]]
        narrow_targets = {
            "Quantity (Per Board / Total)": 18,
            "Quantity": 15,
            "Description": 35,
            "Designator": 25
        }
        for col_idx, header in enumerate(headers, 1):
            if header in narrow_targets:
                ws.column_dimensions[get_column_letter(col_idx)].width = narrow_targets[header]

    def _auto_fit_columns(self, ws, min_width: int = 8, max_width: int = 80, padding: int = 6) -> None:
        """Set worksheet column widths from the longest visible line in each column."""
        for col_idx in range(1, ws.max_column + 1):
            max_length = 0
            for row in ws.iter_rows(min_col=col_idx, max_col=col_idx):
                value = row[0].value
                if value is None:
                    continue

                lines = str(value).splitlines() or [""]
                max_length = max(max_length, *(len(line) for line in lines))

            if max_length:
                width = min(max(max_length + padding, min_width), max_width)
                ws.column_dimensions[get_column_letter(col_idx)].width = width

    def _sanitize_sheet_name(self, name: str) -> str:
        s = re.sub(r'[\\\\/?*\\[\\]:]', '', name).strip()
        return s[:31]

    def _safe_sheet_name(self, base_name: str, used_names: set[str]) -> str:
        """Generates a safe and unique sheet name from the base_name."""
        s = self._sanitize_sheet_name(base_name)
        if not s:
            s = "Sheet"
        sheet_name = s
        counter = 2
        while sheet_name in used_names:
            suffix = f" {counter}"
            sheet_name = s[:31 - len(suffix)] + suffix
            counter += 1
        used_names.add(sheet_name)
        return sheet_name

    def _format_currency(self, cell):
        cell.number_format = '"$"#,##0.0000'

    def _format_supplier_price_cells(self, ws, row: int, jlc_col: int, digikey_col: int, item: BomItem) -> None:
        """Format separate supplier prices and highlight the cheaper option."""
        jlc_cell = ws.cell(row=row, column=jlc_col)
        digikey_cell = ws.cell(row=row, column=digikey_col)
        for cell in (jlc_cell, digikey_cell):
            if isinstance(cell.value, (int, float)):
                self._format_currency(cell)
        if item.unit_price is not None and item.digikey_unit_price is not None:
            if item.unit_price < item.digikey_unit_price:
                jlc_cell.fill, digikey_cell.fill = self.fill_green, self.fill_red
            elif item.digikey_unit_price < item.unit_price:
                jlc_cell.fill, digikey_cell.fill = self.fill_red, self.fill_green
        elif item.unit_price is not None:
            jlc_cell.fill = self.fill_green
        elif item.digikey_unit_price is not None:
            digikey_cell.fill = self.fill_green

    def _add_supplier_stock_sheet(self, items: list[BomItem]) -> None:
        """Write one JLCPCB row and one DigiKey row per component."""
        sheet_name = "Supplier Stock"
        if sheet_name in self.wb.sheetnames:
            del self.wb[sheet_name]
        ws = self.wb.create_sheet(sheet_name)
        headers = [
            "MPN", "Design Item ID", "Supplier", "Supplier Part Number",
            "Stock", "Unit Price", "Required Stock", "Status",
        ]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = self.header_font
            cell.alignment = self.header_alignment

        for item in items:
            supplier_rows = [
                (
                    "JLCPCB",
                    item.jlcpcb_part_number,
                    item.available_stock_qty,
                    item.unit_price,
                    item.status,
                ),
                (
                    "DigiKey",
                    item.digikey_part_number,
                    item.digikey_stock_qty,
                    item.digikey_unit_price,
                    "Found" if item.digikey_part_number else "Not Found",
                ),
            ]
            for supplier, part_number, stock, unit_price, status in supplier_rows:
                ws.append([
                    item.mpn,
                    item.comment,
                    supplier,
                    part_number or "-",
                    stock if stock is not None else "-",
                    unit_price,
                    item.required_stock,
                    status or "",
                ])
                if isinstance(unit_price, (int, float)):
                    self._format_currency(ws.cell(row=ws.max_row, column=6))

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"
        self._auto_fit_columns(ws, max_width=40)
        ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 22)
        ws.column_dimensions["D"].width = max(ws.column_dimensions["D"].width or 0, 24)

    def _get_status_fill(self, status_text: str, has_jlcpcb_part: bool = False):
        """Returns the appropriate PatternFill based on status text and JLCPCB part presence."""
        val = str(status_text or "")
        status_lower = val.lower()
        
        # Failure checks must happen before success checks
        if "not found" in status_lower or "error" in status_lower or "no exact" in status_lower or "insufficient" in status_lower:
            return self.fill_red
            
        # Success checks
        if "✅" in val or "found" in status_lower or (val == "" and has_jlcpcb_part):
            return self.fill_green
            
        # Warning/Unknown checks
        if val != "":
            return self.fill_yellow
            
        return None

    def _is_jlcpcb_usable(self, item: BomItem) -> bool:
        """Determines if JLCPCB data can be used based on component status and flags."""
        if not item.jlcpcb_part_number:
            return False
        if getattr(item, 'skip_jlcpcb', False):
            return False
            
        status_lower = str(item.status or "").lower()
        if "not found" in status_lower:
            return False
        if "no exact" in status_lower:
            return False
        if "insufficient" in status_lower:
            return False
        if "error" in status_lower:
            return False
        
        return True

    def _get_pricing_for_component_item(self, item: BomItem, multiplied_qty: Union[int, float]) -> Dict[str, Any]:
        """Calculates JLCPCB, Remaining DigiKey, and DigiKey-Only prices/costs for a single multiplier."""
        j_price = None
        if self._is_jlcpcb_usable(item):
            j_price = select_unit_price(item.jlcpcb_price_breaks_raw, multiplied_qty)
            
        d_price = select_digikey_price(item.digikey_price_breaks, multiplied_qty)

        j_cost = None
        rem_dk_cost = None
        all_dk_cost = None
        selected_price = None
        selected_source = "Unpriced"
        combined_cost = None

        if d_price is not None:
            all_dk_cost = multiplied_qty * d_price

        if j_price is not None:
            selected_source = "JLCPCB"
            selected_price = j_price
            j_cost = multiplied_qty * j_price
            combined_cost = j_cost
        elif d_price is not None:
            selected_source = "Remaining DigiKey"
            selected_price = d_price
            rem_dk_cost = multiplied_qty * d_price
            combined_cost = rem_dk_cost

        return {
            "selected_source": selected_source,
            "selected_price": selected_price,
            "jlcpcb_cost": j_cost,
            "remaining_digikey_cost": rem_dk_cost,
            "combined_cost": combined_cost,
            "digikey_only_cost": all_dk_cost,
            "j_price": j_price,
            "d_price": d_price
        }
