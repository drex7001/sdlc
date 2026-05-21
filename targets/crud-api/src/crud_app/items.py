from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

items_router = APIRouter()

class Item(BaseModel):
    name: str
    description: str | None = None

class ItemResponse(Item):
    id: int

items_store: list[ItemResponse] = []

@items_router.post("/items", response_model=ItemResponse, status_code=201)
def create_item(item: Item) -> ItemResponse:
    item_id = len(items_store) + 1
    item_response = ItemResponse(id=item_id, **item.model_dump())
    items_store.append(item_response)
    return item_response
