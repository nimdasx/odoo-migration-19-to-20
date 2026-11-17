FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy dependency definition & install via uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Copy application source code & default template config
COPY src/ ./src/
COPY config.example.json ./config.json

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
