from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.accounting_locks import assert_accounting_period_unlocked


router = APIRouter(prefix="/api/inventory", tags=["inventory"])

InventoryItemType = Literal["stock_item", "service"]
StructureType = Literal["bom", "recipe"]
MovementType = Literal["opening_balance", "purchase", "sale", "adjustment", "production", "consumption"]


class InventoryItemInput(BaseModel):
    organisation_id: str
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=250)
    item_type: InventoryItemType
    description: Optional[str] = Field(default=None, max_length=4000)
    unit_of_measure: str = Field(default="Each", min_length=1, max_length=50)
    standard_cost: Decimal = Field(default=Decimal("0"), ge=0)
    selling_price: Decimal = Field(default=Decimal("0"), ge=0)
    reorder_point: Decimal = Field(default=Decimal("0"), ge=0)
    vat_treatment: Literal["standard", "zero_rated", "exempt"] = "standard"
    active: bool = True

    @field_validator("code", "name", "unit_of_measure")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        clean = " ".join(value.split())
        if not clean:
            raise ValueError("This field is required")
        return clean


class StructureLineInput(BaseModel):
    component_item_id: str
    quantity: Decimal = Field(gt=0)
    wastage_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class InventoryStructureInput(BaseModel):
    organisation_id: str
    output_item_id: str
    structure_type: StructureType
    name: str = Field(min_length=1, max_length=250)
    output_quantity: Decimal = Field(default=Decimal("1"), gt=0)
    active: bool = True
    notes: Optional[str] = Field(default=None, max_length=4000)
    components: list[StructureLineInput] = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        clean = " ".join(value.split())
        if not clean:
            raise ValueError("Name is required")
        return clean


