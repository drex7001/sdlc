"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    return app
