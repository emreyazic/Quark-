import datetime
from typing import List, Dict, Any, Union

from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from models.bom_item import BomItem
from models.workspace import Workspace
from services.project_aggregation import WorkspaceAggregationResult, WorkspaceAggregatedComponent
from core.base_excel_writer import BaseExcelWriter


class WorkspaceExcelWriter(BaseExcelWriter):
    """Writes a comprehensive workspace-aware multi-project BOM cost report to Excel."""

    def __init__(
        self,
        workspace: Workspace,
        aggregation_result: WorkspaceAggregationResult,
        enriched_items: List[BomItem],
        component_keys: List[str],
        build_multipliers: List[int] = None
    ):
        BaseExcelWriter.__init__(self)
        
        # 1. Validation: Length match
        if len(enriched_items) != len(component_keys):
            raise ValueError(f"Length mismatch: enriched_items({len(enriched_items)}) != component_keys({len(component_keys)})")

        # 2. Validation: Duplicates
        if len(set(component_keys)) != len(component_keys):
            raise ValueError("Duplicate component keys found in component_keys list.")

        # 3. Validation: Exact match
        agg_keys = {c.component_key for c in aggregation_result.components}
        prov_keys = set(component_keys)
        if agg_keys != prov_keys:
            missing = agg_keys - prov_keys
            extra = prov_keys - agg_keys
            raise ValueError(f"Component key mismatch. Missing: {missing}, Extra: {extra}")

        self.workspace = workspace
        self.aggregation_result = aggregation_result
        self.enriched_items = enriched_items
        self.component_keys = component_keys
        self.build_multipliers = build_multipliers or [1, 5, 10, 50, 100]

        # 4. Dictionary Mapping
        self.enriched_by_key: Dict[str, BomItem] = dict(zip(component_keys, enriched_items))
        
        self.component_by_key: Dict[str, WorkspaceAggregatedComponent] = {
            c.component_key: c for c in self.aggregation_result.components
        }
        
        self.item_id_to_key = {
            id(usage.item): comp.component_key
            for comp in self.aggregation_result.components
            for usage in comp.usages
        }

    def _get_pricing(self, comp_key: str, multiplied_qty: Union[int, float]) -> Dict[str, Any]:
        item = self.enriched_by_key[comp_key]
        return self._get_pricing_for_component_item(item, multiplied_qty)

    def write(self, output_path: str):
        used_names = set()
        
        self._add_aggregated_components_sheet(used_names)
        self._add_mutual_components_sheet(used_names)
        self._add_project_sheets(used_names)
        
        self.wb.save(output_path)

    def _add_aggregated_components_sheet(self, used_names: set[str]):
        sheet_name = self._safe_sheet_name("All Aggregated", used_names)
        ws = self.wb.create_sheet(sheet_name)
        
        headers = [
            "Board Name", "Description", "Designator", "Quantity (Per Board / Total)", 
            "Value", "Manufacturer", "MPN", "Design Item ID", "JLCPCB Part Number", 
            "Unit Price (JLCPCB / DigiKey)", "Status"
        ]

        ws.append(headers)
        for cell in ws[1]:
            cell.font = self.header_font
            cell.alignment = self.header_alignment
            
        for comp_key, comp in self.component_by_key.items():
            enriched = self.enriched_by_key[comp_key]
            
            for u in comp.usages:
                board_str = u.board_name
                desigs_str = str(u.item.designator) if u.item.designator else ""
                qty_str = self._fmt_qty_pair(u.bom_line_quantity, u.total_quantity)
                
                row = [
                    board_str,
                    enriched.description,
                    desigs_str,
                    qty_str,
                    enriched.value,
                    enriched.manufacturer,
                    enriched.mpn,
                    enriched.comment,
                    enriched.jlcpcb_part_number,
                    enriched.combined_unit_price,
                    enriched.status
                ]
                    
                ws.append(row)
                ws.cell(row=ws.max_row, column=1).alignment = self.wrap_alignment
                ws.cell(row=ws.max_row, column=4).alignment = self.wrap_alignment
                
                cell = ws.cell(row=ws.max_row, column=11)
                fill = self._get_status_fill(enriched.status, bool(enriched.jlcpcb_part_number))
                if fill:
                    cell.fill = fill

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"
        self._auto_fit_columns(ws)
        self._apply_narrow_columns(ws)

    def _add_mutual_components_sheet(self, used_names: set[str]):
        sheet_name = self._safe_sheet_name("Mutual Components", used_names)
        ws = self.wb.create_sheet(sheet_name)
        
        headers = [
            "Board Names", "Description", "Designator", "Quantity (Per Board / Total)", 
            "Value", "Manufacturer", "MPN", "Design Item ID", "JLCPCB Part Number", 
            "Unit Price (JLCPCB / DigiKey)", "Status"
        ]

        ws.append(headers)
        for cell in ws[1]:
            cell.font = self.header_font
            cell.alignment = self.header_alignment
            
        for mutual in self.aggregation_result.mutual_components:
            comp_key = mutual.component_key
            enriched = self.enriched_by_key[comp_key]
            
            board_str = "\n".join([u.board_name for u in mutual.usages])
            qty_str = self._format_usage_quantities(mutual.usages)
            
            row = [
                board_str,
                enriched.description,
                "Mutual", # Designators can be huge for mutual, so we just say Mutual
                qty_str,
                enriched.value,
                enriched.manufacturer,
                enriched.mpn,
                enriched.comment,
                enriched.jlcpcb_part_number,
                enriched.combined_unit_price,
                enriched.status
            ]
                
            ws.append(row)
            ws.cell(row=ws.max_row, column=1).alignment = self.wrap_alignment
            ws.cell(row=ws.max_row, column=4).alignment = self.wrap_alignment
            
            cell = ws.cell(row=ws.max_row, column=11)
            fill = self._get_status_fill(enriched.status, bool(enriched.jlcpcb_part_number))
            if fill:
                cell.fill = fill

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"
        self._auto_fit_columns(ws)
        self._apply_narrow_columns(ws)

    def _add_project_sheets(self, used_names: set[str]):
        for project_name, proj_result in self.aggregation_result.project_results.items():
            base_name = f"P {project_name}"
            sheet_name = self._safe_sheet_name(base_name, used_names)
            ws = self.wb.create_sheet(sheet_name)
            
            project_obj = self.workspace.get_project(project_name)
            
            # Count mutual components appearing in this project
            mutual_count = sum(1 for m in self.aggregation_result.mutual_components if project_name in self.component_by_key[m.component_key].source_projects)
            
            ws.append(["Project Name:", project_name])
            ws.append(["Number of Boards:", len(project_obj.board_items) if project_obj else 0])
            ws.append(["Unique Components in Project:", len(proj_result.components)])
            ws.append(["Mutual Workspace Components in Project:", mutual_count])
            ws.append([])
            
            for row_idx in range(1, 5):
                ws.cell(row=row_idx, column=1).font = self.header_font

            # Cost Summary
            ws.append(["Project Cost Summary"])
            ws[f"A{ws.max_row}"].font = Font(bold=True, size=14)
            ws.append(["Build Qty", "JLCPCB Cost", "Remaining DigiKey Cost", "Combined Total", "DigiKey-Only Total"])
            for cell in ws[ws.max_row]:
                cell.font = self.header_font
                cell.alignment = self.header_alignment

            for m in self.build_multipliers:
                tot_jlc = 0.0
                tot_rem_dk = 0.0
                tot_comb = 0.0
                tot_dk_only = 0.0
                
                for proj_comp in proj_result.components:
                    comp_key = proj_comp.component_key
                    # We price based on the project's specific localized sub-quantity to get accurate breakpoints
                    mult_qty = proj_comp.total_quantity * m
                    pricing = self._get_pricing(comp_key, mult_qty)
                    
                    if pricing["jlcpcb_cost"] is not None: tot_jlc += pricing["jlcpcb_cost"]
                    if pricing["remaining_digikey_cost"] is not None: tot_rem_dk += pricing["remaining_digikey_cost"]
                    if pricing["combined_cost"] is not None: tot_comb += pricing["combined_cost"]
                    if pricing["digikey_only_cost"] is not None: tot_dk_only += pricing["digikey_only_cost"]

                row = [f"{m}x", tot_jlc, tot_rem_dk, tot_comb, tot_dk_only]
                ws.append(row)
                for c_idx in range(2, 6):
                    self._format_currency(ws.cell(row=ws.max_row, column=c_idx))

            ws.append([])
            
            # Component Table
            # Component Table
            headers = [
                "Board Name", "Description", "Designator", "Quantity (Per Board / Total)", 
                "Value", "Manufacturer", "MPN", "Design Item ID", "JLCPCB Part Number", 
                "Unit Price (JLCPCB / DigiKey)", "Status"
            ]
                
            ws.append(headers)
            header_row = ws.max_row
            for cell in ws[header_row]:
                cell.font = self.header_font
                cell.alignment = self.header_alignment
                
            for proj_comp in proj_result.components:
                comp_key = proj_comp.component_key
                enriched = self.enriched_by_key[comp_key]
                
                for u in proj_comp.usages:
                    board_str = u.board_name
                    desigs_str = str(u.bom_item.designator) if u.bom_item.designator else ""
                    qty_str = self._fmt_qty_pair(u.bom_line_quantity, u.total_quantity)
                    
                    row = [
                        board_str,
                        enriched.description,
                        desigs_str,
                        qty_str,
                        enriched.value,
                        enriched.manufacturer,
                        enriched.mpn,
                        enriched.comment,
                        enriched.jlcpcb_part_number,
                        enriched.combined_unit_price,
                        enriched.status
                    ]
                        
                    ws.append(row)
                    ws.cell(row=ws.max_row, column=1).alignment = self.wrap_alignment
                    ws.cell(row=ws.max_row, column=4).alignment = self.wrap_alignment
                    
                    cell = ws.cell(row=ws.max_row, column=11)
                    fill = self._get_status_fill(enriched.status, bool(enriched.jlcpcb_part_number))
                    if fill:
                        cell.fill = fill
                        
            ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{ws.max_row}"
            ws.freeze_panes = f"A{header_row + 1}"
            self._auto_fit_columns(ws)
            self._apply_narrow_columns(ws, header_row=header_row)
