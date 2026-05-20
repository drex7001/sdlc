"""FastAPI CRUD application built up spec-by-spec by the pipeline.

The baseline ships with only a /health endpoint. Each CRUD spec (create / list
/ delete) layered on top adds one route and its accompanying tests.
"""

from __future__ import annotations

from fastapi import FastAPI

from crud_app.items import ItemStore, create_items_router


def create_app() -> FastAPI:
    app = FastAPI(title="crud-api", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Wire in the items CRUD module
    item_store = ItemStore()
    items_router = create_items_router(item_store)
    app.include_router(items_router)

    return app
