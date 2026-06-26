import json
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routers.inventory as inventory_router
from app.routers.inventory import (
    InventoryItemInput,
    InventoryStructureInput,
    StockMovementInput,
    create_stock_movement,
    _item_row,
    _json_payload,
)
from tests.conftest import MemoryDB


def test_inventory_item_normalizes_core_master_data():
    item = InventoryItemInput(
        organisation_id="org-1",
        code="  WIDGET-01  ",
        name="  Blue   Widget ",
        item_type="stock_item",
        unit_of_measure="  Each ",
    )

    assert item.code == "WIDGET-01"
    assert item.name == "Blue Widget"
    assert item.unit_of_measure == "Each"
    assert item.standard_cost == Decimal("0")


def test_stock_movements_reject_zero_quantity():
    with pytest.raises(ValidationError, match="Quantity cannot be zero"):
        StockMovementInput(
            organisation_id="org-1",
            inventory_item_id="item-1",
            movement_type="adjustment",
            quantity=0,
        )


def test_inventory_structure_requires_at_least_one_component():
    with pytest.raises(ValidationError):
        InventoryStructureInput(
            organisation_id="org-1",
            output_item_id="item-1",
            structure_type="bom",
            name="Widget assembly",
            components=[],
        )


def test_inventory_write_payloads_are_json_safe_and_preserve_decimal_strings():
    item = InventoryItemInput(
        organisation_id="org-1",
        code="WIDGET-01",
        name="Blue Widget",
        item_type="stock_item",
        standard_cost=Decimal("100.25"),
        selling_price=Decimal("150.50"),
        reorder_point=Decimal("10.125"),
    )
    structure = InventoryStructureInput(
        organisation_id="org-1",
        output_item_id="item-1",
        structure_type="bom",
        name="Widget assembly",
        output_quantity=Decimal("2.5"),
        components=[{"component_item_id": "item-2", "quantity": "1.25", "wastage_percent": "3.5"}],
    )
    movement = StockMovementInput(
        organisation_id="org-1",
        inventory_item_id="item-1",
        movement_type="adjustment",
        quantity=Decimal("-1.75"),
        unit_cost=Decimal("100.25"),
    )

    item_row = _item_row(item, "user-1")
    structure_header = _json_payload(structure, exclude={"components"})
    component_row = _json_payload(structure.components[0])
    movement_row = _json_payload(movement)

    json.dumps([item_row, structure_header, component_row, movement_row])
    assert item_row["standard_cost"] == "100.25"
    assert structure_header["output_quantity"] == "2.5"
    assert component_row["quantity"] == "1.25"
    assert movement_row["quantity"] == "-1.75"


def test_stock_movement_blocks_locked_period(monkeypatch):
    db = MemoryDB({
        "inventory_items": [{
            "id": "item-1",
            "organisation_id": "org-1",
            "item_type": "stock_item",
        }],
        "organisation_accounting_periods": [{
            "organisation_id": "org-1",
            "status": "locked",
            "lock_date": "2026-05-31",
        }],
    })
    monkeypatch.setattr(inventory_router, "ensure_org_write", lambda *_args: None)

    with pytest.raises(HTTPException) as exc_info:
        create_stock_movement(
            StockMovementInput(
                organisation_id="org-1",
                inventory_item_id="item-1",
                movement_type="adjustment",
                quantity=Decimal("1"),
                occurred_on="2026-05-31",
            ),
            auth=("user-1", db),
        )

    assert exc_info.value.status_code == 400
    assert "accounting lock date" in exc_info.value.detail
