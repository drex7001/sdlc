"""Items CRUD module with in-memory store and FastAPI router."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field


class ItemCreate(BaseModel):
    """Request schema for creating an item."""

    name: str = Field(..., min_length=1)
    description: str | None = Field(default=None)


class Item(BaseModel):
    """Response schema for an item."""

    id: int
    name: str
    description: str | None


class ItemStore:
    """In-memory store for items with monotonic ID assignment.

    This is a single-threaded, process-local store. State is ephemeral
    and lost on restart. A future spec can add persistence and locking
    if needed.
    """

    def __init__(self) -> None:
        """Initialize the store with an empty item list and counter."""
        self._items: list[Item] = []
        self._next_id: int = 1

    def create(self, item_create: ItemCreate) -> Item:
        """Create and store a new item, assigning the next monotonic ID.

        Args:
            item_create: The request payload with name and optional description.

        Returns:
            The created Item with server-assigned id.
        """
        item = Item(
            id=self._next_id,
            name=item_create.name,
            description=item_create.description,
        )
        self._next_id += 1
        self._items.append(item)
        return item


def create_items_router(store: ItemStore) -> APIRouter:
    """Create and return a FastAPI router for items endpoints.

    Args:
        store: The ItemStore instance to use for persistence.

    Returns:
        A FastAPI APIRouter with POST /items endpoint.
    """
    router = APIRouter(prefix="", tags=["items"])

    @router.post("/items", status_code=201, response_model=Item)
    def create_item(item_create: ItemCreate) -> Item:
        """Create a new item.

        Args:
            item_create: Request body with name and optional description.

        Returns:
            The created item with server-assigned id and HTTP 201.
        """
        return store.create(item_create)

    return router
