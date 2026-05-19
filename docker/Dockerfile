FROM python:3.12-slim

# System deps: git for any future git-based features; nothing else needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (cacheable layer)
COPY pyproject.toml /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        typer pydantic pyyaml rich anthropic openai \
        fastapi 'uvicorn[standard]' jinja2 python-multipart \
        pytest pytest-cov ruff mypy bandit httpx flask

# Now the source.
COPY . /app
RUN pip install --no-cache-dir -e . && chmod -R a+rwX /app

# Make the venv-less Python see ruff/mypy/etc on PATH (they're already in /usr/local/bin from pip).
ENV PYTHONUNBUFFERED=1 \
    PIPELINE_LLM_PROVIDER=mock

EXPOSE 8000

# Default command: serve the dashboard. Override with `pipeline run ...` to drive the CLI.
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
