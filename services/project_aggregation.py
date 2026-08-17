from dataclasses import dataclass, field
from typing import Optional, Union

import copy
from models.bom_item import BomItem
from models.project import Project
from models.workspace import Workspace
from core.mpn_utils import normalize_mpn
from core.mpn_utils import parse_positive_integer_quantity


@dataclass
class BoardComponentUsage:
    board_name: str
    board_file_path: str
    board_quantity: int
    bom_line_quantity: Union[int, float]
    total_quantity: Union[int, float]
    bom_item: BomItem


@dataclass
class AggregatedComponent:
    component_key: str
    representative_item: BomItem
    total_quantity: Union[int, float]
    usages: list[BoardComponentUsage] = field(default_factory=list)


@dataclass
class ProjectAggregationResult:
    project_name: str
    components: list[AggregatedComponent] = field(default_factory=list)
    skipped_count: int = 0
    warnings: list[str] = field(default_factory=list)


def build_component_key(item: BomItem) -> str:
    """
    Builds a stable string to group identical components.
    Prioritizes strong part numbers and avoids grouping by value alone.
    """
    # Helper to normalize strings
    def norm(val) -> str:
        if val is None:
            return ""
        return str(val).strip().upper()

    mpn = normalize_mpn(item.mpn).upper()
    jlc = norm(item.jlcpcb_part_number)
    
    # 1. MPN is the product identity. Manufacturer text is deliberately not
    # part of this key: the same exact MPN cannot represent a second product
    # merely because one BOM says "KEMET", another says "Kemet Electronics",
    # or leaves the manufacturer blank.
    if mpn:
        return f"MPN:{mpn}"
        
    # 2. JLCPCB / LCSC Part Number
    if jlc:
        return f"JLC:{jlc}"
        
    # 3. DigiKey / supplier part number (if we had it in BomItem, but it's not currently stored there as a source field)
    # If the project had one, we would use it. We'll skip for now.
    
    val = norm(item.value)
    pkg = norm(item.footprint)
    cmt = norm(item.comment)
    desc = norm(item.description)

    # 4. Value + Footprint + Package (using footprint as package here if they are the same)
    # 5. Value + Footprint + Comment
    # We require BOTH value and footprint/package to use this fallback.
    # Comment can be included as extra detail, but comment alone should not replace footprint/package.
    if val and pkg:
        return f"VAL:{val}_PKG:{pkg}_CMT:{cmt}"
        
    # 6. Description fallback only if nothing else exists
    if desc:
        return f"DESC:{desc}"
        
    # Absolute fallback to prevent grouping completely empty rows
    if item._generated_component_key:
        return item._generated_component_key
        
    import uuid
    new_key = f"UNKNOWN:{uuid.uuid4().hex}"
    item._generated_component_key = new_key
    return new_key


def aggregate_project(project: Project) -> ProjectAggregationResult:
    """
    Aggregates a Project by multiplying component quantities by board quantities
    and grouping identical components across boards.
    """
    result = ProjectAggregationResult(project_name=project.project_name)
    components_map: dict[str, AggregatedComponent] = {}
    
    for board in project.board_items:
        if not board.bom_items:
            result.warnings.append(f"Board '{board.board_name}' has no parsed BOM items.")
            continue
            
        board_qty = board.board_quantity
        
        for item in board.bom_items:
            # Parse line quantity
            try:
                qty_val = parse_positive_integer_quantity(item.quantity)
                    
            except (ValueError, TypeError) as e:
                result.skipped_count += 1
                result.warnings.append(
                    f"Skipped {item.designator} on '{board.board_name}': Invalid quantity ({item.quantity})"
                )
                continue
                
            line_total = qty_val * board_qty
            key = build_component_key(item)
            usage_board_name = item.board_name.strip() if item.board_name else board.board_name
            
            usage = BoardComponentUsage(
                board_name=usage_board_name,
                board_file_path=board.file_path,
                board_quantity=board_qty,
                bom_line_quantity=qty_val,
                total_quantity=line_total,
                bom_item=item
            )
            
            if key not in components_map:
                components_map[key] = AggregatedComponent(
                    component_key=key,
                    representative_item=copy.deepcopy(item),
                    total_quantity=0,
                    usages=[]
                )
                
            components_map[key].total_quantity += line_total
            components_map[key].usages.append(usage)
            
    result.components = sorted(list(components_map.values()), key=lambda c: c.component_key)
    return result


def calculate_build_quantities(component: AggregatedComponent, multipliers: Optional[list[int]] = None) -> dict[int, Union[int, float]]:
    """Calculates project-level quantities for multiple build batches."""
    if multipliers is None:
        multipliers = [1, 5, 10, 50, 100]
        
    return {m: component.total_quantity * m for m in multipliers}