class StockMovementInput(BaseModel):
    organisation_id: str
    inventory_item_id: str
    movement_type: MovementType
    quantity: Decimal
    unit_cost: Optional[Decimal] = Field(default=None, ge=0)
    occurred_on: date = Field(default_factory=date.today)
    reference: Optional[str] = Field(default=None, max_length=250)
    notes: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("quantity")
    @classmethod
    def reject_zero_quantity(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("Quantity cannot be zero")
        return value


def _one(result, detail: str) -> dict:
    if not result.data:
        raise HTTPException(status_code=404, detail=detail)
    return result.data[0]


def _json_payload(payload: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Return a JSON-safe record for PostgREST while retaining decimal precision."""
    return payload.model_dump(mode="json", exclude=exclude)


def _item_row(payload: InventoryItemInput, user_id: str) -> dict:
    row = _json_payload(payload)
    row["description"] = (row["description"] or "").strip() or None
    row["updated_by"] = user_id
    return row


def _load_item(db, organisation_id: str, item_id: str) -> dict:
    return _one(
        db.table("inventory_items")
        .select("*")
        .eq("id", item_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Inventory item not found",
    )


def _on_hand_by_item(db, organisation_id: str) -> dict[str, Decimal]:
    rows = (
        db.table("inventory_stock_movements")
        .select("inventory_item_id, quantity")
        .eq("organisation_id", organisation_id)
        .limit(10000)
        .execute()
        .data
        or []
    )
    balances: dict[str, Decimal] = {}
    for row in rows:
        item_id = str(row.get("inventory_item_id") or "")
        if item_id:
            balances[item_id] = balances.get(item_id, Decimal("0")) + Decimal(str(row.get("quantity") or 0))
    return balances


@router.get("/summary")
def inventory_summary(organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    items = (
        db.table("inventory_items")
        .select("id, item_type, active, standard_cost, reorder_point")
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    on_hand = _on_hand_by_item(db, organisation_id)
    stock_items = [row for row in items if row.get("item_type") == "stock_item" and row.get("active")]
    low_stock = sum(
        1 for row in stock_items if on_hand.get(str(row["id"]), Decimal("0")) <= Decimal(str(row.get("reorder_point") or 0))
    )
    stock_value = sum(
        on_hand.get(str(row["id"]), Decimal("0")) * Decimal(str(row.get("standard_cost") or 0))
        for row in stock_items
    )
    return {
        "stock_item_count": len(stock_items),
        "service_count": sum(1 for row in items if row.get("item_type") == "service" and row.get("active")),
        "low_stock_count": low_stock,
        "stock_value": float(stock_value),
    }


@router.get("/items")
def list_inventory_items(
    organisation_id: str,
    auth: UserAuth,
    item_type: Optional[InventoryItemType] = None,
    include_archived: bool = False,
    search: Optional[str] = Query(default=None, max_length=200),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    query = db.table("inventory_items").select("*").eq("organisation_id", organisation_id).order("code").limit(1000)
    if item_type:
        query = query.eq("item_type", item_type)
    if not include_archived:
        query = query.eq("active", True)
    rows = query.execute().data or []
    needle = (search or "").strip().lower()
    if needle:
        rows = [
            row for row in rows
            if needle in " ".join(str(row.get(key) or "").lower() for key in ("code", "name", "description"))
        ]
    on_hand = _on_hand_by_item(db, organisation_id)
    for row in rows:
        row["on_hand"] = float(on_hand.get(str(row["id"]), Decimal("0")))
    return rows


@router.post("/items", status_code=201)
def create_inventory_item(payload: InventoryItemInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    row = _item_row(payload, str(user_id))
    row["created_by"] = str(user_id)
    try:
        result = db.table("inventory_items").insert(row).execute()
        return _one(result, "Inventory item create failed")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to save inventory item: {exc}") from exc


@router.patch("/items/{item_id}")
def update_inventory_item(item_id: str, payload: InventoryItemInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    _load_item(db, payload.organisation_id, item_id)
    try:
        (
            db.table("inventory_items")
            .update(_item_row(payload, str(user_id)))
            .eq("id", item_id)
            .eq("organisation_id", payload.organisation_id)
            .execute()
        )
        return _load_item(db, payload.organisation_id, item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to save inventory item: {exc}") from exc


def _load_structure(db, organisation_id: str, structure_id: str) -> dict:
    structure = _one(
        db.table("inventory_structures")
        .select("*")
        .eq("id", structure_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Inventory structure not found",
    )
    structure["components"] = (
        db.table("inventory_structure_lines")
        .select("*")
        .eq("structure_id", structure_id)
        .eq("organisation_id", organisation_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    return structure


def _validate_structure_items(db, payload: InventoryStructureInput) -> None:
    item_ids = {payload.output_item_id, *(line.component_item_id for line in payload.components)}
    if len(item_ids) != len(payload.components) + 1:
        raise HTTPException(status_code=400, detail="An item may only appear once in a structure and cannot be its own component")
    rows = (
        db.table("inventory_items")
        .select("id")
        .eq("organisation_id", payload.organisation_id)
        .in_("id", list(item_ids))
        .execute()
        .data
        or []
    )
    if len(rows) != len(item_ids):
        raise HTTPException(status_code=400, detail="All structure items must belong to this organisation")


def _replace_structure_lines(db, payload: InventoryStructureInput, structure_id: str) -> None:
    db.table("inventory_structure_lines").delete().eq("structure_id", structure_id).eq(
        "organisation_id", payload.organisation_id
    ).execute()
    db.table("inventory_structure_lines").insert([
        {
            "organisation_id": payload.organisation_id,
            "structure_id": structure_id,
            **_json_payload(line),
            "sort_order": index,
        }
        for index, line in enumerate(payload.components)
    ]).execute()


@router.get("/structures")
def list_inventory_structures(
    organisation_id: str,
    auth: UserAuth,
    structure_type: Optional[StructureType] = None,
    include_archived: bool = False,
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    query = db.table("inventory_structures").select("*").eq("organisation_id", organisation_id).order("name").limit(1000)
    if structure_type:
        query = query.eq("structure_type", structure_type)
    if not include_archived:
        query = query.eq("active", True)
    rows = query.execute().data or []
    item_ids = [str(row["output_item_id"]) for row in rows]
    item_map = {}
    if item_ids:
        item_map = {
            str(row["id"]): row
            for row in (
                db.table("inventory_items").select("id, code, name, unit_of_measure").in_("id", item_ids).execute().data or []
            )
        }
    for row in rows:
        output = item_map.get(str(row["output_item_id"]), {})
        row["output_item"] = output
    return rows


@router.get("/structures/{structure_id}")
def get_inventory_structure(structure_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    return _load_structure(db, organisation_id, structure_id)


@router.post("/structures", status_code=201)
def create_inventory_structure(payload: InventoryStructureInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    _validate_structure_items(db, payload)
    header = _json_payload(payload, exclude={"components"})
    header.update({"created_by": str(user_id), "updated_by": str(user_id)})
    try:
        created = _one(db.table("inventory_structures").insert(header).execute(), "Inventory structure create failed")
        _replace_structure_lines(db, payload, str(created["id"]))
        return _load_structure(db, payload.organisation_id, str(created["id"]))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to save inventory structure: {exc}") from exc


@router.patch("/structures/{structure_id}")
def update_inventory_structure(structure_id: str, payload: InventoryStructureInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    _load_structure(db, payload.organisation_id, structure_id)
    _validate_structure_items(db, payload)
    header = _json_payload(payload, exclude={"components"})
    header["updated_by"] = str(user_id)
    try:
        db.table("inventory_structures").update(header).eq("id", structure_id).eq(
            "organisation_id", payload.organisation_id
        ).execute()
        _replace_structure_lines(db, payload, structure_id)
        return _load_structure(db, payload.organisation_id, structure_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to save inventory structure: {exc}") from exc


@router.get("/movements")
def list_stock_movements(
    organisation_id: str,
    auth: UserAuth,
    inventory_item_id: Optional[str] = None,
    limit: int = Query(default=250, ge=1, le=1000),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    query = (
        db.table("inventory_stock_movements")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("occurred_on", desc=True)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if inventory_item_id:
        query = query.eq("inventory_item_id", inventory_item_id)
    rows = query.execute().data or []
    item_ids = list({str(row["inventory_item_id"]) for row in rows})
    names = {}
    if item_ids:
        names = {
            str(row["id"]): row
            for row in (db.table("inventory_items").select("id, code, name, unit_of_measure").in_("id", item_ids).execute().data or [])
        }
    for row in rows:
        row["item"] = names.get(str(row["inventory_item_id"]), {})
    return rows


@router.post("/movements", status_code=201)
def create_stock_movement(payload: StockMovementInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        assert_accounting_period_unlocked(
            db,
            organisation_id=payload.organisation_id,
            transaction_date=payload.occurred_on,
            action="Record inventory stock movement",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = _load_item(db, payload.organisation_id, payload.inventory_item_id)
    if item.get("item_type") != "stock_item":
        raise HTTPException(status_code=400, detail="Stock movements can only be recorded for stock items")
    try:
        result = db.table("inventory_stock_movements").insert(
            {**_json_payload(payload), "created_by": str(user_id)}
        ).execute()
        return _one(result, "Stock movement create failed")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to record stock movement: {exc}") from exc
