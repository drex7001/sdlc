"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import Flask

from .status import bp as status_bp


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    app.register_blueprint(status_bp)
    return app