@dataclass
class WorkspaceBoardComponentUsage:
    project_name: str
    board_name: str
    board_file_path: str
    board_quantity: int
    bom_line_quantity: Union[int, float]
    total_quantity: Union[int, float]
    item: BomItem


@dataclass
class WorkspaceAggregatedComponent:
    component_key: str
    representative_item: BomItem
    total_quantity: Union[int, float]
    usages: list[WorkspaceBoardComponentUsage] = field(default_factory=list)
    source_projects: list[str] = field(default_factory=list)
    source_locations: list[str] = field(default_factory=list)
    source_board_names: list[str] = field(default_factory=list)
    _temp_projects: set[str] = field(default_factory=set, repr=False)
    _temp_locations: set[str] = field(default_factory=set, repr=False)
    _temp_board_names: set[str] = field(default_factory=set, repr=False)


@dataclass
class WorkspaceMutualComponent:
    component_key: str
    representative_item: BomItem
    total_quantity: Union[int, float]
    usages: list[WorkspaceBoardComponentUsage] = field(default_factory=list)
    project_count: int = 0
    source_location_count: int = 0
    shared_across_projects: bool = False


@dataclass
class WorkspaceAggregationResult:
    workspace_name: str
    components: list[WorkspaceAggregatedComponent] = field(default_factory=list)
    mutual_components: list[WorkspaceMutualComponent] = field(default_factory=list)
    skipped_count: int = 0
    warnings: list[str] = field(default_factory=list)
    project_results: dict[str, ProjectAggregationResult] = field(default_factory=dict)


def aggregate_workspace(workspace: Workspace) -> WorkspaceAggregationResult:
    """
    Aggregates all components across a full Workspace, identifying mutual components 
    and preserving specific source locations.
    """
    result = WorkspaceAggregationResult(workspace_name=workspace.workspace_name)
    components_map: dict[str, WorkspaceAggregatedComponent] = {}
    
    for project in workspace.projects:
        proj_result = aggregate_project(project)
        result.project_results[project.project_name] = proj_result
        
        for board in project.board_items:
            if not board.bom_items:
                continue
                
            board_qty = board.board_quantity
            
            for item in board.bom_items:
                try:
                    qty_val = parse_positive_integer_quantity(item.quantity)
                        
                except (ValueError, TypeError) as e:
                    result.skipped_count += 1
                    result.warnings.append(
                        f"Skipped {item.designator} on '{project.project_name}/{board.board_name}': Invalid quantity ({item.quantity})"
                    )
                    continue
                    
                line_total = qty_val * board_qty
                key = build_component_key(item)
                usage_board_name = item.board_name.strip() if item.board_name else board.board_name
                
                usage = WorkspaceBoardComponentUsage(
                    project_name=project.project_name,
                    board_name=usage_board_name,
                    board_file_path=board.file_path,
                    board_quantity=board_qty,
                    bom_line_quantity=qty_val,
                    total_quantity=line_total,
                    item=item
                )
                
                if key not in components_map:
                    components_map[key] = WorkspaceAggregatedComponent(
                        component_key=key,
                        representative_item=copy.deepcopy(item),
                        total_quantity=0,
                        usages=[]
                    )
                    
                comp = components_map[key]
                comp.total_quantity += line_total
                comp.usages.append(usage)
                
                comp._temp_projects.add(project.project_name)
                source_location = f"{project.project_name}::{board.file_path}::{usage_board_name}"
                comp._temp_locations.add(source_location)
                comp._temp_board_names.add(usage_board_name)

    sorted_keys = sorted(components_map.keys())
    
    for key in sorted_keys:
        comp = components_map[key]
        comp.source_projects = sorted(list(comp._temp_projects))
        comp.source_locations = sorted(list(comp._temp_locations))
        comp.source_board_names = sorted(list(comp._temp_board_names))
        
        result.components.append(comp)
        
        if len(comp.source_locations) > 1:
            mutual = WorkspaceMutualComponent(
                component_key=comp.component_key,
                representative_item=comp.representative_item,
                total_quantity=comp.total_quantity,
                usages=comp.usages,
                project_count=len(comp.source_projects),
                source_location_count=len(comp.source_locations),
                shared_across_projects=(len(comp.source_projects) > 1)
            )
            result.mutual_components.append(mutual)
            
    result.components.sort(key=lambda c: c.component_key)
    result.mutual_components.sort(key=lambda c: c.component_key)
    return result
