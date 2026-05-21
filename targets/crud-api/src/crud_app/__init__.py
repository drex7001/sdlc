"""FastAPI CRUD application built up spec-by-spec by the pipeline.

The baseline ships with only a /health endpoint. Each CRUD spec (create / list
/ delete) layered on top adds one route and its accompanying tests.
"""

from __future__ import annotations

from fastapi import FastAPI

from .items import items_router


def create_app() -> FastAPI:
    app = FastAPI(title="crud-api", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(items_router)

    return app
