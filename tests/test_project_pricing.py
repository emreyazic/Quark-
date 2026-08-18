import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QLabel, QTabWidget

from models.bom_item import BomItem
from models.project import Project, ProjectItem
from services.project_aggregation import aggregate_project
from services.project_pricing import calculate_project_pricing
from ui.main_window import MainWindow


def _aggregation(*items):
    project = Project("P")
    board = ProjectItem("bom.xlsx", "Board")
    board.bom_items = list(items)
    project.add_board(board)
    return aggregate_project(project)


def _priced_item(mpn="X", quantity=2, description="IC", **kwargs):
    defaults = dict(
        mpn=mpn,
        quantity=quantity,
        description=description,
        jlcpcb_part_number="C1",
        available_stock_qty=100000,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": 1.0}]',
        jlcpcb_status="found",
        digikey_status="not_found",
    )
    defaults.update(kwargs)
    return BomItem(**defaults)


def _calculate(aggregation, enriched, scenarios=(1, 10, 100, 1000)):
    keys = [component.component_key for component in aggregation.components]
    return calculate_project_pricing(aggregation, enriched, keys, scenarios)


def test_scenarios_are_independent_and_do_not_carry_excess_stock():
    aggregate = _aggregation(_priced_item(quantity=2))
    item = _priced_item(
        quantity=2,
        jlcpcb_min_order_quantity=50,
        jlcpcb_order_multiple=25,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "qTo": 49, "price": 5}, {"qFrom": 50, "price": 0.02}]',
    )
    scenarios = _calculate(aggregate, [item])

    assert [result.components[0].required_quantity for result in scenarios] == [2, 20, 200, 2000]
    assert [result.components[0].quote.purchase_quantity for result in scenarios] == [50, 50, 200, 2000]
    assert scenarios[0].components[0].quote.excess_stock_quantity == 48
    assert scenarios[1].components[0].quote.excess_stock_quantity == 30
    assert scenarios[0].components[0].quote.unit_price == Decimal("0.02")


def test_aggregation_precedes_single_resistor_surplus_and_other_parts_have_none():
    aggregate = _aggregation(
        _priced_item(mpn="R-1", quantity=2, description="Resistor 10k"),
        _priced_item(mpn="R-1", quantity=3, description="Resistor 10k"),
        _priced_item(mpn="IC-1", quantity=4, description="Microcontroller"),
    )
    enriched = [
        _priced_item(mpn=component.representative_item.mpn, description=component.representative_item.description)
        for component in aggregate.components
    ]
    scenario = _calculate(aggregate, enriched, (1,))[0]
    rows = {row.mpn: row for row in scenario.components}

    assert rows["R-1"].quantity_per_project == 5
    assert rows["R-1"].safety_surplus == 10
    assert rows["R-1"].target_quantity == 15
    assert rows["IC-1"].safety_surplus == 0


def test_moq_multiple_purchase_tier_and_cost_formulas_use_decimal():
    aggregate = _aggregation(_priced_item(quantity=10))
    item = _priced_item(
        quantity=10,
        jlcpcb_min_order_quantity=50,
        jlcpcb_order_multiple=25,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "qTo": 49, "price": 2}, {"qFrom": 50, "price": 0.02}]',
    )
    row = _calculate(aggregate, [item], (1,))[0].components[0]
    quote = row.quote

    assert quote.minimum_order_quantity == 50
    assert quote.purchase_quantity == 50
    assert quote.unit_price == Decimal("0.02")
    assert quote.order_price == Decimal("1.00")
    assert quote.price_for_quantity == Decimal("0.20")
    assert quote.excess_stock_quantity == 40
    assert quote.excess_stock_cost == Decimal("0.80")


def test_supplier_is_reselected_per_scenario_by_order_price():
    aggregate = _aggregation(_priced_item(quantity=1))
    item = _priced_item(
        quantity=1,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "qTo": 99, "price": 1}, {"qFrom": 100, "price": 0.1}]',
        digikey_part_number="DK1",
        digikey_stock_qty=100000,
        digikey_price_breaks=[(1, 0.5)],
        digikey_status="found",
    )
    one, hundred = _calculate(aggregate, [item], (1, 100))

    assert one.components[0].quote.supplier == "DigiKey"
    assert hundred.components[0].quote.supplier == "JLCPCB"


