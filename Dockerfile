FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# The app runs unprivileged; it only writes its spend ledger and traces, so
# that one folder is the only thing it owns. Mount a volume here to keep them.
RUN useradd --create-home silo && mkdir -p /app/.silo && chown silo:silo /app/.silo
ENV SILO_STATE_DIR=/app/.silo
USER silo
# Railway passes PORT (usually 8080); point the public domain at that port.
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "silo", "serve", "--http", "--host", "0.0.0.0"]
