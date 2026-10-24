FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home silo
USER silo
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "silo", "serve", "--http", "--host", "0.0.0.0"]