def test_insufficient_or_error_supplier_does_not_block_valid_other_supplier():
    aggregate = _aggregation(_priced_item(quantity=10))
    item = _priced_item(
        quantity=10,
        available_stock_qty=5,
        jlcpcb_status="found",
        digikey_part_number="DK1",
        digikey_stock_qty=100,
        digikey_price_breaks=[(1, 2.0)],
        digikey_status="found",
    )
    row = _calculate(aggregate, [item], (1,))[0].components[0]
    assert row.quote.supplier == "DigiKey"
    assert any("insufficient stock" in reason for reason in row.supplier_reasons)

    item.jlcpcb_part_number = ""
    item.jlcpcb_status = "error"
    item.jlcpcb_error = "timeout"
    row = _calculate(aggregate, [item], (1,))[0].components[0]
    assert row.quote.supplier == "DigiKey"
    assert "API error" in row.supplier_reasons[0]


def test_unpriced_and_invalid_breaks_are_reported_and_excluded():
    aggregate = _aggregation(_priced_item(quantity=2))
    item = _priced_item(
        quantity=2,
        jlcpcb_price_breaks_raw='[{"qFrom": 1, "price": NaN}, {"qFrom": 2, "price": -1}, {"qFrom": 3, "qTo": 2, "price": 1}]',
        digikey_part_number="DK1",
        digikey_stock_qty=100,
        digikey_price_breaks=[(1, float("inf")), (2, -1)],
        digikey_status="found",
    )
    result = _calculate(aggregate, [item], (1,))[0]

    assert result.components[0].quote is None
    assert result.components[0].status.startswith("Unpriced:")
    assert result.priced_count == 0
    assert result.unpriced_count == 1
    assert result.cost_incomplete
    assert result.order_price_totals == {}


def test_currencies_are_kept_separate_and_per_project_totals_are_safe():
    aggregate = _aggregation(_priced_item(mpn="A", quantity=1), _priced_item(mpn="B", quantity=1))
    items = [
        _priced_item(mpn="A", quantity=1, jlcpcb_currency="USD"),
        _priced_item(mpn="B", quantity=1, jlcpcb_currency="EUR"),
    ]
    result = _calculate(aggregate, items, (10,))[0]

    assert result.order_price_totals == {"USD": Decimal("10.0"), "EUR": Decimal("10.0")}
    assert result.per_project_order_totals == {"USD": Decimal("1.0"), "EUR": Decimal("1.0")}


def test_supplier_quotes_in_different_currencies_are_not_compared_without_fx():
    aggregate = _aggregation(_priced_item(quantity=1))
    item = _priced_item(
        quantity=1,
        jlcpcb_currency="EUR",
        digikey_part_number="DK1",
        digikey_stock_qty=100,
        digikey_price_breaks=[(1, 0.5)],
        digikey_currency="USD",
        digikey_status="found",
    )

    result = _calculate(aggregate, [item], (1,))[0]

    assert result.components[0].quote is None
    assert "cannot be compared" in result.components[0].status
    assert result.cost_incomplete


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_ui_summary_and_table_retain_the_same_scenario_result(qapp):
    aggregate = _aggregation(_priced_item(quantity=2))
    item = _priced_item(quantity=2)
    state = SimpleNamespace(
        _pricing_scenario_tabs=QTabWidget(),
        _pricing_views={},
        _pricing_empty_label=QLabel(),
        _workspace_aggregation_result=aggregate,
        _project_aggregation_result=None,
        _search_item_component_keys=[aggregate.components[0].component_key],
        _all_items=[item],
        _format_currency_totals=MainWindow._format_currency_totals,
    )

    MainWindow._populate_project_pricing(state)

    summary, table, scenario = state._pricing_views[1]
    assert table.rowCount() == len(scenario.components)
    assert str(scenario.order_price_totals["USD"]) in summary.text()
    assert table.item(0, 13).text().endswith(str(scenario.components[0].quote.order_price))
