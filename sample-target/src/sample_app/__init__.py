"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

import time
from flask import Flask, jsonify, request, make_response


def create_app() -> Flask:
    app = Flask(__name__)
    start_time = time.time()
    rate_limit_data = {}

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    @app.get("/status")
    def status() -> Flask.Response:
        client_ip = request.remote_addr
        current_time = time.time()

        # Initialize rate limit data for new IPs
        if client_ip not in rate_limit_data:
            rate_limit_data[client_ip] = []

        # Filter out timestamps older than 10 seconds
        rate_limit_data[client_ip] = [timestamp for timestamp in rate_limit_data[client_ip] if current_time - timestamp < 10]

        # Check if the IP has exceeded the rate limit
        if len(rate_limit_data[client_ip]) >= 5:
            response = make_response('', 429)
            response.headers['Retry-After'] = 10
            return response

        # Record the current request timestamp
        rate_limit_data[client_ip].append(current_time)

        uptime_seconds = int(current_time - start_time)
        return jsonify(status="ok", uptime_seconds=uptime_seconds)

    return app
