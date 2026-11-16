# Remote mode only (streamable HTTP + OAuth). Local use needs no container.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Non-root user; /data holds the SQLite audit log (mount a volume there).
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app:app /data && chmod 700 /data
WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
RUN pip install -r requirements.txt
COPY src ./src
RUN pip install --no-deps .

USER app
VOLUME /data
ENV EBAY_MCP_TRANSPORT=streamable-http EBAY_MCP_HOST=0.0.0.0 EBAY_MCP_PORT=8000 EBAY_MCP_DB=/data/state.db
EXPOSE 8000
# Secrets (eBay keys, refresh token, IdP settings) come from the host's secret store as env vars.
# The server exits at startup if the OAuth settings are missing.
CMD ["guarded-ebay-mcp"]
