"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import Flask, jsonify, request, make_response
import time
from collections import defaultdict


def create_app() -> Flask:
    app = Flask(__name__)

    request_counts = defaultdict(lambda: {'count': 0, 'start_time': time.time()})
    RATE_LIMIT = 5
    TIME_WINDOW = 10  # seconds

    def is_rate_limited(ip: str) -> bool:
        current_time = time.time()
        if current_time - request_counts[ip]['start_time'] > TIME_WINDOW:
            request_counts[ip] = {'count': 0, 'start_time': current_time}
        request_counts[ip]['count'] += 1
        return request_counts[ip]['count'] > RATE_LIMIT

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    @app.get("/status")
    def status() -> dict[str, str | int]:
        client_ip = request.remote_addr
        if is_rate_limited(client_ip):
            response = make_response('', 429)
            response.headers['Retry-After'] = TIME_WINDOW
            return response
        uptime_seconds = int(time.time() - app.config['START_TIME'])
        return jsonify(status="ok", uptime_seconds=uptime_seconds)

    app.config['START_TIME'] = time.time()

    return app
